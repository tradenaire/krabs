"""/scan — LLM + web-search шорт-пикер."""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.ai.scanner import (scan_overbought, analyze_single_coin, mexc_find_futures_symbol,
                            format_coin_card, validate_short_pick)
from bot.ai.analyst import (deep_short_analysis, parse_analyst_blocks, extract_sentiment,
                             format_usage_footer, DEFAULT_MODEL, FALLBACK_MODEL)

logger = logging.getLogger(__name__)

# MEXC enforces minimum 5 USDT notional by default (code 7008).
# Cached per-symbol minimums override this (populated from actual errors).
_MEXC_DEFAULT_MIN_NOTIONAL = 5.0


def _get_min_notional(symbol: str, bot_data: dict) -> float:
    """Minimum USDT notional (position value) for an order on this symbol."""
    cache = bot_data.get("_min_order_cache", {})
    return cache.get(symbol, _MEXC_DEFAULT_MIN_NOTIONAL)


def _get_min_avg_margin(symbol: str, leverage: int, bot_data: dict) -> float:
    """Actual margin to use for averaging (with 5% buffer for contract rounding)."""
    return _get_min_notional(symbol, bot_data) / max(leverage, 1) * 1.05


def _can_avg_at_configured(symbol: str, leverage: int, averaging_amount: float, bot_data: dict) -> bool:
    """True if averaging_amount * leverage covers the MEXC minimum notional."""
    return averaging_amount * max(leverage, 1) >= _get_min_notional(symbol, bot_data)


def _check_budget(free_balance: float, margin: float, config,
                  eff_avg_amount: float | None = None) -> dict:
    """
    Check whether free_balance covers the full position risk budget.
    Formula (same as auto_scan_job):
        base_budget = margin + averaging_budget
        full_budget = base_budget * (sl_pct / 100)   -- unless profit_lock is set
    This is the worst-case capital at risk per position.
    eff_avg_amount: override for symbol-specific minimum (e.g. MEXC 7008 cache)
    """
    avg_amount = eff_avg_amount or float(getattr(config, "averaging_amount", 0.10))
    avg_budget = float(getattr(config, "averaging_budget", 5.00))
    sl_pct = float(getattr(config, "sl_pct", 500))
    profit_lock_trigger = float(getattr(config, "averaging_profit_lock_trigger", 0))

    base_budget = margin + avg_budget
    full_budget = base_budget * (sl_pct / 100.0)

    max_steps = int(avg_budget / avg_amount) if avg_amount > 0 else 0
    # How many full positions the current balance can support
    positions_possible = int(free_balance / full_budget) if full_budget > 0 else 0

    return {
        "can_open": free_balance >= margin,
        "can_full_budget": free_balance >= full_budget,
        "full_budget": full_budget,
        "base_budget": base_budget,
        "positions_possible": positions_possible,
        "max_steps": max_steps,
        "sl_pct": sl_pct,
        "free": free_balance,
    }


async def scan_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = context.bot_data["config"]
    client = context.bot_data["exchange"]

    args = context.args or []
    try:
        n = max(1, min(int(args[0]), 20)) if args else 5
    except (ValueError, IndexError):
        n = 5

    api_key = config.openrouter_api_key
    if not api_key:
        await update.message.reply_text(
            "Нет OpenRouter ключа. Добавь через /setkey openrouter_api_key sk-or-..."
        )
        return

    status = await update.message.reply_text("📡 Собираю MEXC snapshot...")

    try:
        local_results, _total = await scan_overbought(client, 65.0, 10.0)
    except Exception as e:
        logger.warning("Local scan failed: %s", e)
        local_results = []

    model = getattr(config, "openrouter_model", DEFAULT_MODEL) or DEFAULT_MODEL
    await status.edit_text(f"🔍 Передаю MEXC snapshot в {model}...")

    ai_result = await deep_short_analysis(local_results, api_key, model=model, n=n)

    if ai_result.error and not ai_result.text:
        logger.warning("Primary model failed, trying fallback %s", FALLBACK_MODEL)
        await status.edit_text(f"🌐 Пробую {FALLBACK_MODEL}...")
        ai_result = await deep_short_analysis(local_results, api_key, model=FALLBACK_MODEL, n=n)

    if ai_result.error and not ai_result.text:
        await status.edit_text(f"❌ AI недоступен: {ai_result.error}")
        return

    picks = parse_analyst_blocks(ai_result.text, n=n)
    if not picks:
        await status.edit_text(
            f"📝 AI ответил не по формату:\n\n{ai_result.text[:3000]}\n\n"
            f"{format_usage_footer(ai_result)}",
            parse_mode="Markdown",
        )
        return

    await status.edit_text(f"✅ AI выдал {len(picks)} монет. Валидирую MEXC trend/MSB/risk...")

    try:
        open_positions = await client.get_positions()
        open_coins = {p["symbol"].split("/")[0] for p in open_positions}
        open_pos_by_coin = {p["symbol"].split("/")[0]: p for p in open_positions}
    except Exception:
        open_positions = []
        open_coins = set()
        open_pos_by_coin = {}

    validated: list[dict] = []
    existing_picks: list[tuple[str, dict]] = []  # (fut_sym, pick) for already-open coins
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()

    for pick in picks:
        ticker = pick["ticker"].upper()
        if ticker in seen:
            continue
        seen.add(ticker)

        fut_sym = await mexc_find_futures_symbol(client, ticker)
        if not fut_sym:
            skipped.append((ticker, "нет на MEXC"))
            continue

        coin = fut_sym.split("/")[0]
        if coin in open_coins:
            existing_picks.append((fut_sym, pick))
            continue

        tech = await analyze_single_coin(client, fut_sym)
        if not tech:
            skipped.append((ticker, "нет OHLCV"))
            continue
        validation_status, validation_errors = validate_short_pick(tech)
        tech["validation_status"] = validation_status
        tech["validation_errors"] = validation_errors
        if validation_status != "VALIDATED":
            skipped.append((ticker, "; ".join(validation_errors[:2]) or validation_status))
            continue
        tech["_ai_fund"] = pick.get("fund", "")
        tech["_ai_funding"] = pick.get("funding", "")
        tech["_ai_risk"] = pick.get("risk", "")
        tech["_ai_risk_num"] = pick.get("risk_num")
        validated.append(tech)

    try:
        await status.delete()
    except Exception:
        pass

    if not validated and not existing_picks:
        summary = ", ".join(f"{t} ({r})" for t, r in skipped[:5])
        await update.message.reply_text(f"Ничего не прошло проверку MEXC.\nПропущено: {summary}")
        return

    default_bet = float(getattr(config, "default_trade_usdt", 0.20))
    averaging_amount = float(getattr(config, "averaging_amount", 0.10))
    free_balance = float(context.bot_data.get("_bal_cache", 0.0))
    budget_info = _check_budget(free_balance, default_bet, config)

    # Compute leverage and averaging-risk for each validated coin
    for tech in validated:
        sym = tech["symbol"]
        try:
            sym_max = await client.get_max_leverage(sym)
        except Exception:
            sym_max = 100
        user_lev = int(getattr(config, "default_leverage", 0) or 0)
        lev_eff = min(user_lev, sym_max) if user_lev > 0 else sym_max
        tech["_lev_eff"] = lev_eff
        min_avg = _get_min_avg_margin(sym, lev_eff, context.bot_data)
        tech["_min_avg"] = min_avg
        tech["_avg_ok"] = _can_avg_at_configured(sym, lev_eff, averaging_amount, context.bot_data)

    # Sort: OK averaging first, risky (impossible to avg at configured amount) last
    validated.sort(key=lambda t: (0 if t["_avg_ok"] else 1))

    header = f"*🎯 SmartScan top-{len(validated)} шорт ({ai_result.model})*"
    header += "\n_Источник цены/тренда/risk: MEXC post-validation_"
    if existing_picks:
        header += f"\n_уже в позиции: {', '.join(s.split('/')[0] for s, _ in existing_picks)}_"
    if skipped:
        header += f"\n_пропущено: {', '.join(t for t, _ in skipped[:5])}_"
    if not budget_info["can_open"]:
        header += f"\n⛔ *Баланс* `${free_balance:.2f}` — недостаточно даже на открытие (`${default_bet:.2f}`)"
    elif not budget_info["can_full_budget"]:
        header += (
            f"\n⚠️ *Баланс* `${free_balance:.2f}` — хватит на "
            f"`{budget_info['positions_possible']}` полных поз"
            f" (нужно `${budget_info['full_budget']:.2f}` на 1 поз · "
            f"маржа+докупки `${budget_info['base_budget']:.2f}` × SL {budget_info['sl_pct']:.0f}%)"
        )
    await update.message.reply_text(header, parse_mode="Markdown")

    # Output validated candidates
    for i, r in enumerate(validated, 1):
        sym = r["symbol"]
        direction = r.get("direction", "short")
        side_code = "sell" if direction == "short" else "buy"
        lev_eff = r["_lev_eff"]
        min_avg = r["_min_avg"]
        avg_ok = r["_avg_ok"]

        card = format_coin_card(r, i, ai_note=r.get("_ai_fund", ""),
                                max_lev=lev_eff, margin=default_bet)
        if r.get("_ai_funding"):
            card += f"\n   Фандинг (AI): {r['_ai_funding']}"
        if r.get("_ai_risk"):
            card += f"\n   Риск (AI): {r['_ai_risk']}"

        icon = "🔻" if direction == "short" else "🔺"

        if r.get("validation_status") == "VALIDATED" and avg_ok:
            btn = InlineKeyboardButton(
                f"{icon} ${default_bet:g} · {lev_eff}x",
                callback_data=f"open_{side_code}_{sym}",
            )
        elif r.get("validation_status") == "VALIDATED":
            # Averaging minimum exceeds configured amount — show warning
            card += (
                f"\n   ⚠️ *Мин. докупка MEXC* `${min_avg:.2f}` > настройка `${averaging_amount:.2f}`"
                f"\n   При открытии докупка будет по `${min_avg:.2f}`"
            )
            btn = InlineKeyboardButton(
                f"⚠️ Открыть (докупка ~${min_avg:.2f})",
                callback_data=f"open_anyway_{side_code}_{sym}",
            )
        else:
            card += "\n   ⛔ Открытие заблокировано: MEXC validation не пройдена"
            btn = InlineKeyboardButton("⛔ Не actionable", callback_data="noop_scan_blocked")

        kb = InlineKeyboardMarkup([[btn]])
        try:
            await update.message.reply_text(card, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            await update.message.reply_text(card, reply_markup=kb)

    # Output existing-position cards
    if existing_picks:
        await update.message.reply_text("*📂 Уже открыты:*", parse_mode="Markdown")
        for fut_sym, pick in existing_picks:
            coin = fut_sym.split("/")[0]
            pos = open_pos_by_coin.get(coin, {})
            pnl_pct = float(pos.get("percentage", 0))
            pnl_usd = float(pos.get("unrealized_pnl", 0))
            margin = float(pos.get("margin", 0))
            lev = int(pos.get("leverage", 1))
            avg_count = 0
            from bot import db as db_mod
            db_rec = db_mod.get_open_position(fut_sym)
            if db_rec:
                avg_count = db_rec.get("averaging_count", 0)
            max_count = int(getattr(config, "max_averaging_count", 100))

            min_avg = _get_min_avg_margin(fut_sym, lev, context.bot_data)
            step = max(averaging_amount, min_avg)
            step_note = f" (мин MEXC)" if min_avg > averaging_amount else ""

            text = (
                f"*{coin}* ×{lev} | Маржа `${margin:.3f}`\n"
                f"PnL: `{pnl_pct:+.1f}%` / `${pnl_usd:+.3f}`\n"
                f"Докупок: `{avg_count}/{max_count}`\n"
                f"AI: _{pick.get('fund', '—')}_"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"📈 Докупить +${step:.2f}{step_note}",
                    callback_data=f"scan_avg_{fut_sym}",
                )
            ]])
            try:
                await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
            except Exception:
                await update.message.reply_text(text, reply_markup=kb)

    # Sentiment + cost footer
    tail = []
    sentiment = extract_sentiment(ai_result.text)
    if sentiment:
        tail.append(f"📝 _{sentiment}_")
    tail.append(format_usage_footer(ai_result))
    try:
        await update.message.reply_text("\n\n".join(tail), parse_mode="Markdown")
    except Exception:
        await update.message.reply_text("\n\n".join(tail))

    context.bot_data["last_scan"] = validated


async def _do_execute_open(q, client, app, symbol: str, side: str,
                           margin: float, leverage: int):
    """Shared open logic used by open_callback and open_confirm_callback."""
    coin = symbol.split("/")[0]
    side_ru = "SHORT" if side == "sell" else "LONG"
    await q.message.reply_text(f"🚀 Открываю {side_ru} `{coin}` ${margin:g} ×{leverage}...",
                                parse_mode="Markdown")
    from bot.handlers.trading import execute_open
    result = await execute_open(client, app, symbol, side, margin, leverage)
    icon = "🔻" if side == "sell" else "🟩"
    lines = [
        f"*{coin}* {icon}×{result['leverage']} `${margin:.2f}`",
        f"▶ Entry: `{result['entry_price']:.6g}`",
    ]
    if result.get("liquidation_price"):
        lines.append(f"💀 Liq: `{result['liquidation_price']:.6g}`")
    if result.get("tp_price"):
        lines.append(f"✅ TP: `{result['tp_price']:.6g}`")
    if result.get("sl_price"):
        lines.append(f"🛑 SL: `{result['sl_price']:.6g}`")
    await q.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def open_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles open_{side}_{symbol} — budget-check then open."""
    q = update.callback_query
    await q.answer()

    parts = q.data.split("_", 2)
    if len(parts) < 3:
        return
    _, side, symbol = parts
    config = context.bot_data["config"]
    client = context.bot_data["exchange"]

    margin = float(getattr(config, "default_trade_usdt", 0.20))

    # Live balance check
    try:
        free = await client.get_free_futures_balance()
    except Exception:
        free = float(context.bot_data.get("_bal_cache", 0.0))
    _cfg_avg = float(getattr(config, "averaging_amount", 0.10))
    _lev_for_chk = int(getattr(config, "default_leverage", 10) or 10)
    # Pre-fetch live min notional from MEXC so cache is accurate before first order
    try:
        _live_min_margin = await client.get_min_order_usdt(symbol, _lev_for_chk)
        if _live_min_margin > 0:
            _live_notional = _live_min_margin * max(_lev_for_chk, 1)
            _cache = context.bot_data.setdefault("_min_order_cache", {})
            if _live_notional > _cache.get(symbol, 0):
                _cache[symbol] = _live_notional
                try:
                    from bot import db as db_mod
                    db_mod.set_min_order_notional(symbol, _live_notional)
                except Exception:
                    pass
    except Exception:
        pass
    _min_avg_open = _get_min_avg_margin(symbol, _lev_for_chk, context.bot_data)
    _eff_avg = max(_cfg_avg, _min_avg_open)
    budget = _check_budget(free, margin, config, eff_avg_amount=_eff_avg)

    if not budget["can_open"]:
        await q.message.reply_text(
            f"❌ Недостаточно баланса: `${free:.2f}` < маржа `${margin:.2f}`",
            parse_mode="Markdown",
        )
        return

    if not budget["can_full_budget"]:
        coin = symbol.split("/")[0]
        side_ru = "SHORT" if side == "sell" else "LONG"
        margin_milli = int(margin * 1000)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"⚠️ Открыть ({budget['positions_possible']} полных поз доступно)",
                callback_data=f"open_confirm_{side}_{margin_milli}_{symbol}",
            )
        ]])
        _avg_note = (
            f"\n⚠️ Докупка: `${_eff_avg:.2f}`/шаг (мин. MEXC), настроено `${_cfg_avg:.2f}`"
            if _eff_avg > _cfg_avg + 0.001 else ""
        )
        await q.message.reply_text(
            f"⚠️ *{coin}* {side_ru}: недостаточный бюджет\n"
            f"Свободно `${free:.2f}` · нужно `${budget['full_budget']:.2f}` на 1 поз\n"
            f"_(маржа+докупки `${budget['base_budget']:.2f}` × SL {budget['sl_pct']:.0f}%)_"
            + _avg_note +
            f"\nХватит на `{budget['positions_possible']}` полных позиций. Открыть всё равно?",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    user_lev = int(getattr(config, "default_leverage", 0) or 0)
    try:
        sym_max = await client.get_max_leverage(symbol)
    except Exception:
        sym_max = 100
    leverage = min(user_lev, sym_max) if user_lev > 0 else sym_max

    if _eff_avg > _cfg_avg + 0.001:
        _coin = symbol.split("/")[0]
        _side_ru = "SHORT" if side == "sell" else "LONG"
        _margin_milli = int(margin * 1000)
        _kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✅ Открыть {_side_ru} (докупки ${_eff_avg:.2f}/шаг)",
                callback_data=f"open_confirm_{side}_{_margin_milli}_{symbol}",
            )
        ]])
        await q.message.reply_text(
            f"⚠️ *{_coin}* {_side_ru}: мин. докупка `${_eff_avg:.2f}`/шаг (MEXC), "
            f"настроено `${_cfg_avg:.2f}`\n"
            f"Открыть позицию с учётом повышенного мин. ордера?",
            parse_mode="Markdown",
            reply_markup=_kb,
        )
        return
    try:
        await _do_execute_open(q, client, context.application, symbol, side, margin, leverage)
    except Exception as e:
        await q.message.reply_text(f"❌ Ошибка: {e}")


async def open_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles open_confirm_{side}_{margin_milli}_{symbol} — opens ignoring budget warning."""
    q = update.callback_query
    await q.answer()

    # open_confirm_{side}_{margin_milli}_{symbol}
    parts = q.data.split("_", 4)  # ["open","confirm",side,margin_milli,symbol]
    side = parts[2]
    margin = int(parts[3]) / 1000.0
    symbol = parts[4]

    config = context.bot_data["config"]
    client = context.bot_data["exchange"]

    user_lev = int(getattr(config, "default_leverage", 0) or 0)
    try:
        sym_max = await client.get_max_leverage(symbol)
    except Exception:
        sym_max = 100
    leverage = min(user_lev, sym_max) if user_lev > 0 else sym_max

    try:
        await _do_execute_open(q, client, context.application, symbol, side, margin, leverage)
    except Exception as e:
        await q.message.reply_text(f"❌ Ошибка: {e}")


async def open_anyway_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles open_anyway_{side}_{symbol} — opens even if averaging minimum > configured amount."""
    q = update.callback_query
    await q.answer()

    # parse: open_anyway_{side}_{symbol}
    data = q.data  # e.g. "open_anyway_sell_BTC/USDT:USDT"
    prefix = "open_anyway_"
    rest = data[len(prefix):]  # "sell_BTC/USDT:USDT"
    side, symbol = rest.split("_", 1)

    config = context.bot_data["config"]
    client = context.bot_data["exchange"]

    margin = float(getattr(config, "default_trade_usdt", 0.20))
    user_lev = int(getattr(config, "default_leverage", 0) or 0)
    try:
        sym_max = await client.get_max_leverage(symbol)
    except Exception:
        sym_max = 100
    leverage = min(user_lev, sym_max) if user_lev > 0 else sym_max

    # Cache the minimum so averaging_job uses correct amount from first attempt
    min_avg = _get_min_avg_margin(symbol, leverage, context.bot_data)
    # The min notional to cache (reverse from margin)
    min_notional = min_avg * max(leverage, 1) / 1.05
    cache = context.bot_data.setdefault("_min_order_cache", {})
    cache[symbol] = min_notional
    try:
        from bot import db as db_mod
        db_mod.set_min_order_notional(symbol, min_notional)
    except Exception:
        pass
    logger.info("open_anyway: cached min notional $%.1f for %s (avg will use $%.3f)",
                min_notional, symbol, min_avg)

    coin = symbol.split("/")[0]
    side_ru = "SHORT" if side == "sell" else "LONG"
    await q.message.reply_text(
        f"🚀 Открываю {side_ru} `{coin}` ${margin:g} ×{leverage}\n"
        f"⚠️ Докупка будет по `${min_avg:.2f}` (мин MEXC)",
        parse_mode="Markdown",
    )

    try:
        from bot.handlers.trading import execute_open
        result = await execute_open(client, context.application, symbol, side, margin, leverage)
        icon = "🔻" if side == "sell" else "🟩"
        lines = [
            f"*{coin}* {icon}×{result['leverage']} `${margin:.2f}`",
            f"▶ Entry: `{result['entry_price']:.6g}`",
        ]
        if result.get("liquidation_price"):
            lines.append(f"💀 Liq: `{result['liquidation_price']:.6g}`")
        if result.get("tp_price"):
            lines.append(f"✅ TP: `{result['tp_price']:.6g}`")
        if result.get("sl_price"):
            lines.append(f"🛑 SL: `{result['sl_price']:.6g}`")
        lines.append(f"📊 Автодокупка: `${min_avg:.2f}`/шаг")
        await q.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await q.message.reply_text(f"❌ Ошибка: {e}")


async def scan_avg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles scan_avg_{symbol} — immediately places one averaging step for an existing position."""
    q = update.callback_query
    await q.answer()

    symbol = q.data[len("scan_avg_"):]
    coin = symbol.split("/")[0]
    config = context.bot_data["config"]
    client = context.bot_data["exchange"]

    averaging_amount = float(getattr(config, "averaging_amount", 0.10))

    try:
        pos = await client.get_position(symbol)
    except Exception as e:
        await q.message.reply_text(f"❌ Не могу получить позицию {coin}: {e}")
        return

    if not pos:
        await q.message.reply_text(f"❌ Позиция {coin} не найдена")
        return

    lev = int(pos.get("leverage") or 1)
    side = pos.get("side", "short")
    avg_side = "sell" if side == "short" else "buy"
    margin_mode = pos.get("margin_mode")

    min_avg = _get_min_avg_margin(symbol, lev, context.bot_data)
    step = max(averaging_amount, min_avg)

    # Check free balance
    free = context.bot_data.get("_bal_cache", 0.0)
    if free < step:
        try:
            free = await client.get_free_futures_balance()
        except Exception:
            pass
    if free < step:
        await q.message.reply_text(
            f"❌ Недостаточно баланса: `${free:.2f}` < `${step:.2f}`",
            parse_mode="Markdown",
        )
        return

    await q.message.reply_text(f"⏳ Докупаю `{coin}` +`${step:.2f}`...", parse_mode="Markdown")

    try:
        await client.place_futures_order(symbol, avg_side, step, lev, margin_mode=margin_mode)
    except Exception as e:
        await q.message.reply_text(f"❌ Ошибка докупки {coin}: {e}")
        return

    # Update DB
    from bot import db as db_mod
    db_rec = db_mod.get_open_position(symbol)
    if db_rec:
        new_total = float(db_rec.get("total_invested") or 0) + step
        new_count = int(db_rec.get("averaging_count") or 0) + 1
        db_mod.update_averaging(db_rec["id"], new_total, new_count)
        db_mod.log_trade(symbol, "avg", amount=step, note=f"#{new_count} manual")

    # Fetch updated position
    try:
        import asyncio
        await asyncio.sleep(1.5)
        pos_after = await client.get_position(symbol)
    except Exception:
        pos_after = None

    pnl_pct = float((pos_after or pos).get("percentage", 0))
    pnl_usd = float((pos_after or pos).get("unrealized_pnl", 0))
    new_entry = float((pos_after or pos).get("entry_price", 0))

    await q.message.reply_text(
        f"✅ *{coin}* докуплено +`${step:.2f}`\n"
        f"PnL: `{pnl_pct:+.1f}%` / `${pnl_usd:+.3f}`\n"
        f"Avg entry: `{new_entry:.6g}`",
        parse_mode="Markdown",
    )


async def avg_force_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles avg_force_{symbol} — force-average at MEXC minimum after auto-avg failed."""
    q = update.callback_query
    await q.answer()

    symbol = q.data[len("avg_force_"):]
    coin = symbol.split("/")[0]
    config = context.bot_data["config"]
    client = context.bot_data["exchange"]

    try:
        pos = await client.get_position(symbol)
    except Exception as e:
        await q.message.reply_text(f"❌ Не могу получить позицию {coin}: {e}")
        return

    if not pos:
        await q.message.reply_text(f"❌ Позиция {coin} не найдена")
        return

    lev = int(pos.get("leverage") or 1)
    side = pos.get("side", "short")
    avg_side = "sell" if side == "short" else "buy"
    margin_mode = pos.get("margin_mode")

    min_avg = _get_min_avg_margin(symbol, lev, context.bot_data)

    await q.message.reply_text(
        f"⏳ Принудительная докупка `{coin}` +`${min_avg:.2f}` (мин MEXC)...",
        parse_mode="Markdown",
    )

    try:
        await client.place_futures_order(symbol, avg_side, min_avg, lev, margin_mode=margin_mode)
    except Exception as e:
        await q.message.reply_text(f"❌ Ошибка докупки {coin}: {e}")
        return

    from bot import db as db_mod
    db_rec = db_mod.get_open_position(symbol)
    if db_rec:
        new_total = float(db_rec.get("total_invested") or 0) + min_avg
        new_count = int(db_rec.get("averaging_count") or 0) + 1
        db_mod.update_averaging(db_rec["id"], new_total, new_count)
        db_mod.log_trade(symbol, "avg", amount=min_avg, note=f"#{new_count} force-btn")

    try:
        import asyncio
        await asyncio.sleep(1.5)
        pos_after = await client.get_position(symbol)
    except Exception:
        pos_after = None

    pnl_pct = float((pos_after or pos).get("percentage", 0))
    pnl_usd = float((pos_after or pos).get("unrealized_pnl", 0))
    new_entry = float((pos_after or pos).get("entry_price", 0))

    await q.message.reply_text(
        f"✅ *{coin}* докуплено +`${min_avg:.2f}`\n"
        f"PnL: `{pnl_pct:+.1f}%` / `${pnl_usd:+.3f}`\n"
        f"Avg entry: `{new_entry:.6g}`",
        parse_mode="Markdown",
    )

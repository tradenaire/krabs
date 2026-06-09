"""/scan — LLM + web-search market picker."""
import logging
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


DEFAULT_MODEL = "openai/gpt-5.5:online"


async def scan_overbought(*args, **kwargs):
    from bot.ai.scanner import scan_overbought as _impl
    return await _impl(*args, **kwargs)


async def analyze_single_coin(*args, **kwargs):
    from bot.ai.scanner import analyze_single_coin as _impl
    return await _impl(*args, **kwargs)


async def mexc_find_futures_symbol(*args, **kwargs):
    from bot.ai.scanner import mexc_find_futures_symbol as _impl
    return await _impl(*args, **kwargs)


def format_coin_card(*args, **kwargs):
    from bot.ai.scanner import format_coin_card as _impl
    return _impl(*args, **kwargs)


async def deep_short_analysis(*args, **kwargs):
    from bot.ai.analyst import deep_short_analysis as _impl
    return await _impl(*args, **kwargs)


def parse_analyst_blocks(*args, **kwargs):
    from bot.ai.analyst import parse_analyst_blocks as _impl
    return _impl(*args, **kwargs)


def extract_sentiment(*args, **kwargs):
    from bot.ai.analyst import extract_sentiment as _impl
    return _impl(*args, **kwargs)


def format_usage_footer(*args, **kwargs):
    from bot.ai.analyst import format_usage_footer as _impl
    return _impl(*args, **kwargs)


def normalize_openrouter_model(*args, **kwargs):
    from bot.ai.analyst import normalize_openrouter_model as _impl
    return _impl(*args, **kwargs)


async def build_research_snapshot(*args, **kwargs):
    from bot.ai.research_snapshot import build_research_snapshot as _impl
    return await _impl(*args, **kwargs)


from bot.services import sizing as _sizing
from bot.services.ladder import parse_price
from bot.services.trade_plan import format_three_tp_plan, pick_from_plan, plan_fingerprint, plan_from_pick

# Exchanges enforce per-symbol minimum order notionals.
# Cached per-symbol minimums override the default (populated from actual errors).
_MEXC_DEFAULT_MIN_NOTIONAL = _sizing.MEXC_DEFAULT_MIN_NOTIONAL


def _get_min_notional(symbol: str, bot_data: dict) -> float:
    """Minimum USDT notional (position value) for an order on this symbol."""
    return _sizing.get_min_notional(symbol, bot_data.get("_min_order_cache", {}))


def _get_min_avg_margin(symbol: str, leverage: int, bot_data: dict) -> float:
    """Actual margin to use for averaging (with 5% buffer for contract rounding)."""
    return _sizing.get_min_avg_margin(symbol, leverage, bot_data.get("_min_order_cache", {}))


async def _get_live_min_avg_margin(client, symbol: str, leverage: int, bot_data: dict) -> float:
    """Contract-rounded exchange min margin, falling back to cached notional math."""
    fallback = _get_min_avg_margin(symbol, leverage, bot_data)
    cached_notional = bot_data.get("_min_order_cache", {}).get(symbol)
    try:
        live = await client.get_min_order_usdt(
            symbol, leverage, min_notional=cached_notional
        )
        return live or fallback
    except Exception:
        return fallback


def _can_avg_at_configured(symbol: str, leverage: int, averaging_amount: float, bot_data: dict) -> bool:
    """True if averaging_amount * leverage covers the exchange minimum notional."""
    return _sizing.can_avg_at_configured(
        symbol, leverage, averaging_amount, bot_data.get("_min_order_cache", {})
    )


def _check_budget(free_balance: float, margin: float, config,
                  eff_avg_amount: float | None = None) -> dict:
    """Worst-case capital-at-risk budget check (delegates to services.sizing)."""
    return _sizing.check_budget(free_balance, margin, config, eff_avg_amount)


async def scan_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = context.bot_data["config"]
    client = context.bot_data["exchange"]

    args = context.args or []
    try:
        requested = max(1, min(int(args[0]), 10)) if args else 5
    except (ValueError, IndexError):
        requested = 5

    api_key = config.openrouter_api_key
    if not api_key:
        await update.message.reply_text(
            "Нет OpenRouter ключа. Добавь через /setkey openrouter_api_key sk-or-..."
        )
        return

    status = await update.message.reply_text("🧠 Думаю...")

    try:
        local_results, _total = await scan_overbought(client, 65.0, 10.0)
    except Exception as e:
        logger.warning("Local scan failed: %s", e)
        local_results = []

    research_snapshot = await build_research_snapshot(
        client,
        config,
        local_results,
        max_candidates=max(12, requested * 4),
    )
    open_positions = research_snapshot.get("positions", [])
    free_usdt = float((research_snapshot.get("balance") or {}).get("free_usdt") or 0.0)

    ask_n = requested
    model = normalize_openrouter_model(
        getattr(config, "openrouter_model", DEFAULT_MODEL),
        force_default=True,
    )
    await status.edit_text(f"🔍 Анализирую через {model}...")

    ai_result = await deep_short_analysis(
        local_results,
        api_key,
        model=model,
        n=ask_n,
        mode="both",
        research_snapshot=research_snapshot,
    )

    if ai_result.error and not ai_result.text:
        await status.edit_text(f"❌ AI недоступен: {ai_result.error}")
        return

    picks = parse_analyst_blocks(ai_result.text, n=ask_n * 2)
    if not picks:
        logger.warning("AI scan response unusable from %s: %s", ai_result.model, ai_result.text[:1200])
        await status.edit_text(
            f"📝 GPT-5.5 не вернул нужный формат COIN/SIDE.\n"
            f"{format_usage_footer(ai_result)}",
            parse_mode="Markdown",
        )
        return

    await status.edit_text(f"✅ AI выдал {len(picks)} монет. Проверяю рынок биржи...")

    open_coins = {p["symbol"].split("/")[0] for p in open_positions}
    open_pos_by_coin = {p["symbol"].split("/")[0]: p for p in open_positions}

    validated: list[dict] = []
    existing_picks: list[tuple[str, dict]] = []  # (fut_sym, pick) for already-open coins
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()
    side_counts = {"long": 0, "short": 0}

    for pick in picks:
        ticker = pick["ticker"].upper()
        if ticker in seen:
            continue
        seen.add(ticker)

        fut_sym = await mexc_find_futures_symbol(client, ticker)
        if not fut_sym:
            skipped.append((ticker, "нет на бирже"))
            continue

        coin = fut_sym.split("/")[0]
        if coin in open_coins:
            existing_picks.append((fut_sym, pick))
            continue

        tech = await analyze_single_coin(client, fut_sym)
        if not tech:
            skipped.append((ticker, "нет OHLCV"))
            continue
        ai_side = pick.get("side")
        if ai_side in ("long", "short"):
            tech["direction"] = ai_side
        direction = tech.get("direction", "short")
        if direction in side_counts and side_counts[direction] >= requested:
            continue
        tech["_ai_fund"] = pick.get("fund", "")
        tech["_ai_funding"] = pick.get("funding", "")
        tech["_ai_risk"] = pick.get("risk", "")
        tech["_ai_pick"] = pick
        validated.append(tech)
        if direction in side_counts:
            side_counts[direction] += 1

    try:
        await status.delete()
    except Exception:
        pass

    if not validated and not existing_picks:
        summary = ", ".join(f"{t} ({r})" for t, r in skipped[:5])
        await update.message.reply_text(f"Ничего не прошло проверку биржи.\nПропущено: {summary}")
        return

    default_bet = float(getattr(config, "default_trade_usdt", 0.20))
    averaging_amount = float(getattr(config, "averaging_amount", 0.10))
    free_balance = free_usdt
    budget_info = _check_budget(free_balance, default_bet, config)

    # Compute leverage and averaging-risk for each validated coin
    from bot.handlers.trading import _max_leverage_by_vol
    for tech in validated:
        sym = tech["symbol"]
        try:
            sym_max = await client.get_max_leverage(sym)
        except Exception:
            sym_max = 100
        # Apply the same vol-cap that execute_open uses, so displayed leverage is accurate
        try:
            ticker_lev = await client.get_ticker(sym)
            vol_24h = float(ticker_lev.get("quoteVolume") or ticker_lev.get("baseVolume") or 0)
            vol_cap = _max_leverage_by_vol(vol_24h)
            sym_max = min(sym_max, vol_cap)
        except Exception:
            pass
        user_lev = int(getattr(config, "default_leverage", 0) or 0)
        lev_eff = min(user_lev, sym_max) if user_lev > 0 else sym_max
        tech["_lev_eff"] = lev_eff
        min_avg = await _get_live_min_avg_margin(client, sym, lev_eff, context.bot_data)
        tech["_min_avg"] = min_avg
        tech["_avg_ok"] = _can_avg_at_configured(sym, lev_eff, averaging_amount, context.bot_data)

    # Sort: OK averaging first, risky (impossible to avg at configured amount) last
    validated.sort(key=lambda t: (0 if t["_avg_ok"] else 1))

    long_count = sum(1 for r in validated if r.get("direction") == "long")
    short_count = sum(1 for r in validated if r.get("direction") == "short")
    header = f"*🎯 AI top-{requested} long + top-{requested} short ({ai_result.model})*"
    header += f"\n_прошло проверку: LONG {long_count}, SHORT {short_count}_"
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
        pick = r.get("_ai_pick", {})
        if pick:
            card += (
                f"\n   TP1 `{pick.get('tp1', '—')}` | TP2 `{pick.get('tp2', '—')}` | "
                f"TP3 `{pick.get('tp3', '—')}` | SL `{pick.get('sl', '—')}`"
            )

        icon = "🔻" if direction == "short" else "🔺"
        scan_id = uuid.uuid4().hex[:12]
        context.user_data.setdefault("scan_picks", {})[scan_id] = {
            "symbol": sym,
            "side": side_code,
            "direction": direction,
            "margin": default_bet,
            "leverage": lev_eff,
            "pick": pick,
            "min_avg": min_avg,
        }

        if avg_ok:
            btn = InlineKeyboardButton(
                f"{icon} ${default_bet:g} · {lev_eff}x",
                callback_data=f"scan_preview_{scan_id}",
            )
        else:
            # Averaging minimum exceeds configured amount — show warning
            card += (
                f"\n   ⚠️ *Мин. докупка биржи* `${min_avg:.2f}` > настройка `${averaging_amount:.2f}`"
                f"\n   При открытии докупка будет по `${min_avg:.2f}`"
            )
            btn = InlineKeyboardButton(
                f"⚠️ Открыть (докупка ~${min_avg:.2f})",
                callback_data=f"scan_preview_{scan_id}",
            )

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

            min_avg = await _get_live_min_avg_margin(client, fut_sym, lev, context.bot_data)
            step = max(averaging_amount, min_avg)
            step_note = f" (мин биржи)" if min_avg > averaging_amount else ""

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
                           margin: float, leverage: int,
                           pick: dict | None = None,
                           exit_mode_override: str | None = None):
    """Shared open logic used by open_callback and open_confirm_callback."""
    coin = symbol.split("/")[0]
    side_ru = "SHORT" if side == "sell" else "LONG"
    await q.message.reply_text(f"🚀 Открываю {side_ru} `{coin}`...", parse_mode="Markdown")
    from bot.handlers.trading import execute_open
    result = await execute_open(
        client, app, symbol, side, margin, leverage,
        pick=pick, exit_mode_override=exit_mode_override,
    )
    actual_margin = result.get("margin", margin)
    actual_lev = result["leverage"]
    icon = "🔻" if side == "sell" else "🟩"
    lines = [
        f"*{coin}* {icon}×{actual_lev} `${actual_margin:.2f}`",
        f"▶ Entry: `{result['entry_price']:.6g}`",
    ]
    if actual_margin > margin + 0.001:
        lines.append(f"⚠️ Маржа поднята `${margin:.2f}` → `${actual_margin:.2f}` (мин биржи)")
    if result.get("liquidation_price"):
        lines.append(f"💀 Liq: `{result['liquidation_price']:.6g}`")
    if pick:
        try:
            plan = plan_from_pick(
                symbol=symbol,
                side=side,
                entry=float(result["entry_price"]),
                reference=float(result["entry_price"]),
                leverage=int(actual_lev),
                margin=float(actual_margin),
                pick=pick,
                config=app.bot_data.get("config") if app is not None else None,
            )
            opened = format_three_tp_plan(plan, title="Opened position")
            extras = []
            if actual_margin > margin + 0.001:
                extras.append(f"Margin upgraded `${margin:.2f}` -> `${actual_margin:.2f}`")
            if result.get("liquidation_price"):
                extras.append(f"Liq: `{result['liquidation_price']:.6g}`")
            if extras:
                opened += "\n" + "\n".join(extras)
            await q.message.reply_text(opened, parse_mode="Markdown")
            return
        except Exception:
            pass
    if result.get("tp_price"):
        lines.append(f"✅ TP: `{result['tp_price']:.6g}`")
    if result.get("sl_price"):
        lines.append(f"🛑 SL: `{result['sl_price']:.6g}`")
    await q.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _live_reference_price(client, symbol: str, fallback: float = 0.0) -> float:
    try:
        ticker = await client.get_ticker(symbol)
        return float(ticker.get("last") or ticker.get("close") or ticker.get("price") or fallback)
    except Exception:
        return float(fallback or 0)


async def _scan_plan_from_payload(client, config, payload: dict):
    symbol = payload["symbol"]
    pick = payload.get("pick") or {}
    entry = await _live_reference_price(client, symbol, parse_price(pick.get("price") or pick.get("entry") or "0"))
    if entry <= 0:
        entry = parse_price(pick.get("entry") or "0")
    return plan_from_pick(
        symbol=symbol,
        side=payload.get("direction") or payload.get("side"),
        entry=entry,
        reference=entry,
        leverage=int(payload.get("leverage") or 1),
        margin=float(payload.get("margin") or 0),
        pick=pick,
        config=config,
    )


async def scan_preview_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    scan_id = (q.data or "").replace("scan_preview_", "", 1)
    payload = context.user_data.get("scan_picks", {}).get(scan_id)
    if not payload:
        await q.message.reply_text("Scan idea expired. Run /scan again.")
        return

    client = context.bot_data["exchange"]
    config = context.bot_data.get("config")
    try:
        plan = await _scan_plan_from_payload(client, config, payload)
    except Exception as e:
        await q.message.reply_text(f"Cannot build 3TP preview: {e}")
        return
    payload["pick"] = pick_from_plan(plan)
    payload["preview_fingerprint"] = plan_fingerprint(plan)
    text = format_three_tp_plan(plan, title="Open preview")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Confirm open", callback_data=f"scan_confirm_{scan_id}")]])
    await q.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def scan_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    scan_id = (q.data or "").replace("scan_confirm_", "", 1)
    payload = context.user_data.get("scan_picks", {}).get(scan_id)
    if not payload:
        await q.message.reply_text("Scan idea expired. Run /scan again.")
        return
    client = context.bot_data["exchange"]
    config = context.bot_data.get("config")
    try:
        plan = await _scan_plan_from_payload(client, config, payload)
    except Exception as e:
        await q.message.reply_text(f"Cannot open 3TP plan: {e}")
        return
    payload["pick"] = pick_from_plan(plan)
    payload["preview_fingerprint"] = plan_fingerprint(plan)
    try:
        averaging_amount = float(getattr(config, "averaging_amount", 0.10)) if config else 0.10
        min_avg = float(payload.get("min_avg") or 0)
        if min_avg > averaging_amount:
            min_notional = min_avg * max(int(payload["leverage"]), 1) / 1.05
            context.bot_data.setdefault("_min_order_cache", {})[payload["symbol"]] = min_notional
            try:
                from bot import db as db_mod
                db_mod.set_min_order_notional(payload["symbol"], min_notional)
            except Exception:
                pass
    except Exception:
        pass
    try:
        await _do_execute_open(
            q,
            client,
            context.application,
            payload["symbol"],
            payload["side"],
            float(payload["margin"]),
            int(payload["leverage"]),
            pick=payload["pick"],
            exit_mode_override="ladder",
        )
    except Exception as e:
        from bot.services.exchange_errors import format_open_error
        side_name = "short" if payload["side"] == "sell" else "long"
        await q.message.reply_text(
            format_open_error(e, symbol=payload["symbol"], side=side_name),
            parse_mode="Markdown",
        )


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
    _min_avg_open = await _get_live_min_avg_margin(client, symbol, _lev_for_chk, context.bot_data)
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
        await q.message.reply_text(
            f"⚠️ *{coin}* {side_ru}: недостаточный бюджет\n"
            f"Свободно `${free:.2f}` · нужно `${budget['full_budget']:.2f}` на 1 поз\n"
            f"_(маржа+докупки `${budget['base_budget']:.2f}` × SL {budget['sl_pct']:.0f}%)_\n"
            f"Хватит на `{budget['positions_possible']}` полных позиций. Открыть всё равно?",
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

    try:
        await _do_execute_open(q, client, context.application, symbol, side, margin, leverage)
    except Exception as e:
        from bot.services.exchange_errors import format_open_error
        side_name = "short" if side == "sell" else "long"
        await q.message.reply_text(
            format_open_error(e, symbol=symbol, side=side_name),
            parse_mode="Markdown",
        )


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
        from bot.services.exchange_errors import format_open_error
        side_name = "short" if side == "sell" else "long"
        await q.message.reply_text(
            format_open_error(e, symbol=symbol, side=side_name),
            parse_mode="Markdown",
        )


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
        f"🚀 Открываю {side_ru} `{coin}`...",
        parse_mode="Markdown",
    )

    try:
        from bot.handlers.trading import execute_open
        result = await execute_open(client, context.application, symbol, side, margin, leverage)
        actual_margin = result.get("margin", margin)
        actual_lev = result["leverage"]
        icon = "🔻" if side == "sell" else "🟩"
        lines = [
            f"*{coin}* {icon}×{actual_lev} `${actual_margin:.2f}`",
            f"▶ Entry: `{result['entry_price']:.6g}`",
        ]
        if actual_margin > margin + 0.001:
            lines.append(f"⚠️ Маржа поднята `${margin:.2f}` → `${actual_margin:.2f}` (мин биржи)")
        if result.get("liquidation_price"):
            lines.append(f"💀 Liq: `{result['liquidation_price']:.6g}`")
        if result.get("tp_price"):
            lines.append(f"✅ TP: `{result['tp_price']:.6g}`")
        if result.get("sl_price"):
            lines.append(f"🛑 SL: `{result['sl_price']:.6g}`")
        lines.append(f"📊 Автодокупка: `${min_avg:.2f}`/шаг")
        await q.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        from bot.services.exchange_errors import format_open_error
        side_name = "short" if side == "sell" else "long"
        await q.message.reply_text(
            format_open_error(e, symbol=symbol, side=side_name),
            parse_mode="Markdown",
        )


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
    """Handles avg_force_{symbol} — force-average at exchange minimum after auto-avg failed."""
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
        f"⏳ Принудительная докупка `{coin}` +`${min_avg:.2f}` (мин биржи)...",
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

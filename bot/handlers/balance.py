"""/balance — полный баланс с деталями по каждой позиции."""
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from bot.fmt import fmt_usd

logger = logging.getLogger(__name__)
_SEP = "─" * 20


def _build_balance_text(futures_raw: dict, positions: list[dict],
                        tp_sl_pcts: dict, db_map: dict, re_map: dict,
                        config, daily_stats: dict,
                        lev_cache: dict | None = None,
                        spot_raw: dict | None = None) -> str:
    from bot.pos_format import format_position_block

    free = float(futures_raw.get("free", {}).get("USDT", 0) or 0)
    total = float(futures_raw.get("total", {}).get("USDT", 0) or 0)
    raw = futures_raw.get("_raw", {})
    avail_open = float(raw.get("availableOpen", raw.get("availableBalance", free)) or free)

    total_pnl = sum(float(p.get("unrealized_pnl", 0)) for p in positions)

    # Header: just position summary
    if positions:
        word = "зарабатываем" if total_pnl >= 0 else "теряем"
        lines = [f"*💰 Баланс · Позиции ({len(positions)}) — {word} `{fmt_usd(total_pnl)}`*"]
    else:
        lines = ["*Баланс💰*", "_Нет открытых позиций_"]

    lev_cache = lev_cache or {}

    # Per-position blocks
    for pos in positions:
        lines.append(_SEP)
        symbol = pos["symbol"]
        cached = lev_cache.get(symbol, {})
        block = format_position_block(
            pos,
            db_rec=db_map.get(symbol),
            re_rec=re_map.get(symbol),
            config=config,
            tp_sl_pcts=tp_sl_pcts,
            max_lev=cached.get("max_lev", 0),
            max_pos_usdt=cached.get("max_pos_usdt", 0),
        )
        lines.append(block)

    # Balance summary — shown after positions, before margin block
    lines.append(_SEP)
    spot_free = 0.0
    if spot_raw:
        spot_free = float((spot_raw.get("free") or {}).get("USDT", 0) or 0)
    bal_lines = [
        f"💵 Фьючерсы: `${total:.2f}` · Свободно: `${free:.2f}` · Avail: `${avail_open:.2f}`"
    ]
    if spot_raw is not None:
        bal_lines.append(f"💳 Спот: `${spot_free:.2f}`")
    if daily_stats:
        day_pnl = float(daily_stats.get("pnl", 0) or 0)
        day_trades = int(daily_stats.get("trades", 0) or 0)
        icon = "📈" if day_pnl >= 0 else "📉"
        bal_lines.append(f"{icon} Сегодня: `{fmt_usd(day_pnl)}` ({day_trades} сд.)")
    lines.append("\n".join(bal_lines))

    # Margin requirements block — shown below balance, before buttons
    if positions and config:
        avg_amount = float(getattr(config, "averaging_amount", 0.10))
        avg_budget = float(getattr(config, "averaging_budget", 5.00))
        sl_pct = float(getattr(config, "sl_pct", 500))
        max_avg_count = int(getattr(config, "max_averaging_count", 100))
        profit_lock_trigger = float(getattr(config, "averaging_profit_lock_trigger", 0))
        min_order_cache: dict = {}
        try:
            from bot import db as _db_bal
            min_order_cache = _db_bal.get_min_order_cache()
        except Exception:
            pass

        invested_total = 0.0
        remaining_avg_total = 0.0
        avg_violations: list[str] = []
        for pos in positions:
            sym = pos["symbol"]
            db_rec = db_map.get(sym, {}) or {}
            pos_invested = float(db_rec.get("total_invested") or pos.get("margin") or 0)
            avg_count = int(db_rec.get("averaging_count") or 0)
            remaining_steps = max(0, max_avg_count - avg_count)
            pos_lev = int(pos.get("leverage") or 1)
            min_notional = min_order_cache.get(sym, 0)
            eff_avg = (max(avg_amount, min_notional / max(pos_lev, 1) * 1.05)
                       if min_notional > 0 else avg_amount)
            remaining_avg = remaining_steps * eff_avg
            if eff_avg > avg_amount + 0.001:
                avg_violations.append(f"{sym.split('/')[0]} `${eff_avg:.2f}`")
            invested_total += pos_invested
            remaining_avg_total += remaining_avg

        base_total = invested_total + remaining_avg_total
        if profit_lock_trigger > 0:
            risk_total = base_total
            sl_label = f"profit-lock SL {profit_lock_trigger:.0f}%"
        else:
            risk_total = base_total * (sl_pct / 100.0)
            sl_label = f"SL {sl_pct:.0f}%"

        deficit = risk_total - (free + invested_total)
        if deficit > 0:
            budget_status = f"⚠️ дефицит `${deficit:.2f}`"
        else:
            budget_status = f"✅ запас `${-deficit:.2f}`"

        lines.append(_SEP)
        lines.append(
            f"📊 *Требуется маржи ({sl_label}):*\n"
            f"Вложено: `${invested_total:.2f}` · Докупок ост.: `${remaining_avg_total:.2f}`\n"
            f"Итого риск: `${risk_total:.2f}` · {budget_status}"
        )
        if avg_violations:
            lines.append(
                f"⚠️ Мин. докупка > `${avg_amount:.2f}`: " + ", ".join(avg_violations)
            )

    return "\n".join(lines)


def _build_close_kb(positions: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    from bot.fmt import fmt_pct, fmt_usd
    for i, pos in enumerate(positions, 1):
        coin = pos["symbol"].split("/")[0]
        pnl = float(pos.get("unrealized_pnl", 0))
        pct = float(pos.get("percentage", 0))
        icon = "✅" if pnl >= 0 else "🔻"
        rows.append([InlineKeyboardButton(
            f"{i}. {icon} {coin}  {fmt_pct(pct)}  {fmt_usd(pnl)}",
            callback_data=f"bal_close_{pos['symbol']}"
        )])
    rows.append([
        InlineKeyboardButton("🔄 Обновить", callback_data="balance_refresh"),
        InlineKeyboardButton("💱 Перевести", callback_data="transfer_start"),
    ])
    rows.append([InlineKeyboardButton("📊 Позиции", callback_data="positions_show")])
    return InlineKeyboardMarkup(rows)


async def _fetch_lev_cache(client, positions: list[dict]) -> dict:
    """Fetch max_lev and max_pos_usdt per symbol (best-effort, errors ignored)."""
    cache = {}
    for pos in positions:
        symbol = pos["symbol"]
        lev = int(pos.get("leverage", 1))
        try:
            max_lev = await client.get_max_leverage(symbol)
            max_pos = await client.get_position_limit_usdt(symbol, lev)
            cache[symbol] = {"max_lev": max_lev, "max_pos_usdt": max_pos}
        except Exception:
            pass
    return cache


async def _fetch_all(client, context):
    from bot import db as db_mod
    from datetime import date

    futures_bal, spot_bal, positions = await asyncio.gather(
        client.get_futures_balance(),
        client.get_spot_balance(),
        client.get_positions(),
        return_exceptions=True,
    )
    if isinstance(futures_bal, Exception):
        raise futures_bal
    if isinstance(positions, Exception):
        raise positions
    if isinstance(spot_bal, Exception):
        spot_bal = None

    db_recs = {r["symbol"]: r for r in db_mod.get_open_positions()}
    re_recs = {r["symbol"]: r for r in db_mod.get_all_reentry()}
    config = context.bot_data.get("config")
    tp_sl_pcts = context.bot_data.get("tp_sl_pcts", {})
    daily_stats = db_mod.get_daily_stats(date.today().isoformat())
    lev_cache = await _fetch_lev_cache(client, positions)

    return futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache, spot_bal


async def balance_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.bot_data["exchange"]
    try:
        futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache, spot_bal = \
            await _fetch_all(client, context)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return

    text = _build_balance_text(futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache, spot_bal)
    kb = _build_close_kb(positions)
    # Split if Telegram limit exceeded (4096 chars)
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for i, chunk in enumerate(chunks):
        try:
            await update.message.reply_text(
                chunk, parse_mode="Markdown",
                reply_markup=kb if i == len(chunks) - 1 else None
            )
        except Exception:
            await update.message.reply_text(
                chunk,
                reply_markup=kb if i == len(chunks) - 1 else None
            )


async def balance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data.startswith("bal_close_confirm_"):
        symbol = q.data[len("bal_close_confirm_"):]
        coin = symbol.split("/")[0]
        client = context.bot_data["exchange"]
        try:
            await q.edit_message_text(f"⏳ Закрываю `{coin}`...", parse_mode="Markdown")
            pos = await client.get_position(symbol)
            pnl = float(pos.get("unrealized_pnl", 0)) if pos else 0.0
            margin = float(pos.get("margin", 0)) if pos else 0.0
            exit_price = float(pos.get("mark_price", 0)) if pos else 0.0
            await client.cancel_tp_sl_orders(symbol)
            await client.close_futures_position(symbol)
            from bot import db as db_mod
            db_mod.close_position(symbol)
            db_mod.delete_reentry(symbol)
            db_mod.log_trade(symbol, "close", amount=margin, pnl=pnl, note="manual")
            db_mod.close_position_history(symbol, exit_price, pnl, "manual")
            await q.edit_message_text(f"✅ *{coin}* закрыт.", parse_mode="Markdown")
        except Exception as e:
            await q.edit_message_text(f"❌ Ошибка закрытия {coin}: {e}")
        return

    if q.data == "bal_close_cancel":
        await q.answer("Отменено")
        await q.delete_message()
        return

    if q.data.startswith("bal_close_"):
        symbol = q.data[len("bal_close_"):]
        coin = symbol.split("/")[0]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Да, закрыть {coin}", callback_data=f"bal_close_confirm_{symbol}")],
            [InlineKeyboardButton("◀ Отмена", callback_data="bal_close_cancel")],
        ])
        await q.message.reply_text(
            f"⚠️ Закрыть *{coin}* по рынку?",
            parse_mode="Markdown", reply_markup=kb,
        )
        return

    if q.data in ("balance_refresh", "balance_futures"):
        client = context.bot_data["exchange"]
        try:
            futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache, spot_bal = \
                await _fetch_all(client, context)
        except Exception as e:
            await q.answer(f"Ошибка: {e}", show_alert=True)
            return
        text = _build_balance_text(futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache, spot_bal)
        kb = _build_close_kb(positions)
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        try:
            await q.edit_message_text(chunks[0], parse_mode="Markdown",
                                      reply_markup=kb if len(chunks) == 1 else None)
        except Exception:
            try:
                await q.edit_message_text(chunks[0],
                                          reply_markup=kb if len(chunks) == 1 else None)
            except Exception:
                pass
        for chunk in chunks[1:]:
            try:
                await q.message.reply_text(chunk, parse_mode="Markdown",
                                           reply_markup=kb)
            except Exception:
                await q.message.reply_text(chunk, reply_markup=kb)
        return

    if q.data == "positions_show":
        from bot.handlers.positions import _send_positions
        await _send_positions(q.message, context, edit=False)

    if q.data == "transfer_start":
        client = context.bot_data["exchange"]
        try:
            futures_bal, spot_bal = await asyncio.gather(
                client.get_futures_balance(),
                client.get_spot_balance(),
                return_exceptions=True,
            )
            fut_free = float(futures_bal.get("free", {}).get("USDT", 0) or 0) if not isinstance(futures_bal, Exception) else 0.0
            spot_free = float((spot_bal.get("free") or {}).get("USDT", 0) or 0) if not isinstance(spot_bal, Exception) else 0.0
        except Exception:
            fut_free = spot_free = 0.0
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"Спот→Фьючи (${spot_free:.2f})", callback_data="transfer_dir_s2f"),
            InlineKeyboardButton(f"Фьючи→Спот (${fut_free:.2f})", callback_data="transfer_dir_f2s"),
        ], [
            InlineKeyboardButton("✖ Отмена", callback_data="balance_refresh"),
        ]])
        await q.edit_message_text("💱 *Перевод USDT*\nВыбери направление:", parse_mode="Markdown", reply_markup=kb)
        return

    if q.data in ("transfer_dir_s2f", "transfer_dir_f2s"):
        direction = q.data.replace("transfer_dir_", "")
        client = context.bot_data["exchange"]
        try:
            if direction == "s2f":
                bal = await client.get_spot_balance()
                avail = float((bal.get("free") or {}).get("USDT", 0) or 0)
                label = "Спот → Фьючерсы"
            else:
                bal = await client.get_futures_balance()
                raw = bal.get("_raw", {})
                # transferable = min(availableBalance, cashBalance) — MEXC reserves maintenance margin
                cash = float(raw.get("cashBalance", raw.get("availableBalance", 0)) or 0)
                avail_raw = float(bal.get("free", {}).get("USDT", 0) or 0)
                avail = min(avail_raw, cash)
                # floor to 2 decimals to avoid MEXC exact-boundary rejection
                import math
                avail = math.floor(avail * 100) / 100
                label = "Фьючерсы → Спот"
        except Exception:
            avail = 0.0
            label = "Спот → Фьючерсы" if direction == "s2f" else "Фьючерсы → Спот"
        context.user_data["pending_transfer"] = {"dir": direction, "avail": avail}
        await q.edit_message_text(
            f"💱 *{label}*\nДоступно: `${avail:.2f}`\n\nВведи сумму USDT:",
            parse_mode="Markdown",
        )
        return

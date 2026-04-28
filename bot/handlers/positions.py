"""/positions — список с эмодзи-кнопками, детальный вид, закрытие."""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import ContextTypes
from bot.fmt import fmt_pct, fmt_usd

logger = logging.getLogger(__name__)

# Numbered emoji 1️⃣–9️⃣
_NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def _pos_emoji(pos: dict) -> str:
    """Main emoji: side + health."""
    side = pos.get("side", "")
    pct = float(pos.get("percentage", 0))
    liq = float(pos.get("liquidation_price", 0))
    mark = float(pos.get("mark_price", 0))

    # Liquidation proximity
    if liq > 0 and mark > 0:
        dist_pct = abs(mark - liq) / mark * 100
        if dist_pct < 3:
            return "💀"
        if dist_pct < 10:
            return "⚠️"

    if side == "short":
        if pct >= 200:
            return "🔥"
        if pct > 0:
            return "✅"
        return "🔻"
    else:
        if pct >= 200:
            return "🔥"
        if pct > 0:
            return "🟩"
        return "🔺"


def _format_pos_line(pos: dict, num: int) -> str:
    """One-line position summary for the list."""
    num_e = _NUM_EMOJI[num - 1] if num <= len(_NUM_EMOJI) else f"{num}."
    emoji = _pos_emoji(pos)
    coin = pos["symbol"].split("/")[0]
    lev = int(pos.get("leverage", 1))
    pct = float(pos.get("percentage", 0))
    pnl = float(pos.get("unrealized_pnl", 0))
    return f"{num_e} {emoji}{coin}×{lev} ({fmt_pct(pct)}) {fmt_usd(pnl)}"


def _format_pos_detail(pos: dict, extra: dict | None = None) -> str:
    """Full detail card for a single position."""
    extra = extra or {}
    coin = pos["symbol"].split("/")[0]
    emoji = _pos_emoji(pos)
    side = pos.get("side", "")
    side_ru = "SHORT 🔻" if side == "short" else "LONG 🟩"
    lev = int(pos.get("leverage", 1))
    pct = float(pos.get("percentage", 0))
    pnl = float(pos.get("unrealized_pnl", 0))
    entry = float(pos.get("entry_price", 0))
    mark = float(pos.get("mark_price", 0))
    liq = float(pos.get("liquidation_price", 0))
    margin = float(pos.get("margin", 0))
    margin_mode = pos.get("margin_mode", "")

    lines = [
        f"*{emoji} {coin}* — {side_ru} ×{lev}",
        f"PnL: `{fmt_pct(pct)}` ({fmt_usd(pnl)})",
        f"Entry: `{entry:.6g}` | Mark: `{mark:.6g}`",
    ]
    if liq > 0:
        dist = abs(mark - liq) / mark * 100 if mark > 0 else 0
        liq_emoji = "💀" if dist < 3 else ("⚠️" if dist < 10 else "📍")
        lines.append(f"{liq_emoji} Liq: `{liq:.6g}` ({dist:.1f}% до ликв.)")
    lines.append(f"Маржа: `${margin:.4f}` ({margin_mode})")

    # TP/SL
    tp_pct = extra.get("tp_pct")
    sl_pct = extra.get("sl_pct")
    if tp_pct or sl_pct:
        tp_s = f"+{tp_pct:.0f}%" if tp_pct else "—"
        sl_s = f"-{sl_pct:.0f}%" if sl_pct else "—"
        lines.append(f"TP: `{tp_s}` | SL: `{sl_s}`")

    # Max leverage / max position
    max_lev = extra.get("max_lev")
    max_usdt = extra.get("max_usdt")
    if max_lev:
        lev_info = f"Макс плечо: `×{max_lev}`"
        if max_usdt:
            lev_info += f" | Лимит позиции: `~${max_usdt:,.0f}`"
        lines.append(lev_info)

    # Averaging progress
    avg_count = extra.get("avg_count")
    max_avg = extra.get("max_avg")
    total_inv = extra.get("total_invested")
    budget = extra.get("budget")
    if avg_count is not None and max_avg:
        avg_s = f"Докупок: `{avg_count}/{max_avg}`"
        if total_inv is not None and budget:
            avg_s += f" | `${total_inv:.2f}/${budget:.2f}`"
        lines.append(avg_s)

    # Re-entry progress
    reentry_count = extra.get("reentry_count")
    max_reentry = extra.get("max_reentry")
    if reentry_count is not None and max_reentry:
        lines.append(f"Перезаходов: `{reentry_count}/{max_reentry}`")

    return "\n".join(lines)


async def positions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_positions(update.message, context, edit=False)


_SEP = "─" * 20


async def _send_positions(message: Message, context: ContextTypes.DEFAULT_TYPE,
                          edit: bool = False):
    from bot import db as db_mod
    from bot.pos_format import format_position_block

    client = context.bot_data["exchange"]
    try:
        positions = await client.get_positions()
    except Exception as e:
        text = f"❌ Ошибка получения позиций: {e}"
        if edit:
            await message.edit_text(text)
        else:
            await message.reply_text(text)
        return

    if not positions:
        text = "📭 Нет открытых позиций."
        if edit:
            await message.edit_text(text)
        else:
            await message.reply_text(text)
        return

    db_map = {r["symbol"]: r for r in db_mod.get_open_positions()}
    re_map = {r["symbol"]: r for r in db_mod.get_all_reentry()}
    config = context.bot_data.get("config")
    tp_sl_pcts = context.bot_data.get("tp_sl_pcts", {})

    # Fetch max_lev / pos limit / funding per symbol in parallel (best-effort)
    import asyncio as _asyncio
    lev_cache: dict = {}
    funding_cache: dict = {}

    async def _fetch_sym_data(pos):
        sym = pos["symbol"]
        lev = int(pos.get("leverage", 1))
        try:
            ml, mp = await _asyncio.gather(
                client.get_max_leverage(sym),
                client.get_position_limit_usdt(sym, lev),
            )
            lev_cache[sym] = {"max_lev": ml, "max_pos_usdt": mp}
        except Exception:
            pass
        try:
            fr = await client.get_funding_rate(sym)
            funding_cache[sym] = fr
        except Exception:
            pass

    await _asyncio.gather(*[_fetch_sym_data(p) for p in positions])

    total_pnl = sum(float(p.get("unrealized_pnl", 0)) for p in positions)
    word = "зарабатываем" if total_pnl >= 0 else "теряем"
    lines = [f"*📊 Позиции ({len(positions)}) — {word} `{fmt_usd(total_pnl)}`*"]

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
            funding_rate=funding_cache.get(symbol, {}).get("rate", 0.0),
            funding_next_ts=funding_cache.get(symbol, {}).get("next_funding_time"),
        )
        lines.append(block)

    # Inline buttons: one per row with close
    btn_rows = []
    total = len(positions)
    for i, pos in enumerate(positions, 1):
        coin = pos["symbol"].split("/")[0]
        pnl = float(pos.get("unrealized_pnl", 0))
        pct = float(pos.get("percentage", 0))
        icon = "✅" if pnl >= 0 else "🔻"
        label = f"{i}. {icon} {coin}  {fmt_pct(pct)}  {fmt_usd(pnl)}"
        btn_rows.append([InlineKeyboardButton(label, callback_data=f"pos_close_{pos['symbol']}")])
    btn_rows.append([InlineKeyboardButton("🔄 Обновить", callback_data="positions_refresh")])

    kb = InlineKeyboardMarkup(btn_rows)
    text = "\n".join(lines)
    try:
        if edit:
            await message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
        else:
            await message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        if edit:
            await message.edit_text(text, reply_markup=kb)
        else:
            await message.reply_text(text, reply_markup=kb)


async def positions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "positions_refresh":
        await _send_positions(q.message, context, edit=True)
        return

    if data.startswith("pos_detail_"):
        symbol = data.replace("pos_detail_", "")
        client = context.bot_data["exchange"]
        try:
            pos = await client.get_position(symbol)
        except Exception as e:
            await q.answer(f"Ошибка: {e}", show_alert=True)
            return
        if not pos:
            await q.answer("Позиция не найдена.", show_alert=True)
            return

        # Collect extra info
        from bot import db as db_mod
        lev = int(pos.get("leverage", 1))
        extra: dict = {}
        tp_sl_pcts: dict = context.bot_data.get("tp_sl_pcts", {})
        stored = tp_sl_pcts.get(symbol, {})
        db_rec = db_mod.get_open_position(symbol)
        extra["tp_pct"] = stored.get("tp_pct") or (db_rec.get("tp_pct") if db_rec else None)
        extra["sl_pct"] = stored.get("sl_pct") or (db_rec.get("sl_pct") if db_rec else None)
        try:
            extra["max_lev"] = await client.get_max_leverage(symbol)
            extra["max_usdt"] = await client.get_position_limit_usdt(symbol, lev)
        except Exception:
            pass
        if db_rec:
            extra["avg_count"] = db_rec.get("averaging_count", 0)
            extra["total_invested"] = db_rec.get("total_invested", 0)
            extra["budget"] = db_rec.get("averaging_budget", 0)
        config = context.bot_data.get("config")
        if config:
            extra["max_avg"] = config.max_averaging_count
        re_rec = db_mod.get_reentry(symbol)
        if re_rec:
            extra["reentry_count"] = re_rec.get("cycle_count", 0)
            extra["max_reentry"] = re_rec.get("max_cycles", 3)

        detail = _format_pos_detail(pos, extra)
        coin = symbol.split("/")[0]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Закрыть", callback_data=f"pos_close_{symbol}")],
            [InlineKeyboardButton("◀ Назад", callback_data="positions_refresh")],
        ])
        try:
            await q.edit_message_text(detail, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            await q.edit_message_text(detail, reply_markup=kb)
        return

    if data.startswith("pos_close_"):
        symbol = data.replace("pos_close_", "")
        client = context.bot_data["exchange"]
        coin = symbol.split("/")[0]
        # Confirm close
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, закрыть", callback_data=f"pos_close_confirm_{symbol}"),
            InlineKeyboardButton("◀ Отмена", callback_data=f"pos_detail_{symbol}"),
        ]])
        try:
            await q.edit_message_text(
                f"⚠️ Закрыть *{coin}* по рынку?",
                parse_mode="Markdown",
                reply_markup=kb,
            )
        except Exception:
            pass
        return

    if data.startswith("pos_close_confirm_"):
        symbol = data.replace("pos_close_confirm_", "")
        client = context.bot_data["exchange"]
        coin = symbol.split("/")[0]
        try:
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

"""/positions — список с эмодзи-кнопками, детальный вид, закрытие."""
import json
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path
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
        lines.append(f"Настройки: TP `{tp_s}` | SL `{sl_s}` (не подтверждение ордеров)")

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


async def _fetch_native_stop_orders(client) -> list[dict] | None:
    """Read active native TP/SL rows once; None means the read failed."""
    import asyncio
    try:
        rows = await asyncio.wait_for(client.get_native_stop_orders(), timeout=5)
        return rows if isinstance(rows, list) else None
    except Exception as error:
        logger.warning("Native TP/SL display unavailable: %s", error)
        return None


def _native_symbol(symbol: str) -> str:
    if "/" not in symbol:
        return symbol.split(":", 1)[0]
    base, quote = symbol.split("/", 1)
    return f"{base}_{quote.split(':', 1)[0]}"


def _native_row_matches(pos: dict, row: dict) -> bool:
    try:
        if pos.get("side") not in ("long", "short"):
            return False
        side_type = 1 if pos.get("side") == "long" else 2
        return (int(row.get("state")) == 1 and int(row.get("isFinished")) == 0
                and row.get("positionId") is not None
                and str(row.get("positionId")) == str(pos.get("position_id"))
                and row.get("symbol") == _native_symbol(pos["symbol"])
                and int(row.get("positionType")) == side_type)
    except (KeyError, TypeError, ValueError):
        return False


def _native_value(value) -> str:
    try:
        number = Decimal(str(value))
        return format(number, "f") if number.is_finite() and number > 0 else "нет данных"
    except (TypeError, ValueError, InvalidOperation):
        return "нет данных"


def _format_native_protection(pos: dict, rows: list[dict] | None) -> str:
    """Display exchange-native rows without attributing ownership or coverage."""
    if rows is None:
        return "🛡 Native TP/SL (последние 90 дней): native TP/SL данные недоступны"
    matches = [row for row in rows if isinstance(row, dict) and _native_row_matches(pos, row)]
    if not matches:
        return "🛡 Native TP/SL (последние 90 дней): активные native записи не найдены"

    lines = ["🛡 Native TP/SL (последние 90 дней):"]
    for row in matches:
        tp = row.get("takeProfitPrice")
        sl = row.get("stopLossPrice")
        tp_s = _native_value(tp) if tp not in (None, "", 0, "0") else "—"
        sl_s = _native_value(sl) if sl not in (None, "", 0, "0") else "—"
        native_id = row.get("id")
        if native_id in (None, ""):
            native_id = row.get("orderId")
        lines.append(f"TP `{tp_s}` | SL `{sl_s}` | id `{native_id}`")
        lines.append("Объём покрытия не подтверждён")
    return "\n".join(lines)


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

    native_orders = await _fetch_native_stop_orders(client)
    db_map = {p["symbol"]: r for p in positions if (r := db_mod.get_managed_position(p))}
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
        lines.append(block + "\n" + _format_native_protection(pos, native_orders))

    # Inline buttons: close + profit-lock toggle per position
    btn_rows = []
    for i, pos in enumerate(positions, 1):
        sym = pos["symbol"]
        coin = sym.split("/")[0]
        pnl = float(pos.get("unrealized_pnl", 0))
        pct = float(pos.get("percentage", 0))
        icon = "✅" if pnl >= 0 else "🔻"
        label = f"{i}. {icon} {coin}  {fmt_pct(pct)}  {fmt_usd(pnl)}"
        from bot import db as db_mod
        managed = db_mod.get_managed_position(pos)
        if not managed:
            lock_label = "Не под управлением"
        elif not managed["profit_lock_enabled"]:
            lock_label = "🔓 лок выкл"
        elif managed["profit_lock_step"]:
            lock_sl = managed["profit_lock_step"] - 50
            lock_label = f"🔒 SL+{lock_sl}%"
        else:
            lock_label = "🔒 лок"
        btn_rows.append([
            InlineKeyboardButton(label, callback_data=f"pos_close_{sym}"),
            InlineKeyboardButton(lock_label, callback_data=f"pos_plock_{sym}"),
        ])
    btn_rows.append([InlineKeyboardButton("🔄 Обновить", callback_data="positions_refresh")])

    kb = InlineKeyboardMarkup(btn_rows)
    text = "\n".join(lines)
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for idx, chunk in enumerate(chunks):
        is_last = idx == len(chunks) - 1
        try:
            if edit and idx == 0:
                await message.edit_text(chunk, parse_mode="Markdown",
                                        reply_markup=kb if is_last else None)
            else:
                await message.reply_text(chunk, parse_mode="Markdown",
                                         reply_markup=kb if is_last else None)
        except Exception:
            if edit and idx == 0:
                await message.edit_text(chunk, reply_markup=kb if is_last else None)
            else:
                await message.reply_text(chunk, reply_markup=kb if is_last else None)


async def positions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "positions_refresh":
        await _send_positions(q.message, context, edit=True)
        return

    if data.startswith("pos_plock_"):
        from bot import db as db_mod
        from bot.jobs.main import _calc_tp_price, _calc_sl_price
        symbol = data[len("pos_plock_"):]
        client = context.bot_data["exchange"]
        pos = await client.get_position(symbol)
        record = db_mod.get_managed_position(pos) if pos else None
        if not record:
            await q.message.reply_text("Позиция не принята под управление. /adopt SYMBOL")
            return
        enabled = not record["profit_lock_enabled"]
        with db_mod._connect() as conn:
            conn.execute("UPDATE positions SET profit_lock_enabled=? WHERE id=?", (int(enabled), record["id"]))
            if not enabled:
                conn.execute("UPDATE positions SET locked_sl=NULL,profit_lock_step=0 WHERE id=?", (record["id"],))
        try:
            entry, lev, side = pos["entry_price"], pos["leverage"], pos["side"]
            step = (int(pos["percentage"]) // 50) * 50 if enabled and pos["percentage"] >= 100 else None
            sl = _calc_tp_price(entry, lev, step - 50, side) if step else _calc_sl_price(entry, lev, record["sl_pct"], side)
            await client.set_tp_sl(symbol, _calc_tp_price(entry, lev, record["tp_pct"], side), sl,
                                   pos_data=pos, profit_lock_step=step)
            await q.message.reply_text(f"Profit-lock {'включён' if enabled else 'выключен'}; защита подтверждена.")
        except Exception as error:
            await q.message.reply_text(f"Настройка изменена; защита не подтверждена: {error}")
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
        db_rec = db_mod.get_managed_position(pos)
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

        native_orders = await _fetch_native_stop_orders(client)
        detail = _format_pos_detail(pos, extra) + "\n\n" + _format_native_protection(pos, native_orders)
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
        coin = symbol.split("/")[0]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 С перезаходом", callback_data=f"close_reentry_{symbol}"),
             InlineKeyboardButton("❌ Насовсем", callback_data=f"close_final_{symbol}")],
            [InlineKeyboardButton("◀ Отмена", callback_data="positions_refresh")],
        ])
        try:
            await q.edit_message_text(
                f"Закрыть `{coin}`?", parse_mode="Markdown", reply_markup=kb
            )
        except Exception:
            pass
        return

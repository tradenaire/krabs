"""/balance — полный баланс с деталями по каждой позиции."""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from bot.fmt import fmt_usd

logger = logging.getLogger(__name__)
_SEP = "─" * 20


def _build_balance_text(futures_raw: dict, positions: list[dict],
                        tp_sl_pcts: dict, db_map: dict, re_map: dict,
                        config, daily_stats: dict,
                        lev_cache: dict | None = None) -> str:
    from bot.pos_format import format_position_block

    free = float(futures_raw.get("free", {}).get("USDT", 0) or 0)
    total = float(futures_raw.get("total", {}).get("USDT", 0) or 0)
    raw = futures_raw.get("_raw", {})
    avail_open = float(raw.get("availableOpen", raw.get("availableBalance", free)) or free)

    lines = ["*Баланс💰*",
             f"Фьючерсы: `${total:.2f}`",
             f"└ Свободно: `${free:.2f}` · Avail: `${avail_open:.2f}`"]

    # Daily stats
    realized = float(daily_stats.get("realized_pnl", 0))
    if realized != 0:
        start_bal = total - realized
        if start_bal > 0:
            day_pct = realized / start_bal * 100
            sign = "+" if realized >= 0 else ""
            lines.append(f"За сегодня: `{sign}${realized:.2f}` ({sign}{day_pct:.1f}%) от `${start_bal:.2f}`")

    # Total unrealized
    total_pnl = sum(float(p.get("unrealized_pnl", 0)) for p in positions)
    if positions:
        word = "зарабатываем" if total_pnl >= 0 else "теряем"
        lines.append(f"Позиции ({len(positions)}) — {word} `{fmt_usd(total_pnl)}`")

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

    return "\n".join(lines)


def _build_close_kb(positions: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    total = len(positions)
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
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data="balance_refresh")])
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

    futures_bal = await client.get_futures_balance()
    positions = await client.get_positions()

    db_recs = {r["symbol"]: r for r in db_mod.get_open_positions()}
    re_recs = {r["symbol"]: r for r in db_mod.get_all_reentry()}
    config = context.bot_data.get("config")
    tp_sl_pcts = context.bot_data.get("tp_sl_pcts", {})
    daily_stats = db_mod.get_daily_stats(date.today().isoformat())
    lev_cache = await _fetch_lev_cache(client, positions)

    return futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache


async def balance_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.bot_data["exchange"]
    try:
        futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache = \
            await _fetch_all(client, context)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return

    text = _build_balance_text(futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache)
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
            futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache = \
                await _fetch_all(client, context)
        except Exception as e:
            await q.answer(f"Ошибка: {e}", show_alert=True)
            return
        text = _build_balance_text(futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache)
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

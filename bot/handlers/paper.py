"""/paper — бумажный портфель $500."""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from bot import db as db_mod
from bot.fmt import fmt_pct, fmt_usd
from bot.paper_trading import calc_pnl_pct, _remaining_budget, PAPER_INITIAL_BALANCE
import logging

logger = logging.getLogger(__name__)


_SEP = "─" * 20


def _fmt_paper_pos_block(pos: dict, mark_price: float) -> str:
    symbol = pos["symbol"]
    coin = symbol.split("/")[0]
    side = pos["side"]
    lev = int(pos["leverage"])
    entry = float(pos["entry_price"])
    total_inv = float(pos["total_invested"])
    avg_count = int(pos["averaging_count"])
    budget = float(pos.get("averaging_budget", 0))
    tp_pct = float(pos.get("tp_pct") or 500.0)
    sl_pct = float(pos.get("sl_pct") or 500.0)

    pnl_pct = calc_pnl_pct(entry, mark_price, lev, side) if mark_price > 0 else 0.0
    pnl_usd = total_inv * pnl_pct / 100
    pnl_icon = "🟢" if pnl_usd >= 0 else "🔴"

    side_icon = "🔴⬇️" if side == "short" else "🟢⬆️"

    move_tp = entry * tp_pct / 100 / lev
    move_sl = entry * sl_pct / 100 / lev
    tp_price = entry - move_tp if side == "short" else entry + move_tp
    sl_price = entry + move_sl if side == "short" else entry - move_sl

    lines = [
        f"{coin} {side_icon} {lev}x ${total_inv:.2f}",
        f"▶️ {entry:.6g}" + (f" | Mark: {mark_price:.6g}" if mark_price > 0 else ""),
        f"{pnl_icon} {fmt_usd(pnl_usd)} ({fmt_pct(pnl_pct)})",
        f"SL:{sl_price:.6g} (-{sl_pct:.0f}%)",
        f"TP:{tp_price:.6g} (+{tp_pct:.0f}%)",
        f"🔁 Докупок: {avg_count} | вложено ${total_inv:.2f}/${budget:.2f}",
    ]
    return "\n".join(lines)


def _build_text_and_kb(account, open_positions, stats, live_prices: dict):
    free = float(account["balance"])
    initial = float(account["initial_balance"])

    total_unrealized = 0.0
    for pos in open_positions:
        mark_price = live_prices.get(pos["symbol"], 0.0)
        if mark_price > 0:
            entry = float(pos["entry_price"])
            lev = int(pos["leverage"])
            total_inv = float(pos["total_invested"])
            pnl_pct = calc_pnl_pct(entry, mark_price, lev, pos["side"])
            total_unrealized += total_inv * pnl_pct / 100

    locked = sum(float(p["total_invested"]) + _remaining_budget(p) for p in open_positions)
    equity = free + locked + total_unrealized
    delta = equity - initial
    sign = "+" if delta >= 0 else ""

    lines = [
        "*📄 Бумажный портфель*",
        f"Старт: `${initial:.0f}` | Эквити: `${equity:.2f}` (`{sign}{delta:.2f}`)",
        f"Свободно: `${free:.2f}` | В позах: `${locked:.2f}`",
    ]

    if open_positions:
        total_pnl = total_unrealized
        word = "зарабатываем" if total_pnl >= 0 else "теряем"
        lines.append(f"Нереализовано: `{fmt_usd(total_pnl)}` ({word})")
        lines.append(f"\n*Открыто {len(open_positions)}/10:*")
        for pos in open_positions:
            mark_price = live_prices.get(pos["symbol"], 0.0)
            lines.append(_SEP)
            lines.append(_fmt_paper_pos_block(pos, mark_price))
    else:
        lines.append("\n_Нет открытых позиций_")

    tp_c = stats.get("tp_count", 0)
    sl_c = stats.get("sl_count", 0)
    total_closed = tp_c + sl_c
    if total_closed > 0:
        win_rate = tp_c / total_closed * 100
        realized = stats.get("tp_pnl", 0.0) + stats.get("sl_pnl", 0.0)
        lines.append(
            f"\n*Статистика ({total_closed} закрытых):*"
            f"\n✅ TP: {tp_c}  ❌ SL: {sl_c}  ({win_rate:.0f}% побед)"
            f"\nРеализовано: `{fmt_usd(realized)}`"
        )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Обновить", callback_data="paper_refresh"),
        InlineKeyboardButton("🗑 Сбросить", callback_data="paper_reset_ask"),
    ]])
    return "\n".join(lines), kb


async def _send_paper(message, context, edit: bool = False):
    db_mod.init_paper_account(PAPER_INITIAL_BALANCE)
    client = context.bot_data.get("exchange")
    open_positions = db_mod.get_open_paper_positions()
    live_prices: dict = {}
    if client:
        for pos in open_positions:
            try:
                ticker = await client._exchange.fetch_ticker(pos["symbol"])
                live_prices[pos["symbol"]] = float(ticker.get("last", 0) or 0)
            except Exception:
                pass
    text, kb = _build_text_and_kb(
        db_mod.get_paper_account(), open_positions, db_mod.get_paper_stats(), live_prices
    )
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


async def paper_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    cmd = args[0].lower() if args else ""

    if cmd in ("off", "on"):
        config = context.bot_data.get("config")
        enabled = cmd == "on"
        if config:
            config.paper_enabled = enabled
        db_mod.set_config("paper_enabled", "true" if enabled else "false")
        state = "включён ✅" if enabled else "отключён ❌"
        await update.message.reply_text(f"📄 Бумажный портфель {state}")
        return

    await _send_paper(update.message, context, edit=False)


async def paper_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "paper_refresh":
        await _send_paper(q.message, context, edit=True)
        return

    if q.data == "paper_reset_ask":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, сбросить", callback_data="paper_reset_confirm"),
            InlineKeyboardButton("◀ Отмена", callback_data="paper_reset_cancel"),
        ]])
        await q.message.reply_text(
            "⚠️ Сбросить бумажный портфель?\n"
            "Все открытые позиции будут закрыты, баланс вернётся к `$500`.",
            parse_mode="Markdown", reply_markup=kb,
        )
        return

    if q.data == "paper_reset_cancel":
        await q.delete_message()
        return

    if q.data == "paper_reset_confirm":
        db_mod.reset_paper_account(PAPER_INITIAL_BALANCE)
        await q.edit_message_text("✅ Бумажный портфель сброшен. Баланс `$500`, все позиции закрыты.",
                                  parse_mode="Markdown")

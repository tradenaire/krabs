"""Callback handlers for the live monitor inline keyboard."""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes


async def monitor_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """mon_close_{symbol} — ask confirmation before closing."""
    q = update.callback_query
    await q.answer()

    symbol = q.data[len("mon_close_"):]
    coin = symbol.split("/")[0]

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Да, закрыть {coin}", callback_data=f"mon_close_confirm_{symbol}")],
        [InlineKeyboardButton("◀ Отмена", callback_data="mon_close_cancel")],
    ])
    await q.message.reply_text(
        f"⚠️ Закрыть *{coin}* по рынку?",
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def monitor_close_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """mon_close_confirm_{symbol} — actually close the position."""
    q = update.callback_query
    await q.answer()

    symbol = q.data[len("mon_close_confirm_"):]
    client = context.bot_data["exchange"]
    coin = symbol.split("/")[0]

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


async def monitor_close_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Отменено")
    await q.delete_message()


async def monitor_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """mon_stats — show stats from monitor button."""
    q = update.callback_query
    await q.answer()
    from bot.handlers.stats import stats_handler
    await stats_handler(update, context)

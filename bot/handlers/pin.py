"""/pin — закрепить баланс и обновлять каждые 30 секунд."""
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from bot import db as db_mod

logger = logging.getLogger(__name__)

_PIN_CHAT_KEY = "pin_chat_id"
_PIN_MSG_KEY  = "pin_message_id"


async def _build_pin_text(client, context) -> str:
    from bot.handlers.balance import _fetch_all, _build_balance_text
    try:
        futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache, spot_bal = \
            await _fetch_all(client, context)
        text = _build_balance_text(futures_bal, positions, tp_sl_pcts, db_recs, re_recs,
                                   config, daily_stats, lev_cache, spot_bal)
    except Exception as e:
        return f"❌ Свежий баланс не получен: {type(e).__name__}. Повторная проверка по расписанию."
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    return text[:3900] + f"\n\n🕐 _снимок получен {now}_"


async def pin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.bot_data.get("exchange")
    chat_id = update.effective_chat.id

    text = await _build_pin_text(client, context)
    try:
        msg = await update.message.reply_text(text, parse_mode="Markdown")
    except Exception:
        msg = await update.message.reply_text(text)

    try:
        await context.bot.pin_chat_message(chat_id, msg.message_id,
                                           disable_notification=True)
    except Exception as e:
        logger.warning("pin_chat_message failed: %s", e)

    # Persist
    context.bot_data[_PIN_CHAT_KEY] = chat_id
    context.bot_data[_PIN_MSG_KEY]  = msg.message_id
    db_mod.set_config(_PIN_CHAT_KEY, str(chat_id))
    db_mod.set_config(_PIN_MSG_KEY,  str(msg.message_id))


async def pin_update_job(app):
    """Обновляет закреплённое сообщение по расписанию."""
    chat_id    = app.bot_data.get(_PIN_CHAT_KEY)
    message_id = app.bot_data.get(_PIN_MSG_KEY)

    if not chat_id or not message_id:
        # Try restore from DB on first run after restart
        chat_id    = db_mod.get_config(_PIN_CHAT_KEY)
        message_id = db_mod.get_config(_PIN_MSG_KEY)
        if chat_id and message_id:
            app.bot_data[_PIN_CHAT_KEY] = int(chat_id)
            app.bot_data[_PIN_MSG_KEY]  = int(message_id)
            chat_id    = int(chat_id)
            message_id = int(message_id)
        else:
            return

    client = app.bot_data.get("exchange")
    text = await _build_pin_text(client, app)

    try:
        await app.bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                        text=text, parse_mode="Markdown")
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.debug("pin update failed: %s", e)

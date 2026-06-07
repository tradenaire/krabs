from __future__ import annotations

import tempfile
import logging
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from bot.signals.execution import execute_signal
from bot.signals.parser import SignalParseError, parse_signal
from bot.signals.preview import build_signal_confirmation_text, build_signal_keyboard
from bot.signals.store import clear_signal, get_signal, save_signal
from bot.signals.vision import decode_signal_image

logger = logging.getLogger(__name__)


def prepare_signal_confirmation(raw_text: str, user_data: dict, warning: str = ""):
    signal = parse_signal(raw_text)
    signal_id = save_signal(user_data, signal)
    text = build_signal_confirmation_text(signal, warning=warning)
    keyboard = build_signal_keyboard(signal_id)
    return signal_id, text, keyboard


def prepare_signal_confirmation_from_signal(signal, user_data: dict, warning: str = ""):
    signal_id = save_signal(user_data, signal)
    text = build_signal_confirmation_text(signal, warning=warning)
    keyboard = build_signal_keyboard(signal_id)
    return signal_id, text, keyboard


def format_vision_decode_warning(error: Exception, model: str) -> str:
    raw = str(error)
    low = raw.lower()
    if "short sl must be above entry" in low:
        reason = "для SHORT стоп-лосс должен быть выше entry, а TP должны быть ниже entry."
    elif "long sl must be below entry" in low:
        reason = "для LONG стоп-лосс должен быть ниже entry, а TP должны быть выше entry."
    elif "tps must be" in low or "tp targets" in low:
        reason = "тейк-профиты выглядят противоречиво относительно entry."
    elif "missing entry" in low or "missing sl" in low or "missing tp" in low or "missing" in low:
        reason = "на картинке не удалось уверенно найти все обязательные поля: entry, SL и TP."
    elif "valid json" in low or "json" in low:
        reason = "модель не вернула структурированные переменные сигнала."
    else:
        reason = (
            "модель не смогла надежно разобрать скрин. Это может быть защита API, лимит, "
            "неподдерживаемая модель или неясная картинка."
        )
    return (
        f"⚠️ Не открываю сделку по скрину через `{model}`.\n"
        f"Причина: {reason}\n\n"
        "Кнопки открытия не показываю, чтобы не поставить опасную или неверную позицию. "
        "Пришли сигнал текстом или caption: SYMBOL, LONG/SHORT, Entry, SL, TP1/TP2/TP3."
    )


async def _mark_processing(update: Update, context: ContextTypes.DEFAULT_TYPE, reaction: str = "👀") -> None:
    if not update.effective_chat or not update.effective_message:
        return
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    except Exception:
        pass
    try:
        await context.bot.set_message_reaction(
            chat_id=update.effective_chat.id,
            message_id=update.effective_message.message_id,
            reaction=reaction,
        )
    except Exception:
        pass


async def maybe_handle_signal_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.message.text:
        return False
    try:
        _signal_id, text, keyboard = prepare_signal_confirmation(update.message.text, context.user_data)
    except SignalParseError:
        return False
    await _mark_processing(update, context, "👀")
    await update.message.reply_text(text, reply_markup=keyboard)
    return True


async def signal_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await _mark_processing(update, context, "👀")

    if update.message.caption:
        try:
            _signal_id, text, keyboard = prepare_signal_confirmation(update.message.caption, context.user_data)
            await update.message.reply_text(text, reply_markup=keyboard)
            return
        except SignalParseError:
            pass

    if not update.message.photo:
        return

    with tempfile.NamedTemporaryFile(prefix="krabs-signal-", suffix=".jpg", delete=False) as fh:
        path = Path(fh.name)
    try:
        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        await tg_file.download_to_drive(custom_path=str(path))

        config = context.bot_data.get("config")
        api_key = getattr(config, "openrouter_api_key", "") if config else ""
        model = getattr(config, "signal_vision_model", "openai/gpt-5.5") if config else "openai/gpt-5.5"
        if not api_key:
            await _mark_processing(update, context, "⚠️")
            await update.message.reply_text(
                "⚠️ Не настроен `openrouter_api_key`, поэтому картинку не могу расшифровать нейросетью.\n"
                "Кнопки открытия по скрину не показываю. Пришли этот же сигнал текстом или caption, и я покажу подтверждение.",
                parse_mode="Markdown",
            )
            return

        try:
            result = await decode_signal_image(path, api_key=api_key, model=model)
        except Exception as e:
            logger.warning("signal vision decode failed with %s: %s", model, e)
            await _mark_processing(update, context, "⚠️")
            await update.message.reply_text(
                format_vision_decode_warning(e, model),
                parse_mode="Markdown",
            )
            return

        try:
            _signal_id, preview, keyboard = prepare_signal_confirmation_from_signal(
                result.signal,
                context.user_data,
                warning=result.warning,
            )
        except SignalParseError as e:
            logger.warning("signal vision validation failed with %s: %s", model, e)
            await _mark_processing(update, context, "⚠️")
            await update.message.reply_text(
                format_vision_decode_warning(e, model),
                parse_mode="Markdown",
            )
            return
        await update.message.reply_text(preview, reply_markup=keyboard)
    finally:
        try:
            path.unlink()
        except OSError:
            pass


async def signal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        await q.edit_message_text("Сигнал устарел. Пришли его еще раз.")
        return

    action = parts[0]
    signal_id = parts[1]
    signal = get_signal(context.user_data, signal_id)
    if not signal:
        await q.edit_message_text("Сигнал устарел. Пришли его еще раз.")
        return

    if action == "sig_cancel":
        clear_signal(context.user_data, signal_id)
        await q.edit_message_text("Отменено.")
        return

    if action == "sig_edit":
        await q.edit_message_text(
            "Пришли исправленный сигнал текстом: SYMBOL, LONG/SHORT, Entry, SL, TP1/TP2/TP3."
        )
        return

    if action != "sig_open" or len(parts) < 3:
        await q.edit_message_text("Неизвестное действие.")
        return

    try:
        margin = float(parts[2])
    except ValueError:
        await q.edit_message_text("Неверная сумма входа.")
        return

    client = context.bot_data.get("exchange")
    if not client:
        await q.edit_message_text("Биржевой клиент недоступен.")
        return

    await q.edit_message_text(f"Открываю {signal.symbol} {signal.side.upper()} на ${margin:g}...")
    try:
        result = await execute_signal(client, context.application, signal, margin)
    except Exception as e:
        await q.edit_message_text(f"Ошибка открытия сигнала: {e}")
        return

    clear_signal(context.user_data, signal_id)
    await q.edit_message_text(
        "\n".join([
            f"Открыто: {result['symbol']} {signal.side.upper()}",
            f"Margin: ${margin:g}",
            f"Leverage: x{result['leverage']}",
            f"Entry: {result['entry_price']:.8g}",
            f"TP orders: {len(signal.tps)}",
            f"SL: {signal.stop:.8g}",
        ])
    )

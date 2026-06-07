from __future__ import annotations

import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from bot.signals.execution import execute_signal
from bot.signals.parser import SignalParseError, parse_signal
from bot.signals.preview import build_signal_confirmation_text, build_signal_keyboard
from bot.signals.store import clear_signal, get_signal, save_signal
from bot.signals.vision import decode_signal_image


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
                "Не блокирую сигнал: пришли этот же сигнал текстом или caption, и я покажу подтверждение.",
                parse_mode="Markdown",
            )
            return

        try:
            result = await decode_signal_image(path, api_key=api_key, model=model)
        except Exception as e:
            await _mark_processing(update, context, "⚠️")
            await update.message.reply_text(
                f"⚠️ Не смог расшифровать картинку через `{model}`: {e}\n"
                "Не блокирую сигнал: пришли его текстом, и я соберу переменные для ордеров.",
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
            await _mark_processing(update, context, "⚠️")
            await update.message.reply_text(f"⚠️ Сигнал распознан неполно: {e}\n\nПришли исправленный текст.")
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

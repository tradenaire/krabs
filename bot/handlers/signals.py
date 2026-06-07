from __future__ import annotations

import os
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from bot.signals.execution import execute_signal
from bot.signals.parser import SignalParseError, parse_signal
from bot.signals.preview import build_signal_confirmation_text, build_signal_keyboard
from bot.signals.store import clear_signal, get_signal, save_signal


def prepare_signal_confirmation(raw_text: str, user_data: dict):
    signal = parse_signal(raw_text)
    signal_id = save_signal(user_data, signal)
    text = build_signal_confirmation_text(signal)
    keyboard = build_signal_keyboard(signal_id)
    return signal_id, text, keyboard


async def maybe_handle_signal_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.message.text:
        return False
    try:
        _signal_id, text, keyboard = prepare_signal_confirmation(update.message.text, context.user_data)
    except SignalParseError:
        return False
    await update.message.reply_text(text, reply_markup=keyboard)
    return True


def _ocr_image(path: Path) -> str:
    try:
        from rapidocr_onnxruntime import RapidOCR

        engine = RapidOCR()
        result, _ = engine(str(path))
        if not result:
            return ""
        return "\n".join(str(row[1]) for row in result if len(row) > 1)
    except ImportError:
        pass

    try:
        from PIL import Image
        import pytesseract

        return pytesseract.image_to_string(Image.open(path), lang="eng+rus")
    except ImportError as e:
        raise RuntimeError("OCR engine is not installed. Send the signal as text or caption.") from e


async def signal_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if update.message.caption:
        try:
            _signal_id, text, keyboard = prepare_signal_confirmation(update.message.caption, context.user_data)
            await update.message.reply_text(text, reply_markup=keyboard)
            return
        except SignalParseError:
            pass

    if not update.message.photo:
        return

    fd, raw_path = tempfile.mkstemp(prefix="krabs-signal-", suffix=".jpg")
    os.close(fd)
    path = Path(raw_path)
    try:
        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        await tg_file.download_to_drive(custom_path=str(path))
        try:
            text = _ocr_image(path)
        except RuntimeError as e:
            await update.message.reply_text(f"Не смог прочитать картинку: {e}")
            return
        if not text.strip():
            await update.message.reply_text("Не смог прочитать сигнал с картинки. Пришли текстом или caption.")
            return
        try:
            _signal_id, preview, keyboard = prepare_signal_confirmation(text, context.user_data)
        except SignalParseError as e:
            await update.message.reply_text(f"Картинку прочитал, но сигнал неполный: {e}\n\nПришли сигнал текстом.")
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

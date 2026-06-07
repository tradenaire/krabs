from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.signals.model import ParsedSignal


DEFAULT_SIGNAL_MARGINS = (1, 2, 5, 10)


def _price(value: float) -> str:
    return f"{value:.8g}"


def _pct(value: float) -> str:
    return f"{value:g}%"


def build_signal_confirmation_text(signal: ParsedSignal, margin: float | None = None,
                                   warning: str = "") -> str:
    entry = _price(signal.entry_min)
    if signal.entry_max != signal.entry_min:
        entry = f"{entry} / {_price(signal.entry_max)}"

    lines = [
        "Проверь распознанный сигнал:",
        f"{signal.symbol} {signal.side.upper()}",
        f"Entry: {entry}",
        f"SL: {_price(signal.stop)}",
    ]
    for idx, tp in enumerate(signal.tps, 1):
        lines.append(f"TP{idx}: {_price(tp.price)} — {_pct(tp.share_pct)}")
    if warning:
        lines.extend(["", f"⚠️ Warning: {warning}"])

    lines.extend([
        "",
        "После подтверждения бот выставит TP ордера и 1 SL.",
        "Если доли TP не были указаны в сигнале, бот распределит их по количеству TP.",
    ])
    if margin is not None:
        lines.append(f"Margin: ${margin:g}")
    if signal.leverage:
        lines.append(f"Leverage in signal: x{signal.leverage}")
    if signal.confidence is not None:
        lines.append(f"Confidence: {signal.confidence}%")
    lines.extend([
        "",
        "Если все распознано правильно: выбери сумму входа кнопкой ниже.",
    ])
    return "\n".join(lines)


def build_signal_keyboard(signal_id: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(f"Открыть ${margin:g}", callback_data=f"sig_open:{signal_id}:{margin:g}")
            for margin in DEFAULT_SIGNAL_MARGINS[:2]
        ],
        [
            InlineKeyboardButton(f"Открыть ${margin:g}", callback_data=f"sig_open:{signal_id}:{margin:g}")
            for margin in DEFAULT_SIGNAL_MARGINS[2:]
        ],
        [InlineKeyboardButton("Проверить/исправить", callback_data=f"sig_edit:{signal_id}")],
        [InlineKeyboardButton("Отмена", callback_data=f"sig_cancel:{signal_id}")],
    ]
    return InlineKeyboardMarkup(rows)

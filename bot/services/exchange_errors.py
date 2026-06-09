from __future__ import annotations

import json
import re


def _coin(symbol: str) -> str:
    if not symbol:
        return "позицию"
    return symbol.split("/")[0].split(":")[0]


def _binance_payload(raw: str) -> dict:
    match = re.search(r"(\{.*\})", raw)
    if not match:
        return {}
    try:
        value = json.loads(match.group(1))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _binance_code(raw: str) -> int | None:
    payload = _binance_payload(raw)
    code = payload.get("code")
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def _fmt_price(value: float) -> str:
    return f"`{value:.8g}`" if value else "`?`"


def format_open_error(
    exc: Exception,
    *,
    symbol: str = "",
    side: str = "",
    stage: str = "",
    entry_price: float = 0.0,
    mark_price: float = 0.0,
    trigger_price: float = 0.0,
) -> str:
    raw = str(exc)
    raw_low = raw.lower()
    code = _binance_code(raw)
    coin = _coin(symbol)
    if not stage:
        match = re.search(r"\b(TP\d+|SL)\b", raw, re.IGNORECASE)
        if match:
            stage = match.group(1).upper()
    stage_text = f" на этапе `{stage}`" if stage else ""
    header = f"❌ Не открыл {coin}{stage_text}"
    side_norm = (side or "").lower()
    side_label = "SHORT" if side_norm in ("short", "sell") else "LONG" if side_norm in ("long", "buy") else "позиции"

    verify_match = re.search(r"expected\s+(\d+)\s+TP orders,\s+found\s+(\d+)", raw, re.IGNORECASE)
    if verify_match:
        expected_tp, found_tp = verify_match.groups()
        return "\n".join([
            header,
            "Причина: TP/SL не подтвердились через Binance после входа.",
            "Бот открыл вход, но вход был закрыт, чтобы не оставить без TP/SL.",
            f"Проверка: ожидал {expected_tp} TP, нашёл {found_tp}.",
            "Что сделать: повторить открытие после фикса/обновления; бот оставит позицию только если Binance readback подтвердит защиту.",
            "Сырой ответ Binance скрыт. Подробности есть в логах.",
        ])

    if code == -2021 or "would immediately trigger" in raw_low or "сработал бы сразу" in raw_low:
        if "tp" in stage.lower() or "tp" in raw_low:
            if side_label == "SHORT":
                rule = "Для SHORT тейк должен быть ниже текущей цены."
            elif side_label == "LONG":
                rule = "Для LONG тейк должен быть выше текущей цены."
            else:
                rule = "TP стоит слишком близко или с неправильной стороны от текущей цены."
        else:
            if side_label == "SHORT":
                rule = "Для SHORT стоп должен быть выше текущей цены."
            elif side_label == "LONG":
                rule = "Для LONG стоп должен быть ниже текущей цены."
            else:
                rule = "SL стоит слишком близко или с неправильной стороны от текущей цены."
        return "\n".join([
            header,
            "Причина: ордер сработал бы сразу, поэтому Binance его отклонил.",
            rule,
            f"Entry: {_fmt_price(entry_price)} | текущая: {_fmt_price(mark_price)} | триггер: {_fmt_price(trigger_price)}",
            "Что сделать: обновить сигнал по текущей цене или отодвинуть TP/SL от рынка.",
        ])

    if code == -4130 or ("closeposition" in raw_low and "existing" in raw_low):
        return "\n".join([
            header,
            "Причина: на Binance уже есть защитный стоп/тейк `closePosition` в эту сторону.",
            "Что сделает бот после фикса: старые TP/SL будут отменены и подтверждены через биржу перед установкой новых.",
            "Позиция не должна оставаться без защиты: если новые TP/SL нельзя поставить, бот закроет свежий вход или покажет явную инструкцию.",
        ])

    clean = raw.split("binanceusdm", 1)[0].strip(" :-")
    if len(clean) > 180:
        clean = clean[:177] + "..."
    return "\n".join([
        header,
        f"Причина: {clean or 'биржа отклонила открытие позиции.'}",
        "Сырой ответ Binance скрыт, чтобы не путать диагностику. Подробности есть в логах.",
    ])

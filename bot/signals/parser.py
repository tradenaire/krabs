from __future__ import annotations

import re

from bot.signals.model import ParsedSignal, SignalValidationError, TpTarget, default_tp_shares


class SignalParseError(ValueError):
    """Raised when text does not contain a complete trading signal."""


_PRICE = r"\$?\s*(\d+(?:[.,]\d+)?)"
_ENTRY_RE = re.compile(
    rf"(?:ENTRY|ENTER|ВХОД)\D{{0,60}}{_PRICE}(?:\s*(?:-|/|–|—|TO|ДО)\s*{_PRICE})?",
    re.IGNORECASE,
)
_STOP_RE = re.compile(rf"(?:SL|STOP|СТОП|СТОПЛОСС)\D{{0,60}}{_PRICE}", re.IGNORECASE)
_TP_RE = re.compile(rf"(?:TP|ТП)\s*([1-9])\D{{0,30}}{_PRICE}", re.IGNORECASE)
_SYMBOL_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,15})\s*(?:/|\s|-)?\s*USDT\b", re.IGNORECASE)
_LEV_RE = re.compile(r"(?:\bX|Х)\s*(\d{1,3})\b|\b(\d{1,3})\s*(?:X|Х)\b", re.IGNORECASE)
_CONF_RE = re.compile(r"\b(\d{1,3})\s*%")


def _to_float(raw: str) -> float:
    return float(raw.replace(",", ".").replace("$", "").strip())


def _clean_symbol(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", raw.upper())


def _parse_side(text: str) -> str:
    upper = text.upper()
    if re.search(r"\b(SHORT|ШОРТ)\b", upper):
        return "short"
    if re.search(r"\b(LONG|ЛОНГ)\b", upper):
        return "long"
    raise SignalParseError("Signal side is missing.")


def _parse_symbol(text: str) -> str:
    match = _SYMBOL_RE.search(text.upper())
    if match:
        return _clean_symbol(match.group(1))
    raise SignalParseError("Signal symbol is missing.")


def _parse_entry(text: str) -> tuple[float, float]:
    match = _ENTRY_RE.search(text)
    if not match:
        raise SignalParseError("Signal entry is missing.")
    first = _to_float(match.group(1))
    second = _to_float(match.group(2)) if match.group(2) else first
    return min(first, second), max(first, second)


def _parse_stop(text: str) -> float:
    match = _STOP_RE.search(text)
    if not match:
        raise SignalParseError("Signal SL is missing.")
    return _to_float(match.group(1))


def _parse_tps(text: str) -> tuple[TpTarget, ...]:
    indexed: dict[int, float] = {}
    for match in _TP_RE.finditer(text):
        indexed[int(match.group(1))] = _to_float(match.group(2))
    if not indexed:
        raise SignalParseError("Signal TP targets are missing.")
    prices = [indexed[i] for i in sorted(indexed)]
    shares = default_tp_shares(len(prices))
    return tuple(TpTarget(price=price, share_pct=share) for price, share in zip(prices, shares))


def _parse_leverage(text: str) -> int | None:
    match = _LEV_RE.search(text)
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    value = int(raw)
    return value if value > 0 else None


def _parse_confidence(text: str) -> int | None:
    matches = [int(m.group(1)) for m in _CONF_RE.finditer(text)]
    plausible = [value for value in matches if 0 <= value <= 100]
    return plausible[-1] if plausible else None


def parse_signal(text: str) -> ParsedSignal:
    if not text or not text.strip():
        raise SignalParseError("Signal text is empty.")
    normalized = text.replace(",", ".")
    try:
        signal = ParsedSignal(
            symbol=_parse_symbol(normalized),
            side=_parse_side(normalized),
            entry_min=_parse_entry(normalized)[0],
            entry_max=_parse_entry(normalized)[1],
            stop=_parse_stop(normalized),
            tps=_parse_tps(normalized),
            leverage=_parse_leverage(normalized),
            confidence=_parse_confidence(normalized),
            source_text=text,
        )
        return signal.validate()
    except SignalValidationError as e:
        raise SignalParseError(str(e)) from e

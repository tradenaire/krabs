from __future__ import annotations

from dataclasses import dataclass


class SignalValidationError(ValueError):
    """Raised when a parsed signal is incomplete or unsafe to execute."""


@dataclass(frozen=True)
class TpTarget:
    price: float
    share_pct: float


@dataclass(frozen=True)
class ParsedSignal:
    symbol: str
    side: str
    entry_min: float
    entry_max: float
    stop: float
    tps: tuple[TpTarget, ...]
    leverage: int | None = None
    confidence: int | None = None
    source_text: str = ""

    @property
    def entry_mid(self) -> float:
        return (self.entry_min + self.entry_max) / 2

    @property
    def order_side(self) -> str:
        return "sell" if self.side == "short" else "buy"

    @property
    def close_side(self) -> str:
        return "buy" if self.side == "short" else "sell"

    def validate(self) -> "ParsedSignal":
        if self.side not in ("long", "short"):
            raise SignalValidationError("Signal side must be long or short.")
        if not self.symbol:
            raise SignalValidationError("Signal symbol is missing.")
        if self.entry_min <= 0 or self.entry_max <= 0:
            raise SignalValidationError("Signal entry price must be positive.")
        if self.entry_min > self.entry_max:
            raise SignalValidationError("Signal entry range is inverted.")
        if self.stop <= 0:
            raise SignalValidationError("Signal stop price must be positive.")
        if not self.tps:
            raise SignalValidationError("Signal must include at least one TP.")

        mid = self.entry_mid
        if self.side == "short":
            if self.stop <= mid:
                raise SignalValidationError("Short SL must be above entry.")
            bad_tp = [tp.price for tp in self.tps if tp.price >= mid]
            if bad_tp:
                raise SignalValidationError("Short TPs must be below entry.")
        else:
            if self.stop >= mid:
                raise SignalValidationError("Long SL must be below entry.")
            bad_tp = [tp.price for tp in self.tps if tp.price <= mid]
            if bad_tp:
                raise SignalValidationError("Long TPs must be above entry.")
        return self


def default_tp_shares(count: int) -> list[float]:
    if count <= 0:
        return []
    if count == 3:
        return [50.0, 25.0, 25.0]
    share = round(100.0 / count, 8)
    shares = [share] * count
    shares[-1] = round(100.0 - sum(shares[:-1]), 8)
    return shares

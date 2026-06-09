from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CloseFacts:
    symbol: str
    side: str
    reason_code: str
    reason_label: str
    entry_price: float
    exit_price: float
    leverage: int
    margin: float
    realized_pnl: float | None
    hold_seconds: int = 0


@dataclass(frozen=True)
class ReentryFacts:
    enabled: bool
    will_reenter: bool
    why: str
    cycle_next: int | None = None
    max_cycles: int | None = None
    cooldown_text: str = ""


def _coin(symbol: str) -> str:
    return symbol.split("/")[0].split(":")[0]


def _side_label(side: str) -> str:
    return "SHORT" if side in ("short", "sell") else "LONG" if side in ("long", "buy") else side.upper()


def _money(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):.2f}"


def _price(value: float) -> str:
    return f"{value:.8g}" if value else "?"


def calc_pnl_pct(entry: float, exit_price: float, side: str, leverage: int) -> float:
    if entry <= 0 or exit_price <= 0:
        return 0.0
    raw = (exit_price - entry) / entry * 100.0
    if side in ("short", "sell"):
        raw = -raw
    return raw * max(leverage, 1)


def format_hold(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds}с"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}м {sec}с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}ч {minutes}м"


def format_close_message(close: CloseFacts, reentry: ReentryFacts) -> str:
    pnl_known = close.realized_pnl is not None and close.entry_price > 0 and close.exit_price > 0
    pnl_pct = calc_pnl_pct(close.entry_price, close.exit_price, close.side, close.leverage) if pnl_known else None
    pnl_icon = "⚪" if not pnl_known else ("✅" if close.realized_pnl >= 0 else "🛑")
    reentry_line = "Перезаход: да" if reentry.will_reenter else "Перезаход: нет"
    if reentry.will_reenter and reentry.cycle_next is not None and reentry.max_cycles is not None:
        reentry_line += f", цикл `{reentry.cycle_next}/{reentry.max_cycles}`"
    if reentry.cooldown_text:
        reentry_line += f" ({reentry.cooldown_text})"

    pnl_line = (
        f"Маржа: `${close.margin:.2f}` | PnL: `{pnl_pct:+.1f}%` / `{_money(close.realized_pnl)}`"
        if pnl_known
        else "Маржа: `${:.2f}` | PnL: `неизвестен`".format(close.margin)
    )
    lines = [
        f"{pnl_icon} *{_coin(close.symbol)}* {_side_label(close.side)} закрыта",
        f"Причина: {close.reason_label}",
        f"Entry: `{_price(close.entry_price)}` | Exit: `{_price(close.exit_price)}` | Плечо: `×{close.leverage}`",
        pnl_line,
        reentry_line,
        f"Почему: {reentry.why}",
    ]
    if not pnl_known:
        lines.insert(4, "PnL неизвестен: Binance/история не дали цену закрытия, бот не выдумывает прибыль или убыток.")
    if close.hold_seconds:
        lines.insert(4, f"Время в позиции: `{format_hold(close.hold_seconds)}`")
    return "\n".join(lines)

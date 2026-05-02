"""Shared formatters for PnL display."""


def fmt_pct(v: float) -> str:
    """Format percentage with adaptive precision."""
    sign = "+" if v >= 0 else ""
    av = abs(v)
    if av >= 10:
        d = 1
    elif av >= 1:
        d = 2
    elif av >= 0.01:
        d = 3
    else:
        d = 4
    return f"{sign}{v:.{d}f}%"


def fmt_usd(v: float) -> str:
    """Format USD amount with adaptive precision."""
    sign = "+" if v >= 0 else "-"
    av = abs(v)
    if av >= 1:
        d = 2
    elif av >= 0.001:
        d = 4
    else:
        d = 6
    return f"{sign}${av:.{d}f}"


def calc_close_pnl(entry: float, exit_price: float, side: str,
                   leverage: int, margin: float) -> tuple[float, float]:
    """Compute realized PnL from close.

    Returns (pnl_usdt, pnl_pct_of_margin), both signed.
    side='short' → выигрываем когда exit_price < entry.
    pnl_pct_of_margin = ценовое движение в % × leverage (с правильным знаком).
    Если данных нет (entry/exit ≤ 0 или margin ≤ 0) — возвращает (0, 0).
    """
    if entry <= 0 or exit_price <= 0 or margin <= 0:
        return 0.0, 0.0
    move_pct = (exit_price - entry) / entry * 100  # +ve если цена выросла
    if side == "short":
        pct_signed = -move_pct * leverage
    else:
        pct_signed = move_pct * leverage
    pnl_usdt = margin * pct_signed / 100
    return pnl_usdt, pct_signed


def format_close_pnl(entry: float, exit_price: float, side: str,
                     leverage: int, margin: float) -> str:
    """Human-readable PnL suffix '($+12.34 / +234%)' for close-event messages.

    Возвращает пустую строку если данных недостаточно — caller просто не
    добавит суффикс к сообщению. Используется в reentry_job для всех веток
    (TP/profit-SL/loss-SL/exhausted/max_cycles=0).
    """
    if entry <= 0 or exit_price <= 0 or margin <= 0:
        return ""
    pnl, pct = calc_close_pnl(entry, exit_price, side, leverage, margin)
    # Знак ставим ПЕРЕД $/% а не внутри числа: '+$2.27' / '-$4.76', '+455%' / '-952%'.
    pnl_sign = "+" if pnl >= 0 else "-"
    pct_sign = "+" if pct >= 0 else "-"
    return f"`{pnl_sign}${abs(pnl):.2f} / {pct_sign}{abs(pct):.0f}%`"

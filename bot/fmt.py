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

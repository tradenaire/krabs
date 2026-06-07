"""Position sizing: leverage caps, minimum order notional, capital budget.

Single source of truth for math previously duplicated across
handlers/trading.py, handlers/scan.py, handlers/balance.py and jobs/main.py.
"""
from __future__ import annotations

# MEXC enforces a 5 USDT minimum notional by default (error 7008). Per-symbol
# overrides are populated from real rejections and cached.
MEXC_DEFAULT_MIN_NOTIONAL = 5.0


def max_leverage_by_vol(vol_24h_usdt: float) -> int:
    """Cap leverage by 24h quote volume as a liquidity/volatility proxy."""
    if vol_24h_usdt >= 500_000_000:
        return 20
    if vol_24h_usdt >= 50_000_000:
        return 10
    return 5


def get_min_notional(symbol: str, min_order_cache: dict) -> float:
    """Minimum USDT notional (position value) for an order on this symbol."""
    return min_order_cache.get(symbol, MEXC_DEFAULT_MIN_NOTIONAL)


def get_min_avg_margin(symbol: str, leverage: int, min_order_cache: dict) -> float:
    """Margin to use for averaging (5% buffer for contract rounding)."""
    return get_min_notional(symbol, min_order_cache) / max(leverage, 1) * 1.05


def can_avg_at_configured(symbol: str, leverage: int, averaging_amount: float,
                          min_order_cache: dict) -> bool:
    """True if averaging_amount * leverage covers the MEXC minimum notional."""
    return averaging_amount * max(leverage, 1) >= get_min_notional(symbol, min_order_cache)


def check_budget(free_balance: float, margin: float, config,
                 eff_avg_amount: float | None = None) -> dict:
    """Worst-case capital-at-risk budget check for opening a position.

    full_budget = (margin + averaging_budget) * sl_pct/100, unless profit-lock is
    enabled (then max loss is just the invested base budget).
    """
    avg_amount = eff_avg_amount or float(getattr(config, "averaging_amount", 0.10))
    avg_budget = float(getattr(config, "averaging_budget", 5.00))
    sl_pct = float(getattr(config, "sl_pct", 500))
    profit_lock_trigger = float(getattr(config, "averaging_profit_lock_trigger", 0))

    base_budget = margin + avg_budget
    if profit_lock_trigger > 0:
        full_budget = base_budget
    else:
        full_budget = base_budget * (sl_pct / 100.0)

    max_steps = int(avg_budget / avg_amount) if avg_amount > 0 else 0
    positions_possible = int(free_balance / full_budget) if full_budget > 0 else 0

    return {
        "can_open": free_balance >= margin,
        "can_full_budget": free_balance >= full_budget,
        "full_budget": full_budget,
        "base_budget": base_budget,
        "positions_possible": positions_possible,
        "max_steps": max_steps,
        "sl_pct": sl_pct,
        "free": free_balance,
    }

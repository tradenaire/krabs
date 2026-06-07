"""TP/SL math and exchange-side set/verify — single source of truth.

Previously duplicated across handlers/trading.py, jobs/main.py and pos_format.py.
All formulas operate on PnL-on-margin percentage (leveraged move).
"""
from __future__ import annotations


def calc_tp_price(entry: float, leverage: int, tp_pct: float, side: str) -> float:
    """Price that yields +tp_pct PnL on margin for the given side."""
    move = entry * tp_pct / 100 / leverage
    return entry - move if side == "short" else entry + move


def calc_sl_price(entry: float, leverage: int, sl_pct: float, side: str) -> float:
    """Price that yields -sl_pct PnL on margin for the given side."""
    move = entry * sl_pct / 100 / leverage
    return entry + move if side == "short" else entry - move


def pnl_pct_at_price(entry: float, leverage: int, price: float, side: str) -> float:
    """Leveraged PnL% on margin if the position were at ``price``."""
    if entry <= 0:
        return 0.0
    if side == "short":
        return (entry - price) / entry * leverage * 100
    return (price - entry) / entry * leverage * 100


def trigger_price_matches(want: float, got: float, rel_tol: float = 0.003) -> bool:
    if want <= 0 or got <= 0:
        return False
    return abs(got - want) / want <= rel_tol


async def verify_active_sl(client, symbol: str, side: str, sl_price: float) -> None:
    """Raise if no SL plan-order is active near ``sl_price`` on the exchange."""
    sl_type = 2 if side == "long" else 1
    orders = await client.get_tp_sl_orders(symbol)
    for order in orders:
        if int(order.get("trigger_type", 0) or 0) != sl_type:
            continue
        if trigger_price_matches(sl_price, float(order.get("trigger_price", 0) or 0)):
            return
    raise RuntimeError(f"active SL not found at {sl_price:.6g}")


async def set_tp_sl_verified(client, symbol: str, side: str,
                             tp_price: float | None, sl_price: float,
                             pos_data: dict, **kwargs) -> None:
    """Set TP/SL and verify the SL actually registered (raises otherwise)."""
    results = await client.set_tp_sl(
        symbol, tp_price=tp_price, sl_price=sl_price, pos_data=pos_data, **kwargs
    )
    for result in results or []:
        if result.get("error"):
            raise RuntimeError(result["error"])
        body = result.get("result")
        if isinstance(body, dict) and body.get("success") is False:
            raise RuntimeError(str(body.get("message", body)))
    await verify_active_sl(client, symbol, side, sl_price)

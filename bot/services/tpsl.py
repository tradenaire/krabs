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


def _norm_side(side: str) -> str:
    return "short" if side in ("short", "sell") else "long"


def _trigger_types(side: str) -> tuple[int, int]:
    return (1, 2) if _norm_side(side) == "long" else (2, 1)


def validate_exit_prices(symbol: str, side: str, reference: float,
                         tp_prices: list[float] | tuple[float, ...],
                         sl_price: float | None = None) -> None:
    """Validate that exits are positive and on the executable side of price."""
    reference = float(reference or 0)
    if reference <= 0:
        raise ValueError(f"{symbol}: reference price must be positive before TP/SL placement.")
    norm_side = _norm_side(side)
    for idx, raw_price in enumerate(tp_prices, 1):
        price = float(raw_price or 0)
        if price <= 0:
            raise ValueError(f"{symbol}: TP{idx} must be positive before TP/SL placement.")
        if norm_side == "long" and price <= reference:
            raise ValueError(f"{symbol}: TP{idx} must be above current price for LONG.")
        if norm_side == "short" and price >= reference:
            raise ValueError(f"{symbol}: TP{idx} must be below current price for SHORT.")
    if sl_price is not None:
        sl = float(sl_price or 0)
        if sl <= 0:
            raise ValueError(f"{symbol}: SL must be positive before TP/SL placement.")
        if norm_side == "long" and sl >= reference:
            raise ValueError(f"{symbol}: SL must be below current price for LONG.")
        if norm_side == "short" and sl <= reference:
            raise ValueError(f"{symbol}: SL must be above current price for SHORT.")


async def verify_exit_orders(client, symbol: str, side: str,
                             tp_prices: list[float] | tuple[float, ...],
                             sl_price: float | None = None) -> dict:
    """Read active exchange orders and require every expected TP/SL to exist."""
    tp_type, sl_type = _trigger_types(side)
    orders = await client.get_tp_sl_orders(symbol)
    active_tps = [
        float(order.get("trigger_price", 0) or 0)
        for order in orders
        if int(order.get("trigger_type", 0) or 0) == tp_type
    ]
    active_sls = [
        float(order.get("trigger_price", 0) or 0)
        for order in orders
        if int(order.get("trigger_type", 0) or 0) == sl_type
    ]

    missing_tps = [
        float(price)
        for price in tp_prices
        if not any(trigger_price_matches(float(price), got) for got in active_tps)
    ]
    if missing_tps:
        raise RuntimeError(
            f"{symbol}: expected {len(tp_prices)} TP orders, found {len(active_tps)}; "
            f"missing {', '.join(f'{price:.8g}' for price in missing_tps)}"
        )

    sl_ok = True
    if sl_price is not None:
        sl_ok = any(trigger_price_matches(float(sl_price), got) for got in active_sls)
        if not sl_ok:
            raise RuntimeError(
                f"{symbol}: expected active SL at {float(sl_price):.8g}, found {len(active_sls)} SL orders."
            )

    return {
        "tp_count": len(active_tps),
        "sl_count": len(active_sls),
        "orders": orders,
    }


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
    reference = float((pos_data or {}).get("mark_price") or (pos_data or {}).get("entry_price") or 0)
    validate_exit_prices(
        symbol,
        side,
        reference,
        [tp_price] if tp_price else [],
        sl_price,
    )
    results = await client.set_tp_sl(
        symbol, tp_price=tp_price, sl_price=sl_price, pos_data=pos_data, **kwargs
    )
    for result in results or []:
        if result.get("error"):
            raise RuntimeError(result["error"])
        body = result.get("result")
        if isinstance(body, dict) and body.get("success") is False:
            raise RuntimeError(str(body.get("message", body)))
    await verify_exit_orders(
        client,
        symbol,
        side,
        [tp_price] if tp_price else [],
        sl_price,
    )

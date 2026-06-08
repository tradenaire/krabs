"""3-TP ladder exit: partial take-profits + breakeven stop-loss.

On open (when exit_mode == "ladder"): place a stop-loss plus 3 reduce-only
take-profit orders. Each TP closes `tp_partial_pct`% of the *remaining* position.
After the first TP fills, the SL is moved to breakeven (entry).

Levels come from the analyst pick (tp1/tp2/tp3/sl) when valid, otherwise from a
config PnL%% ladder fallback.
"""
from __future__ import annotations

import logging
import re

from bot.services.tpsl import calc_tp_price, calc_sl_price, validate_exit_prices, verify_exit_orders

logger = logging.getLogger(__name__)


def parse_price(text: str) -> float:
    """Parse a price from analyst text like '$1.23', '1.23–1.45', '1,234.5'."""
    if not text:
        return 0.0
    cleaned = str(text).replace("$", "").replace(",", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return float(m.group(0)) if m else 0.0


def _levels_from_pick(entry: float, side: str, pick: dict) -> tuple[list[float], float] | None:
    if not pick:
        return None
    tps = [parse_price(pick.get(k, "")) for k in ("tp1", "tp2", "tp3")]
    tps = [t for t in tps if t > 0]
    sl = parse_price(pick.get("sl", ""))
    if not tps or sl <= 0 or entry <= 0:
        return None
    if side == "long":
        if any(t <= entry for t in tps) or tps != sorted(tps) or sl >= entry:
            return None
    else:  # short
        if any(t >= entry for t in tps) or tps != sorted(tps, reverse=True) or sl <= entry:
            return None
    return tps, sl


def _levels_from_config(entry: float, leverage: int, side: str, config) -> tuple[list[float], float]:
    raw = str(getattr(config, "tp_ladder_pcts", "50,120,250"))
    try:
        pcts = [float(x) for x in raw.split(",") if x.strip()][:3]
    except Exception:
        pcts = [50.0, 120.0, 250.0]
    while len(pcts) < 3:
        pcts.append(pcts[-1] * 2 if pcts else 100.0)
    tps = [calc_tp_price(entry, leverage, p, side) for p in pcts]
    sl = calc_sl_price(entry, leverage, float(getattr(config, "sl_pct", 500)), side)
    return tps, sl


def compute_levels(entry: float, leverage: int, side: str, pick: dict | None, config):
    """Return (TP levels, SL). Prefer AI/filter levels, fall back to config ladder."""
    from_pick = _levels_from_pick(entry, side, pick or {})
    if from_pick:
        return from_pick
    return _levels_from_config(entry, leverage, side, config)


def tp_quantities(contracts: float, partial_pct: float, n_levels: int = 3) -> list[float]:
    """Split `contracts` so each level closes partial_pct%% of the remaining.
    Last level closes whatever remains."""
    frac = max(0.0, min(partial_pct / 100.0, 1.0))
    qtys: list[float] = []
    remaining = contracts
    for i in range(n_levels):
        if i == n_levels - 1:
            q = remaining
        else:
            q = remaining * frac
        qtys.append(q)
        remaining -= q
    return qtys


async def setup_on_open(client, app, symbol: str, side: str, entry: float,
                        leverage: int, contracts: float, pick: dict | None) -> None:
    """Place SL + 3 partial TP orders and record the ladder in the DB."""
    from bot.infra import db as adb

    config = app.bot_data.get("config")
    partial_pct = float(getattr(config, "tp_partial_pct", 50.0))
    tps, sl = compute_levels(entry, leverage, side, pick, config)
    validate_exit_prices(symbol, side, entry, tps, sl)
    qtys = tp_quantities(contracts, partial_pct, n_levels=len(tps))

    fsym = client.futures_symbol(symbol)
    await client.place_reduce_sl(symbol, side, sl, qty=None)

    for i, (tp, q) in enumerate(zip(tps, qtys), 1):
        if q <= 0:
            continue
        await client.place_reduce_tp(symbol, side, q, tp)

    await verify_exit_orders(client, symbol, side, tps, sl)

    padded = (tps + [0.0, 0.0, 0.0])[:3]
    await adb.upsert_tp_ladder(fsym, side, entry, leverage, padded[0], padded[1], padded[2], sl)
    logger.info("ladder set for %s %s: TP %s SL %.6g", fsym, side,
                [round(t, 6) for t in tps], sl)


async def rebuild(client, app, symbol: str, ladder: dict, contracts: float,
                  breakeven: bool, reference: float | None = None) -> None:
    """Cancel all conditional orders and re-place SL + remaining (unfilled) TPs,
    sizing from the current remaining contracts. SL goes to entry if breakeven."""
    config = app.bot_data.get("config")
    partial_pct = float(getattr(config, "tp_partial_pct", 50.0))
    side = ladder["side"]
    entry = float(ladder["entry_price"])

    remaining_levels = []
    for i in (1, 2, 3):
        if not ladder.get(f"filled{i}"):
            price = float(ladder[f"tp{i}"])
            if price > 0:
                remaining_levels.append(price)

    try:
        await client.cancel_tp_sl_orders(symbol)
    except Exception as e:
        logger.debug("ladder rebuild cancel %s: %s", symbol, e)

    sl_price = entry if breakeven else float(ladder["sl"])
    validation_reference = float(reference or entry)
    validate_exit_prices(symbol, side, validation_reference, remaining_levels, sl_price)
    await client.place_reduce_sl(symbol, side, sl_price, qty=None)

    if remaining_levels:
        qtys = tp_quantities(contracts, partial_pct, n_levels=len(remaining_levels))
        for tp, q in zip(remaining_levels, qtys):
            if q <= 0:
                continue
            await client.place_reduce_tp(symbol, side, q, tp)
    await verify_exit_orders(client, symbol, side, remaining_levels, sl_price)

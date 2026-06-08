"""Ladder exit engine — monitors 3-TP partial exits and moves SL to breakeven.

For each active ladder it counts the still-open take-profit orders on the
exchange; when that count drops, a TP filled. It marks the level filled, moves
the stop-loss to breakeven after the first fill, and re-asserts the remaining
TP orders sized from the current remaining position. Cleans up when the position
is gone.
"""
from __future__ import annotations

import logging

from bot.engines.base import Engine

logger = logging.getLogger(__name__)


def _is_tp(order: dict, side: str) -> bool:
    # trigger_type for TP: long=1, short=2 (MEXC-style, mirrored by all adapters)
    tp_type = 1 if side == "long" else 2
    return int(order.get("trigger_type", 0) or 0) == tp_type


def _tp_slot_price(ladder: dict, idx: int) -> float:
    try:
        return float(ladder.get(f"tp{idx}") or 0)
    except Exception:
        return 0.0


def _expected_open_tp_count(ladder: dict) -> int:
    return sum(
        1
        for idx in (1, 2, 3)
        if _tp_slot_price(ladder, idx) > 0 and not ladder.get(f"filled{idx}")
    )


def _next_unfilled_tp_slots(ladder: dict, count: int) -> list[int]:
    slots: list[int] = []
    for idx in (1, 2, 3):
        if len(slots) >= count:
            break
        if _tp_slot_price(ladder, idx) > 0 and not ladder.get(f"filled{idx}"):
            slots.append(idx)
    return slots


class LadderExitEngine(Engine):
    name = "ladder_exit"
    interval = 10.0

    async def tick(self) -> None:
        from bot.infra import db as adb
        client = self.app.bot_data.get("exchange")
        if not client:
            return

        ladders = await adb.get_active_tp_ladders()
        if not ladders:
            return

        from bot.services import ladder as ladder_svc

        for lad in ladders:
            symbol = lad["symbol"]
            side = lad["side"]
            try:
                pos = await client.get_position(symbol)
            except Exception as e:
                logger.debug("ladder tick get_position %s: %s", symbol, e)
                continue

            # Position fully closed (TP3 / SL / manual) — clean up.
            if not pos or float(pos.get("contracts", 0)) <= 0:
                try:
                    await client.cancel_tp_sl_orders(symbol)
                except Exception:
                    pass
                await adb.close_tp_ladder(symbol)
                logger.info("ladder %s closed (position gone)", symbol)
                continue

            try:
                orders = await client.get_tp_sl_orders(symbol)
            except Exception as e:
                logger.debug("ladder tick orders %s: %s", symbol, e)
                continue
            open_tps = sum(1 for o in orders if _is_tp(o, side))

            expected_open = _expected_open_tp_count(lad)
            if open_tps >= expected_open:
                continue  # nothing newly filled

            newly = expected_open - open_tps
            # Mark the next `newly` unfilled levels as filled.
            for i in _next_unfilled_tp_slots(lad, newly):
                await adb.mark_tp_filled(symbol, i)
                lad[f"filled{i}"] = 1
                logger.info("ladder %s: TP%d filled", symbol, i)

            # After the first TP, move SL to breakeven; re-assert remaining TPs.
            breakeven = bool(getattr(self.app.bot_data.get("config"), "breakeven_on_first_tp", True))
            contracts = float(pos.get("contracts", 0))
            reference = float(pos.get("mark_price") or pos.get("entry_price") or lad.get("entry_price") or 0)
            await ladder_svc.rebuild(
                client,
                self.app,
                symbol,
                lad,
                contracts,
                breakeven=breakeven,
                reference=reference,
            )
            if breakeven:
                await adb.mark_ladder_breakeven(symbol)

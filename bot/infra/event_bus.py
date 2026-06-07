"""Tiny async pub/sub event bus for inter-engine coordination.

Engines publish domain events (position opened, SL hit, scan finished) and other
engines subscribe without direct imports/coupling. Handlers are awaited
sequentially inside ``publish``; exceptions in one subscriber never break others.

Example:
    bus = EventBus()
    bus.subscribe("position.opened", reentry_engine.on_opened)
    await bus.publish("position.opened", symbol="BTC/USDT:USDT")
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[None]]


# Canonical event names (avoid stringly-typed typos across modules).
class Events:
    POSITION_OPENED = "position.opened"
    POSITION_CLOSED = "position.closed"
    POSITION_AVERAGED = "position.averaged"
    SL_HIT = "position.sl_hit"
    TP_HIT = "position.tp_hit"
    EMERGENCY_TRIGGERED = "emergency.triggered"
    SCAN_CANDIDATES = "scan.candidates"


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event: str, handler: Handler) -> None:
        self._subs[event].append(handler)

    def unsubscribe(self, event: str, handler: Handler) -> None:
        if handler in self._subs.get(event, []):
            self._subs[event].remove(handler)

    async def publish(self, event: str, **payload) -> None:
        handlers = list(self._subs.get(event, []))
        for handler in handlers:
            try:
                await handler(**payload)
            except Exception:
                logger.exception("event handler failed for %s", event)


def get_bus(app) -> EventBus:
    bus = app.bot_data.get("event_bus")
    if bus is None:
        bus = EventBus()
        app.bot_data["event_bus"] = bus
    return bus

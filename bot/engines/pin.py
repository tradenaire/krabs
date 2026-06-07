"""Pin engine — refreshes the pinned balance message every minute.

The legacy ``pin_update_job`` was defined but never scheduled; this engine wires
it in so the pinned message actually auto-updates after ``/pin``.
"""
from __future__ import annotations

from bot.engines.base import Engine


class PinEngine(Engine):
    name = "pin_update"
    interval = 60.0
    initial_delay = 30.0

    async def tick(self) -> None:
        from bot.handlers.pin import pin_update_job
        await pin_update_job(self.app)

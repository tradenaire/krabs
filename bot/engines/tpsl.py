"""TP/SL enforce engine — keeps DB in sync and clears orphan plan-orders."""
from __future__ import annotations

from bot.engines.base import Engine


class TpSlEngine(Engine):
    name = "tpsl_enforce"
    interval = 60.0

    async def tick(self) -> None:
        from bot.jobs.main import tpsl_enforce_job
        await tpsl_enforce_job(self.app)

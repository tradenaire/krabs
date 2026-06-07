"""Re-entry engine — reopens positions after TP / profit-lock SL per cycles."""
from __future__ import annotations

from bot.engines.base import Engine


class ReentryEngine(Engine):
    name = "reentry"
    interval = 30.0

    async def tick(self) -> None:
        from bot.jobs.main import reentry_job
        await reentry_job(self.app)

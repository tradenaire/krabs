"""Averaging (docupka) engine — adds to losing positions per config rules.

Wraps the averaging logic in jobs/main.py. The margin-emergency safeguard that
used to live inside it now runs in EmergencyEngine; this loop honors the shared
``_avg_disabled_until`` pause flag the emergency engine sets.
"""
from __future__ import annotations

from bot.engines.base import Engine


class AveragingEngine(Engine):
    name = "averaging"
    interval = 3.0

    async def tick(self) -> None:
        from bot.jobs.main import averaging_job
        await averaging_job(self.app)

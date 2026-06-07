"""Scout engine — searches for new short candidates and opens positions.

Phase 2: wraps the existing ``auto_scan_job`` (scan + LLM analysis + open) as an
independent loop. Phase 3 moves the heavy scan/analysis into a separate worker
process; this engine then consumes candidates and only places orders here.
"""
from __future__ import annotations

from bot.engines.base import Engine


class ScoutEngine(Engine):
    name = "auto_scan"
    interval = 1800.0  # 30 min default; overridden from config at build time
    initial_delay = 30.0

    async def tick(self) -> None:
        from bot.jobs.main import auto_scan_job
        await auto_scan_job(self.app)

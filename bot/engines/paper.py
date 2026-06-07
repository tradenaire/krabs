"""Paper-trading engines — virtual portfolio simulation loops."""
from __future__ import annotations

from bot.engines.base import Engine


class PaperScanEngine(Engine):
    name = "paper_scan"
    interval = 1800.0
    initial_delay = 15.0

    async def tick(self) -> None:
        from bot.paper_trading import paper_scan_job
        await paper_scan_job(self.app)


class PaperSignalEngine(Engine):
    name = "paper_signal"
    interval = 300.0
    initial_delay = 20.0

    async def tick(self) -> None:
        from bot.paper_trading import paper_signal_job
        await paper_signal_job(self.app)


class PaperUpdateEngine(Engine):
    name = "paper_update"
    interval = 20.0

    async def tick(self) -> None:
        from bot.paper_trading import paper_update_job
        await paper_update_job(self.app)

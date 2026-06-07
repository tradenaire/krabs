"""Monitoring engines: position/balance cache and low-balance alerts.

These wrap the existing job bodies in jobs/main.py so behavior is unchanged;
they simply run as independent loops instead of APScheduler jobs.
"""
from __future__ import annotations

from bot.engines.base import Engine


class MonitorEngine(Engine):
    """Refreshes _pos_cache / _bal_cache every few seconds."""
    name = "monitor"
    interval = 3.0

    async def tick(self) -> None:
        from bot.jobs.main import positions_cache_job
        await positions_cache_job(self.app)


class BalanceAlertEngine(Engine):
    name = "balance_alert"
    interval = 180.0

    async def tick(self) -> None:
        from bot.jobs.main import balance_alert_job
        await balance_alert_job(self.app)

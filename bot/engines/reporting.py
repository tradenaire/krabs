"""Reporting engine — sends the daily summary once per day at ~23:00 local.

Replaces the APScheduler CronTrigger. Checks the clock each minute and fires
``daily_report_job`` the first time the local hour reaches the target after the
last sent date.
"""
from __future__ import annotations

import datetime as _dt

from bot.engines.base import Engine


class ReportingEngine(Engine):
    name = "daily_report"
    interval = 60.0
    target_hour = 23

    def __init__(self, app, interval: float | None = None):
        super().__init__(app, interval)
        self._last_report_date: _dt.date | None = None

    async def tick(self) -> None:
        now = _dt.datetime.now()
        if now.hour == self.target_hour and self._last_report_date != now.date():
            self._last_report_date = now.date()
            from bot.jobs.main import daily_report_job
            await daily_report_job(self.app)

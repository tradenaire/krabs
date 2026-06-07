"""Engine lifecycle primitives.

An ``Engine`` is a named coroutine that runs ``tick()`` on a fixed interval in
its own asyncio task. Ticks never overlap for a single engine (the loop awaits
the previous tick before sleeping), mirroring APScheduler's ``max_instances=1``,
but different engines run fully concurrently.

The ``EngineManager`` owns the set of engines and supports hot interval changes
(used by ``/automode interval`` and ``/avg`` averaging interval edits).
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class Engine:
    #: unique id, also used by EngineManager.set_interval / get
    name: str = "engine"
    #: seconds between the end of one tick and the start of the next
    interval: float = 5.0
    #: delay before the first tick (lets startup sync settle)
    initial_delay: float = 0.0

    def __init__(self, app, interval: float | None = None):
        self.app = app
        if interval is not None:
            self.interval = interval
        self._task: asyncio.Task | None = None
        self._running = False
        self._wake = asyncio.Event()
        self.last_run_ts: float = 0.0
        self.last_error: str | None = None

    async def setup(self) -> None:
        """Optional one-time async init before the loop starts."""

    async def tick(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _loop(self) -> None:
        if self.initial_delay:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.initial_delay)
            except asyncio.TimeoutError:
                pass
        while self._running:
            t0 = time.monotonic()
            try:
                await self.tick()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e)
                logger.exception("engine %s tick failed", self.name)
            self.last_run_ts = time.time()
            elapsed = time.monotonic() - t0
            delay = max(0.0, self.interval - elapsed)
            # Sleep, but wake early if interval changed / stop requested
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name=f"engine:{self.name}")
        logger.info("engine %s started (interval=%.1fs)", self.name, self.interval)

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def set_interval(self, interval: float) -> None:
        self.interval = interval
        self._wake.set()  # re-evaluate sleep immediately

    def next_run_ts(self) -> float:
        return self.last_run_ts + self.interval if self.last_run_ts else time.time()


class EngineManager:
    def __init__(self, app):
        self.app = app
        self._engines: dict[str, Engine] = {}

    def add(self, engine: Engine) -> Engine:
        self._engines[engine.name] = engine
        return engine

    def get(self, name: str) -> Engine | None:
        return self._engines.get(name)

    def all(self) -> list[Engine]:
        return list(self._engines.values())

    async def start_all(self) -> None:
        for eng in self._engines.values():
            try:
                await eng.setup()
            except Exception:
                logger.exception("engine %s setup failed", eng.name)
            eng.start()
        logger.info("EngineManager started %d engines: %s",
                    len(self._engines), ", ".join(self._engines))

    async def stop_all(self) -> None:
        await asyncio.gather(*(e.stop() for e in self._engines.values()),
                             return_exceptions=True)

    def set_interval(self, name: str, interval: float) -> bool:
        eng = self._engines.get(name)
        if eng:
            eng.set_interval(interval)
            return True
        return False


def get_manager(app) -> "EngineManager | None":
    return app.bot_data.get("engine_manager")

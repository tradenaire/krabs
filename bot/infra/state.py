"""Shared runtime state and per-symbol locking.

Replaces the ~20 loose keys scattered across ``application.bot_data`` with a
single typed container, and provides a per-symbol lock manager so that
operations on *different* symbols can run in parallel while operations on the
*same* symbol are serialized (e.g. averaging vs re-entry vs manual close).

The container is intentionally backward-compatible: it does not remove existing
``bot_data`` keys. Engines read/write the typed fields; legacy code keeps using
``bot_data`` directly until migrated.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field


class LockManager:
    """Lazily-created asyncio.Lock per key (symbol). Safe for one event loop."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._global = asyncio.Lock()

    def get(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @contextlib.asynccontextmanager
    async def symbol(self, symbol: str):
        """Serialize order-affecting work for a single symbol."""
        lock = self.get(symbol)
        async with lock:
            yield

    @contextlib.asynccontextmanager
    async def globally(self):
        """Serialize work that touches all positions at once (e.g. emergency)."""
        async with self._global:
            yield


@dataclass
class AppState:
    """Typed runtime state shared by engines and services.

    Caches and flags previously held as bare bot_data keys live here. Each field
    keeps the legacy semantics so migration is a mechanical move.
    """

    # Position / balance caches (written by MonitorEngine, read by everyone)
    pos_cache: list[dict] = field(default_factory=list)
    pos_cache_ts: float = 0.0
    bal_cache: float = 0.0

    # Averaging / profit-lock coordination
    avg_last_ts: dict[str, float] = field(default_factory=dict)
    avg_disabled_until: float = 0.0
    emerg_last_check: float = 0.0
    avg_synth: dict[str, dict] = field(default_factory=dict)
    notified_exhausted: set[str] = field(default_factory=set)
    expected_contracts: dict[str, float] = field(default_factory=dict)
    contracts_warned: set[str] = field(default_factory=set)
    profit_lock_step: dict[str, float] = field(default_factory=dict)
    profit_lock_disabled: set[str] = field(default_factory=set)
    was_profit_locked: set[str] = field(default_factory=set)
    age_12h_notified: set[str] = field(default_factory=set)
    avg_bal_warn_ts: dict[str, float] = field(default_factory=dict)

    # Re-entry coordination
    sl_cooldown: dict[str, float] = field(default_factory=dict)

    # Misc flags
    bal_alert_sent: bool = False
    tpsl_run_count: int = 0
    min_order_cache: dict[str, float] = field(default_factory=dict)

    # Locks
    locks: LockManager = field(default_factory=LockManager)


def get_state(app) -> AppState:
    """Get (or lazily create) the AppState attached to a PTB Application."""
    st = app.bot_data.get("app_state")
    if st is None:
        st = AppState()
        app.bot_data["app_state"] = st
    return st

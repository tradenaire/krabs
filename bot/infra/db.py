"""Async wrapper over the synchronous SQLite layer (bot.db).

The legacy ``bot.db`` module is fully synchronous; calling it directly from a
coroutine blocks the event loop. Engines and services should use this module
instead, which offloads every call to a worker thread via ``asyncio.to_thread``.

Usage:
    from bot.infra import db as adb
    positions = await adb.get_open_positions()
    await adb.set_config("foo", "bar")

Any public function of ``bot.db`` is also reachable through ``adb.call``:
    await adb.call("some_new_db_fn", arg1, arg2)
"""
from __future__ import annotations

import asyncio
import functools

from bot import db as _db


async def call(fn_name: str, *args, **kwargs):
    """Run an arbitrary bot.db function in a thread."""
    fn = getattr(_db, fn_name)
    return await asyncio.to_thread(fn, *args, **kwargs)


def _wrap(name: str):
    sync_fn = getattr(_db, name)

    @functools.wraps(sync_fn)
    async def _async(*args, **kwargs):
        return await asyncio.to_thread(sync_fn, *args, **kwargs)

    _async.__name__ = name
    return _async


# Explicit async mirrors for every public bot.db function. Generated at import
# time so signatures/docstrings are preserved via functools.wraps.
_EXPORTED = [
    "init_db", "get_all_config", "set_config", "get_config",
    "upsert_position", "get_open_positions", "get_open_position",
    "update_averaging", "update_position_tpsl", "close_position",
    "get_reentry", "upsert_reentry", "increment_reentry_cycle",
    "delete_reentry", "get_all_reentry", "dedupe_open_positions",
    "sync_closed_positions", "log_trade", "get_daily_stats",
    "open_position_history", "update_position_history_avg",
    "close_position_history", "get_last_position_history",
    "get_position_history", "init_paper_account", "get_paper_account",
    "update_paper_balance", "open_paper_position", "get_open_paper_positions",
    "get_closed_paper_positions", "update_paper_averaging",
    "close_paper_position", "log_paper_trade", "update_paper_funding",
    "update_paper_profit_lock", "update_paper_liq_price",
    "reset_paper_account", "get_paper_stats", "get_min_order_cache",
    "set_min_order_notional",
    "upsert_tp_ladder", "get_tp_ladder", "get_active_tp_ladders",
    "get_ladder_symbols", "mark_tp_filled", "mark_ladder_breakeven",
    "close_tp_ladder",
]

for _name in _EXPORTED:
    if hasattr(_db, _name):
        globals()[_name] = _wrap(_name)

del _name

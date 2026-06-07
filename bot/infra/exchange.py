"""Exchange concurrency helpers.

The single shared ``ExchangeClient`` (ccxt + one aiohttp session) is not safe to
hammer from many concurrent coroutines. This module provides:

- ``throttle(client)``: a global semaphore (per client instance) limiting the
  number of in-flight exchange requests, on top of ccxt's own rate limiter.
- ``rebuild_client(app, api_key, secret)``: recreate the client when MEXC keys
  change at runtime (legacy code never re-instantiated it after ``/setkey``).
"""
from __future__ import annotations

import asyncio
import logging
import weakref

logger = logging.getLogger(__name__)

MAX_INFLIGHT = 8

_semaphores: "weakref.WeakKeyDictionary[object, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _sem_for(client) -> asyncio.Semaphore:
    sem = _semaphores.get(client)
    if sem is None:
        sem = asyncio.Semaphore(MAX_INFLIGHT)
        _semaphores[client] = sem
    return sem


class _Throttle:
    def __init__(self, client):
        self._sem = _sem_for(client)

    async def __aenter__(self):
        await self._sem.acquire()
        return self

    async def __aexit__(self, *exc):
        self._sem.release()
        return False


def throttle(client) -> _Throttle:
    """`async with throttle(client): await client.something()`."""
    return _Throttle(client)


async def rebuild_client(app, config=None):
    """Recreate the exchange client from config (provider + credentials) and swap
    it in bot_data. Used after /setkey changes keys or the active provider.

    Returns the new client. The old client's sessions are closed best-effort.
    """
    from bot.exchange.factory import create_exchange_client

    config = config or app.bot_data.get("config")
    old = app.bot_data.get("exchange")
    new = create_exchange_client(config)
    app.bot_data["exchange"] = new
    if old is not None:
        try:
            await old.close()
        except Exception:
            logger.warning("failed to close old exchange client", exc_info=True)
    logger.info("Exchange client rebuilt (provider=%s)",
                getattr(config, "exchange_provider", "mexc"))
    return new

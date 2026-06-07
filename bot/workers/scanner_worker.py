"""Scanner worker process + client.

A separate OS process runs the pandas/pandas_ta technical scan (``scan_overbought``)
so it does not block the main event loop. The main process talks to it through
two multiprocessing Queues (requests in, results out).

The worker uses its own read-only ``ExchangeClient`` (public market data only),
so it does not need valid trading keys and never places orders. All order
placement stays in the main process (``ScoutEngine`` / ``auto_scan_job``).
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import queue as _queue

logger = logging.getLogger(__name__)

_SHUTDOWN = "__shutdown__"


# ── Worker process side ───────────────────────────────────────────

def _worker_entry(req_q: "mp.Queue", res_q: "mp.Queue", provider: str,
                  api_key: str, secret: str, testnet: bool):
    """Process entrypoint (must be top-level & importable for spawn)."""
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(_worker_loop(req_q, res_q, provider, api_key, secret, testnet))
    except Exception:  # pragma: no cover
        logging.getLogger(__name__).exception("scanner worker crashed")


def _build_worker_client(provider: str, api_key: str, secret: str, testnet: bool):
    """Build the same exchange provider as the main process (read-only scan)."""
    if (provider or "mexc").lower() in ("binance", "binance_testnet"):
        from bot.exchange.binance_client import BinanceClient
        return BinanceClient(api_key, secret, testnet=testnet)
    from bot.exchange.client import ExchangeClient
    return ExchangeClient(api_key, secret)


async def _worker_loop(req_q, res_q, provider: str, api_key: str, secret: str, testnet: bool):
    from bot.ai.scanner import scan_overbought

    client = _build_worker_client(provider, api_key, secret, testnet)
    loop = asyncio.get_running_loop()
    try:
        while True:
            # Blocking get off-loop so we can await it.
            req = await loop.run_in_executor(None, req_q.get)
            if req == _SHUTDOWN:
                break
            req_id = req.get("id")
            try:
                if req.get("type") == "scan":
                    results, scanned = await scan_overbought(
                        client,
                        float(req.get("rsi", 65.0)),
                        float(req.get("change", 10.0)),
                        int(req.get("max_symbols", 80)),
                    )
                    res_q.put({"id": req_id, "ok": True, "results": results, "scanned": scanned})
                else:
                    res_q.put({"id": req_id, "ok": False, "error": f"unknown type {req.get('type')}"})
            except Exception as e:
                res_q.put({"id": req_id, "ok": False, "error": str(e)})
    finally:
        try:
            await client.close()
        except Exception:
            pass


# ── Main process side ─────────────────────────────────────────────

class ScannerWorkerClient:
    def __init__(self):
        self._proc: mp.Process | None = None
        self._req_q: mp.Queue | None = None
        self._res_q: mp.Queue | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    def start(self, provider: str, api_key: str, secret: str, testnet: bool = False) -> bool:
        if self.alive:
            return True
        try:
            ctx = mp.get_context("spawn")
            self._req_q = ctx.Queue()
            self._res_q = ctx.Queue()
            self._proc = ctx.Process(
                target=_worker_entry,
                args=(self._req_q, self._res_q, provider, api_key, secret, testnet),
                name="scanner-worker",
                daemon=True,
            )
            self._proc.start()
            logger.info("scanner worker started (pid=%s)", self._proc.pid)
            return True
        except Exception:
            logger.exception("failed to start scanner worker")
            self._proc = None
            return False

    async def scan(self, rsi: float = 65.0, change: float = 10.0,
                   max_symbols: int = 80, timeout: float = 120.0) -> tuple[list[dict], int]:
        """Request a technical scan from the worker. Raises on timeout/error."""
        if not self.alive:
            raise RuntimeError("scanner worker not running")
        async with self._lock:
            self._next_id += 1
            req_id = self._next_id
            self._req_q.put({"id": req_id, "type": "scan", "rsi": rsi,
                             "change": change, "max_symbols": max_symbols})
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("scanner worker scan timed out")
                try:
                    resp = await loop.run_in_executor(
                        None, lambda: self._res_q.get(timeout=min(remaining, 5.0))
                    )
                except _queue.Empty:
                    continue
                if resp.get("id") != req_id:
                    continue  # stale response, keep waiting
                if not resp.get("ok"):
                    raise RuntimeError(resp.get("error", "scan failed"))
                return resp.get("results", []), resp.get("scanned", 0)

    async def stop(self):
        if self._proc is None:
            return
        try:
            if self._req_q is not None:
                self._req_q.put(_SHUTDOWN)
            await asyncio.get_running_loop().run_in_executor(None, self._proc.join, 5)
        except Exception:
            pass
        if self._proc.is_alive():
            self._proc.terminate()
        self._proc = None
        logger.info("scanner worker stopped")

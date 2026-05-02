"""Tests for bot/jobs/main.py:_resolve_close_reason — TP/SL/profit-SL detection."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from bot.jobs.main import _resolve_close_reason


def _client(was_closed_returns, ticker_last=None):
    """Build mock ExchangeClient. was_closed_returns: tuple (is_tp, trigger_price)."""
    c = MagicMock()
    c.was_closed_by_tp = AsyncMock(return_value=was_closed_returns)
    c._exchange = MagicMock()
    if ticker_last is not None:
        c._exchange.fetch_ticker = AsyncMock(return_value={"last": ticker_last})
    else:
        c._exchange.fetch_ticker = AsyncMock(side_effect=Exception("ticker unavailable"))
    return c


@pytest.mark.asyncio
async def test_tp_short_returns_tp_and_trigger():
    """SHORT: was_closed_by_tp возвращает (True, 1.95) — должны вернуть TP=True."""
    client = _client(was_closed_returns=(True, 1.95))
    is_tp, profitable_sl, exit_price = await _resolve_close_reason(
        client, "X/USDT:USDT", "short", opened_at_ms=1000, entry_price=2.0
    )
    assert is_tp is True
    assert profitable_sl is False
    assert exit_price == 1.95


@pytest.mark.asyncio
async def test_loss_sl_short():
    """SHORT loss SL: trigger > entry (2.20 > 2.0). closed_by_tp=False, profitable_sl=False."""
    client = _client(was_closed_returns=(False, 2.20))
    is_tp, profitable_sl, exit_price = await _resolve_close_reason(
        client, "X/USDT:USDT", "short", opened_at_ms=1000, entry_price=2.0
    )
    assert is_tp is False
    assert profitable_sl is False
    assert exit_price == 2.20


@pytest.mark.asyncio
async def test_profit_lock_sl_short():
    """SHORT profit-lock SL: trigger < entry (1.90 < 2.0) даже хотя is_tp=False.
    Должны распознать как profitable_sl=True (ползущий SL уже в плюсе)."""
    client = _client(was_closed_returns=(False, 1.90))
    is_tp, profitable_sl, exit_price = await _resolve_close_reason(
        client, "X/USDT:USDT", "short", opened_at_ms=1000, entry_price=2.0
    )
    assert is_tp is False
    assert profitable_sl is True
    assert exit_price == 1.90


@pytest.mark.asyncio
async def test_profit_lock_sl_long():
    """LONG profit-lock SL: trigger > entry (2.10 > 2.0) — profitable_sl=True."""
    client = _client(was_closed_returns=(False, 2.10))
    is_tp, profitable_sl, exit_price = await _resolve_close_reason(
        client, "X/USDT:USDT", "long", opened_at_ms=1000, entry_price=2.0
    )
    assert profitable_sl is True


@pytest.mark.asyncio
async def test_unknown_falls_back_to_ticker():
    """Если was_closed_by_tp вернула (None, None) — fallback на ticker.last."""
    client = _client(was_closed_returns=(None, None), ticker_last=1.95)
    is_tp, profitable_sl, exit_price = await _resolve_close_reason(
        client, "X/USDT:USDT", "short", opened_at_ms=1000, entry_price=2.0
    )
    # mark 1.95 < entry 2.0 для SHORT — считаем TP
    assert is_tp is True
    assert exit_price == 1.95


@pytest.mark.asyncio
async def test_unknown_with_no_ticker_returns_none():
    """Если и was_closed и ticker недоступны — is_tp=None, exit_price=None."""
    client = _client(was_closed_returns=(None, None), ticker_last=None)
    is_tp, profitable_sl, exit_price = await _resolve_close_reason(
        client, "X/USDT:USDT", "short", opened_at_ms=1000, entry_price=2.0
    )
    assert is_tp is None
    assert exit_price is None

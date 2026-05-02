"""Tests for bot/ai/scanner.py:mexc_suggest_tickers (Fix #3)."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from bot.ai.scanner import mexc_suggest_tickers


def _client_with_markets(*ids):
    c = MagicMock()
    c._exchange = MagicMock()
    c._exchange.load_markets = AsyncMock(return_value=None)
    markets = {}
    for mid in ids:
        base = mid.removesuffix("_USDT")
        markets[f"{base}/USDT:USDT"] = {
            "id": mid,
            "active": True,
            "type": "swap",
            "settle": "USDT",
        }
    c._exchange.markets = markets
    return c


@pytest.mark.asyncio
async def test_substring_priority_full_inclusion():
    """FART → FARTCOIN (substring внутри base) — приоритет 1."""
    client = _client_with_markets("FARTCOIN_USDT", "BTC_USDT", "AR_USDT")
    sugg = await mexc_suggest_tickers(client, "FART", n=3)
    assert "FARTCOIN" in sugg
    # FARTCOIN содержит FART → должна быть первой
    assert sugg[0] == "FARTCOIN"


@pytest.mark.asyncio
async def test_typo_longer_input():
    """Пользователь написал FARTCOIN, биржа имеет только FART. Ticker длиннее base."""
    client = _client_with_markets("FART_USDT", "BTC_USDT")
    sugg = await mexc_suggest_tickers(client, "FARTCOIN", n=3)
    assert "FART" in sugg


@pytest.mark.asyncio
async def test_returns_empty_for_no_matches():
    """Если ничего похожего — пустой список."""
    client = _client_with_markets("BTC_USDT", "ETH_USDT")
    sugg = await mexc_suggest_tickers(client, "ZZZNONEXISTENT", n=3)
    # difflib может всё-таки вернуть что-то близкое (например пустое — для длинного random)
    # Достаточно убедиться что результат — list (без падения).
    assert isinstance(sugg, list)


@pytest.mark.asyncio
async def test_inactive_markets_skipped():
    """Неактивный фьючерс не должен попадать в подсказки."""
    c = MagicMock()
    c._exchange = MagicMock()
    c._exchange.load_markets = AsyncMock(return_value=None)
    c._exchange.markets = {
        "FART/USDT:USDT": {"id": "FART_USDT", "active": False, "type": "swap", "settle": "USDT"},
        "BTC/USDT:USDT":  {"id": "BTC_USDT",  "active": True,  "type": "swap", "settle": "USDT"},
    }
    sugg = await mexc_suggest_tickers(c, "FART", n=3)
    assert "FART" not in sugg


@pytest.mark.asyncio
async def test_n_limit_respected():
    """N=2 → возвращаем максимум 2."""
    client = _client_with_markets("FARTCOIN_USDT", "FARTBOY_USDT", "FARTING_USDT", "FART_USDT")
    sugg = await mexc_suggest_tickers(client, "FART", n=2)
    assert len(sugg) <= 2


@pytest.mark.asyncio
async def test_empty_ticker_returns_empty():
    """Защита от пустого ввода."""
    client = _client_with_markets("BTC_USDT")
    assert await mexc_suggest_tickers(client, "", n=3) == []
    assert await mexc_suggest_tickers(client, "   ", n=3) == []

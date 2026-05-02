"""Tests for Fix #5 — cancel_tp_sl_orders returns -1 on delisted contract.

Проверяем именно error-detection в обёртке, не сам MEXC-вызов.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from bot.exchange.client import ExchangeClient


def _make_client(cancel_exception):
    """ExchangeClient с замоканным exchange API."""
    # Создаём instance без реального init (он требует API keys + ccxt session).
    c = ExchangeClient.__new__(ExchangeClient)
    c._exchange = MagicMock()
    # load_markets / market — не критичны, делаем no-op
    c._exchange.load_markets = AsyncMock(return_value=None)
    c._exchange.market = MagicMock(return_value={"id": "ZEC_USDT", "symbol": "ZEC/USDT:USDT"})
    # _mexc_contract_symbol → используем замоканный возврат
    c._mexc_contract_symbol = MagicMock(return_value="ZEC_USDT")
    # futures_symbol — простая функция, можно реальную через staticmethod
    c.futures_symbol = MagicMock(return_value="ZEC/USDT:USDT")
    # get_tp_sl_orders — пустой список (нет активных)
    c.get_tp_sl_orders = AsyncMock(return_value=[])
    # contractPrivatePostPlanorderCancelAll — кидает заданный exception
    c._exchange.contractPrivatePostPlanorderCancelAll = AsyncMock(side_effect=cancel_exception)
    return c


@pytest.mark.asyncio
async def test_cancel_returns_minus1_on_1001():
    """code:1001 'Contract does not exist' → CANCEL_DELISTED (-1)."""
    err = Exception('mexc {"success":false,"code":1001,"message":"Contract does not exist"}')
    client = _make_client(err)
    n = await client.cancel_tp_sl_orders("ZEC/USDT:USDT")
    assert n == ExchangeClient.CANCEL_DELISTED
    assert n == -1


@pytest.mark.asyncio
async def test_cancel_returns_minus1_on_text_match():
    """Альтернативный текст ошибки с 'contract does not exist' (без 1001)."""
    err = Exception("ApiError: Contract does not exist for the provided symbol")
    client = _make_client(err)
    n = await client.cancel_tp_sl_orders("ZEC/USDT:USDT")
    assert n == -1


@pytest.mark.asyncio
async def test_cancel_returns_zero_on_transient_error():
    """Сетевая ошибка → возвращаем 0, НЕ -1 (это не делистинг)."""
    err = Exception("Connection timeout")
    client = _make_client(err)
    n = await client.cancel_tp_sl_orders("BTC/USDT:USDT")
    assert n == 0  # старое поведение для транзитных ошибок


@pytest.mark.asyncio
async def test_cancel_success_returns_count():
    """Успешный CancelAll → возвращаем число до-отмены."""
    client = _make_client(cancel_exception=None)
    # Заменим: success path
    client._exchange.contractPrivatePostPlanorderCancelAll = AsyncMock(return_value={"success": True})
    # Скажем что было 3 ордера до отмены
    client.get_tp_sl_orders = AsyncMock(return_value=[
        {"symbol": "ZEC_USDT", "id": "1"},
        {"symbol": "ZEC_USDT", "id": "2"},
        {"symbol": "ZEC_USDT", "id": "3"},
    ])
    n = await client.cancel_tp_sl_orders("ZEC/USDT:USDT")
    assert n == 3

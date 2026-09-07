import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.balance import _fetch_all, _fetch_with_typing, _build_balance_text


class BalanceResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_overlap_and_no_contract_limit_requests(self):
        started = set()
        ready = asyncio.Event()

        async def request(name, result):
            started.add(name)
            if len(started) == 4:
                ready.set()
            await asyncio.wait_for(ready.wait(), 1)
            return result

        client = SimpleNamespace(
            get_futures_balance=lambda: request('futures', {'total': {'USDT': 10}}),
            get_spot_balance=lambda: request('spot', {'total': {'USDT': 5}}),
            get_positions=lambda: request('positions', []),
            get_asset_prices=lambda: request('prices', {'USDT': 1}))
        with patch('bot.db.get_all_reentry', return_value=[]), patch('bot.db.get_daily_stats', return_value={}):
            result = await _fetch_all(client, SimpleNamespace(bot_data={}))
        text = _build_balance_text(*result)
        self.assertIn('Всего: ≈ 15.00 USDT', text)
        self.assertLess(len(text), 450)
        self.assertNotIn('Монеты', text)

    async def test_typing_starts_during_fetch_and_stops_on_error(self):
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def action(**kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        async def fetch(*args):
            await asyncio.wait_for(entered.wait(), 1)
            raise RuntimeError('exchange unavailable')

        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock(side_effect=action)))
        with patch('bot.handlers.balance._fetch_all', side_effect=fetch):
            with self.assertRaises(RuntimeError):
                await _fetch_with_typing(None, context, SimpleNamespace(chat_id=42))
        self.assertTrue(stopped.is_set())
        context.bot.send_chat_action.assert_awaited_once_with(chat_id=42, action='typing')

import unittest
from unittest.mock import AsyncMock

from bot.exchange.client import ExchangeClient, available_margin
from bot.handlers.balance import _build_balance_text


class FuturesMarginDisplayTests(unittest.IsolatedAsyncioTestCase):
    def client(self, *responses):
        client = object.__new__(ExchangeClient)
        client._exchange = AsyncMock()
        client._exchange.request.side_effect = responses
        return client

    async def test_multi_asset_uses_exchange_summary_instead_of_usdt_zero(self):
        client = self.client(
            {'success': True, 'data': 'OPEN'},
            {'success': True, 'data': {'currency': 'USDT', 'adjEquity': 140.25,
                                      'availableBalance': 42.125678}})
        summary = await client.get_futures_margin_summary()
        balance = {'_raw': {'availableOpen': 0}, '_margin_summary': summary,
            '_assets': {'ETH': {'equity': 2, 'contributeMarginAmount': 200}},
            '_prices': {'ETH': 100, 'USDT': 1}}
        text = _build_balance_text(balance, [], {}, {}, {}, None, {})
        self.assertIn('Доступная маржа MEXC: 42.1257 USDT', text)
        self.assertIn('Обеспечение MEXC: 140.25 USDT', text)
        self.assertEqual(available_margin(balance), 0)
        self.assertEqual(client._exchange.request.call_args_list[0].args,
            ('multiAssets/getMultiAssetMode', ['contract', 'private'], 'GET'))
        self.assertEqual(client._exchange.request.call_args_list[1].args,
            ('multiAssets/getMultiAssets', ['contract', 'private'], 'GET'))

    async def test_zero_summary_is_displayed_as_zero(self):
        client = self.client({'success': True, 'data': 'OPEN'},
            {'success': True, 'data': {'currency': 'USDT', 'adjEquity': 10, 'availableBalance': 0}})
        summary = await client.get_futures_margin_summary()
        text = _build_balance_text({'_raw': {'availableOpen': 500}, '_margin_summary': summary},
            [], {}, {}, {}, None, {})
        self.assertIn('Доступная маржа MEXC: 0.0000 USDT', text)

    async def test_single_asset_mode_does_not_sum_other_coins(self):
        for mode in ('NOT_OPEN', 'FUNCTION_NOT_ALLOWED'):
            client = self.client({'success': True, 'data': mode})
            summary = await client.get_futures_margin_summary()
            text = _build_balance_text({'_margin_summary': summary,
                '_raw': {'availableOpen': 7.25}, '_assets': {'ETH': {'equity': 2}}},
                [], {}, {}, {}, None, {})
            self.assertIn('Доступная маржа MEXC: 7.2500 USDT', text)
            client._exchange.request.assert_awaited_once()

    async def test_invalid_or_missing_exchange_data_is_rejected(self):
        for data in ({}, {'currency': 'USDT', 'adjEquity': 10},
                     {'currency': 'USDT', 'adjEquity': 10, 'availableBalance': 'NaN'},
                     {'currency': 'USDT', 'adjEquity': 10, 'availableBalance': 'Infinity'}):
            client = self.client({'success': True, 'data': 'OPEN'}, {'success': True, 'data': data})
            with self.assertRaises((ValueError, RuntimeError)):
                await client.get_futures_margin_summary()

    async def test_unknown_mode_and_failed_response_are_rejected(self):
        for response in ({'success': True, 'data': 'UNKNOWN'}, {'success': False, 'code': 401}):
            with self.assertRaises((ValueError, RuntimeError)):
                await self.client(response).get_futures_margin_summary()

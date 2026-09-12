import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.exchange.client import ExchangeClient


class PositionSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.positions = [dict(symbol=f"COIN{i}_USDT", holdVol=10, holdAvgPrice=100,
                               leverage=10, im=10, positionType=1, positionId=i,
                               unRealizedPnl=0 if i == 0 else -1.25) for i in range(23)]
        self.tickers = [dict(symbol=p['symbol'], fairPrice=101, lastPrice=200,
                             fundingRate=0.000123,
                             timestamp=123456789) for p in self.positions]
        self.client = ExchangeClient.__new__(ExchangeClient)
        self.gateway = SimpleNamespace(
            contractPrivateGetPositionOpenPositions=AsyncMock(return_value={
                'success': True, 'data': self.positions}),
            contractPublicGetTicker=AsyncMock(return_value={'success': True, 'data': self.tickers}),
            load_markets=AsyncMock(),
            markets={f"COIN{i}/USDT:USDT": {'id': f"COIN{i}_USDT", 'contractSize': .1,
                                         'settle': 'USDT'} for i in range(23)},
            fetch_ticker=AsyncMock(side_effect=AssertionError('No per-symbol ticker reads')))
        self.client._exchange = self.gateway

    async def test_concurrent_callers_use_one_bulk_read_each_and_preserve_exchange_pnl(self):
        results = await asyncio.gather(*(self.client.get_positions() for _ in range(5)))
        self.assertEqual(self.gateway.contractPublicGetTicker.await_count, 5)
        self.assertEqual(self.gateway.contractPrivateGetPositionOpenPositions.await_count, 5)
        self.gateway.fetch_ticker.assert_not_awaited()
        for snapshot in results:
            self.assertEqual(len(snapshot), 23)
            self.assertEqual([p['unrealized_pnl'] for p in snapshot],
                             [p['unRealizedPnl'] for p in self.positions])
            self.assertEqual(snapshot[0]['mark_price'], 101)
            self.assertEqual(snapshot[0]['mark_price_source'], 'ticker.fairPrice')
            self.assertEqual(snapshot[0]['pnl_source'], 'position.unRealizedPnl')
            self.assertEqual(snapshot[0]['mark_price_timestamp_ms'], 123456789)
            self.assertEqual(snapshot[0]['funding_rate'], 0.000123)
            self.assertGreater(snapshot[0]['snapshot_received_at_ms'], 0)

    async def test_second_call_reads_changed_identity_size_and_price_without_cache(self):
        await self.client.get_positions()
        self.positions[0].update(positionId=999, holdVol=2)
        self.tickers[0]['fairPrice'] = 102
        snapshot = await self.client.get_positions()
        self.assertEqual((snapshot[0]['position_id'], snapshot[0]['contracts'],
                          snapshot[0]['mark_price']), (999, 2, 102))

    async def test_missing_pnl_is_calculated_from_fair_price_for_both_sides(self):
        for p in self.positions:
            p.pop('unRealizedPnl')
        self.positions[1]['positionType'] = 2
        snapshot = await self.client.get_positions()
        self.assertEqual(snapshot[0]['unrealized_pnl'], 1)
        self.assertEqual(snapshot[1]['unrealized_pnl'], -1)
        self.assertEqual(snapshot[0]['pnl_source'], 'calculated.fairPrice')

    async def test_rejection_missing_or_invalid_mark_fails_instead_of_zero_or_last(self):
        await self.client.get_positions()  # previous successful data must not hide the failure
        for response in ({'success': False, 'code': 510}, {'success': True, 'data': []},
                         {'success': True, 'data': {'symbol': 'COIN0_USDT'}},
                         {'success': True, 'data': [dict(self.tickers[0], fairPrice='NaN')]}):
            with self.subTest(response=response):
                self.gateway.contractPublicGetTicker.return_value = response
                with self.assertRaises(RuntimeError):
                    await self.client.get_positions()

    async def test_empty_positions_or_embedded_marks_do_not_fetch_tickers(self):
        for p in self.positions:
            p['markPrice'] = 99
        snapshot = await self.client.get_positions()
        self.assertEqual(snapshot[0]['mark_price_source'], 'position.markPrice')
        self.positions.clear()
        self.assertEqual(await self.client.get_positions(), [])
        self.gateway.contractPublicGetTicker.assert_not_awaited()

    async def test_invalid_embedded_mark_uses_fair_price_and_unknown_size_is_not_invented(self):
        for value in ('0', 'NaN', -1, 'invalid'):
            with self.subTest(mark=value):
                self.positions[0]['markPrice'] = value
                snapshot = await self.client.get_positions()
                self.assertEqual(snapshot[0]['mark_price'], 101)
        self.tickers[0]['fundingRate'] = 'NaN'
        self.assertNotIn('funding_rate', (await self.client.get_positions())[0])
        self.tickers[0]['fundingRate'] = 0
        self.assertEqual((await self.client.get_positions())[0]['funding_rate'], 0)
        self.gateway.markets.clear()
        with self.assertRaisesRegex(RuntimeError, 'contract size unavailable'):
            await self.client.get_positions()


if __name__ == '__main__':
    unittest.main()

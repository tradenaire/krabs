import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.exchange.client import ExchangeClient


class NativeOrdersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = ExchangeClient.__new__(ExchangeClient)
        self.get = AsyncMock()
        self.client._exchange = SimpleNamespace(contractPrivateGetStoporderListOrders=self.get)

    async def test_pages_active_only_keep_zero_volume_and_use_native_endpoint(self):
        rows = [dict(id=i, state=1, isFinished=0, symbol='BTC_USDT', positionId=42,
                     positionType=1, vol=0, volType=2, stopLossPrice=90, takeProfitPrice=120)
                for i in range(1, 102)]
        rows[1]['state'] = 2
        rows[2]['isFinished'] = 1
        self.get.side_effect = [{'success': True, 'data': rows[:100]},
                                {'success': True, 'data': rows[100:]}]
        with patch('bot.exchange.client.time.time', return_value=1_800_000_000):
            result = await self.client.get_native_stop_orders('BTC')
        self.assertEqual(len(result), 99)
        self.assertEqual(result[0]['vol'], 0)
        self.assertEqual(result[0]['volType'], 2)
        for page, call in enumerate(self.get.call_args_list, 1):
            self.assertEqual(call.args[0], dict(symbol='BTC_USDT', page_num=page, page_size=100,
                is_finished=0, start_time=1_800_000_000_000-90*86400_000, end_time=1_800_000_000_000))

    async def test_failure_or_overlapping_pages_never_returns_partial_snapshot(self):
        for response in ({'success': False, 'code': 510}, {'success': True, 'data': None},
                         {'success': True, 'data': [{}]}):
            self.get.return_value = response
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                await self.client.get_native_stop_orders()
        page = [dict(id=i, state=1, isFinished=0) for i in range(1, 101)]
        self.get.side_effect = [{'success': True, 'data': page}, {'success': True, 'data': page}]
        with self.assertRaisesRegex(RuntimeError, 'overlap'):
            await self.client.get_native_stop_orders()


if __name__ == '__main__':
    unittest.main()

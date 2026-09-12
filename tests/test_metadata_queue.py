import asyncio
import unittest
from unittest.mock import AsyncMock

from bot.exchange.client import ExchangeClient


class MetadataQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_loading_contract_metadata_does_not_delay_account_queue(self):
        client = ExchangeClient('dummy', 'dummy')
        client._metadata.fetch = AsyncMock(return_value={'success': True, 'data': [{
            'symbol': 'BTC_USDT', 'baseCoin': 'BTC', 'quoteCoin': 'USDT', 'settleCoin': 'USDT',
            'contractSize': .0001, 'priceUnit': .1, 'volUnit': 1, 'volScale': 0,
            'priceScale': 1, 'minVol': 1, 'maxVol': 10000, 'minLeverage': 1, 'maxLeverage': 200}]})
        client._exchange.fetch = AsyncMock(return_value={'success': True, 'data': [
            {'currency': 'USDT', 'availableBalance': 10, 'equity': 10}]})
        client._spot.fetch = AsyncMock(side_effect=AssertionError('No spot metadata needed'))
        try:
            markets = await client._exchange.load_markets()
            self.assertIn('BTC/USDT:USDT', markets)
            # Real CCXT throttle remains enabled: metadata debt used to delay this by 5 s.
            balance = await asyncio.wait_for(client.get_futures_balance(), timeout=1)
            self.assertEqual(balance['free']['USDT'], 10)
            self.assertEqual(client._metadata.fetch.await_count, 1)
            self.assertIn('/contract/detail', client._metadata.fetch.call_args.args[0])
            self.assertIn('/account/assets', client._exchange.fetch.call_args.args[0])
            self.assertEqual(client._exchange.fetch.await_count, 1)
            client._spot.fetch.assert_not_awaited()
            self.assertTrue(client._metadata.enableRateLimit)
            self.assertEqual(client._metadata.api['contract']['public']['get']['detail'], 100)
            client._metadata.contractPublicGetDetail = AsyncMock(return_value={'data': [{'symbol': 'BTC_USDT'}]})
            self.assertEqual(await client.get_contract_details(), [{'symbol': 'BTC_USDT'}])
            client._metadata.contractPublicGetDetail.assert_awaited_once_with()
        finally:
            await client.close()

    async def test_all_three_sessions_close(self):
        client = ExchangeClient('dummy', 'dummy')
        sessions = [c.session for c in (client._exchange, client._spot, client._metadata)]
        await client.close()
        self.assertTrue(all(s.closed for s in sessions))
        self.assertTrue(all(c.session is None for c in (client._exchange, client._spot, client._metadata)))


if __name__ == '__main__':
    unittest.main()

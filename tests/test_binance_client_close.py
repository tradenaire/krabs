import unittest

from bot.exchange.binance_client import BinanceClient


class BinanceClientCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_releases_threaded_dns_session(self):
        client = BinanceClient("dummy", "dummy", testnet=True)
        session = client._exchange.session

        await client.close()

        self.assertTrue(session.closed)
        self.assertIsNone(client._exchange._session)


if __name__ == "__main__":
    unittest.main()

import unittest

from bot.exchange.binance_client import BinanceClient


class BinanceClientCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_releases_threaded_dns_session(self):
        client = BinanceClient("dummy", "dummy", testnet=True)
        session = client._exchange.session

        await client.close()

        self.assertTrue(session.closed)
        self.assertIsNone(client._exchange._session)

    async def test_close_does_not_create_session_when_none_exists(self):
        client = BinanceClient("dummy", "dummy", testnet=True)

        await client.close()

        self.assertIsNone(client._exchange._session)

    async def test_session_property_stays_none_after_close(self):
        client = BinanceClient("dummy", "dummy", testnet=True)
        _ = client._exchange.session

        await client.close()

        self.assertIsNone(client._exchange.session)
        self.assertIsNone(client._exchange._session)


if __name__ == "__main__":
    unittest.main()

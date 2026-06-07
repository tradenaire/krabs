import unittest
from types import SimpleNamespace

from bot.ai.research_snapshot import build_research_snapshot, format_research_snapshot


class FakeExchange:
    async def fetch_order_book(self, symbol, limit=5):
        return {
            "bids": [[100.0, 3.0], [99.5, 1.0]],
            "asks": [[100.5, 2.0], [101.0, 1.5]],
        }

    async def fetch_open_interest(self, symbol):
        return {"openInterestAmount": 123456.0}


class FakeClient:
    def __init__(self):
        self._exchange = FakeExchange()
        self.calls = []

    async def get_futures_balance(self):
        self.calls.append("balance")
        return {
            "USDT": {"free": 125.5, "total": 140.0, "used": 14.5},
            "_raw": {"availableOpen": 125.5, "equity": 140.0},
        }

    async def get_positions(self):
        self.calls.append("positions")
        return [
            {
                "symbol": "BTC/USDT:USDT",
                "side": "short",
                "entry_price": 69000.0,
                "mark_price": 68000.0,
                "unrealized_pnl": 2.3,
                "percentage": 4.6,
                "leverage": 5,
                "margin": 10.0,
                "liquidation_price": 73000.0,
            }
        ]

    async def get_tp_sl_orders(self):
        self.calls.append("tp_sl")
        return [
            {"symbol": "BTC/USDT:USDT", "trigger_price": 65000.0, "trigger_type": 2, "side": 2}
        ]

    async def get_ticker(self, symbol):
        self.calls.append(f"ticker:{symbol}")
        return {"last": 100.2, "percentage": 11.4, "quoteVolume": 25000000.0}

    async def get_funding_rate(self, symbol):
        self.calls.append(f"funding:{symbol}")
        return {"rate": 0.00042, "next_funding_time": "2026-06-08T08:00:00Z", "symbol": symbol}


class ResearchSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_rich_exchange_snapshot_without_secrets(self):
        client = FakeClient()
        config = SimpleNamespace(
            exchange_provider="binance_testnet",
            binance_api_key="SECRET_KEY_SHOULD_NOT_LEAK",
            binance_secret="SECRET_VALUE_SHOULD_NOT_LEAK",
        )

        snapshot = await build_research_snapshot(
            client,
            config,
            [
                {
                    "symbol": "ETH/USDT:USDT",
                    "direction": "long",
                    "rsi": 31.5,
                    "daily_change_pct": -12.4,
                    "score": 55,
                    "reasons": ["oversold bounce"],
                }
            ],
            max_candidates=1,
        )
        text = format_research_snapshot(snapshot)

        self.assertIn("balance", client.calls)
        self.assertIn("positions", client.calls)
        self.assertIn("tp_sl", client.calls)
        self.assertIn("ticker:ETH/USDT:USDT", client.calls)
        self.assertIn("funding:ETH/USDT:USDT", client.calls)
        self.assertEqual(snapshot["provider"], "binance_testnet")
        self.assertEqual(snapshot["balance"]["free_usdt"], 125.5)
        self.assertEqual(snapshot["positions"][0]["symbol"], "BTC/USDT:USDT")
        self.assertEqual(snapshot["candidates"][0]["funding_rate"], 0.00042)
        self.assertEqual(snapshot["candidates"][0]["best_bid"], 100.0)
        self.assertEqual(snapshot["candidates"][0]["best_ask"], 100.5)
        self.assertEqual(snapshot["candidates"][0]["open_interest"], 123456.0)
        self.assertIn("BINANCE API SNAPSHOT", text)
        self.assertIn("provider=binance_testnet", text)
        self.assertIn("BTC short", text)
        self.assertIn("ETH LONG", text)
        self.assertIn("funding=+0.0420%", text)
        self.assertNotIn("SECRET", text)


if __name__ == "__main__":
    unittest.main()

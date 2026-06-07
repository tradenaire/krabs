import unittest

from bot.signals.symbols import resolve_signal_symbol


class FakeBinanceClient:
    def __init__(self):
        self._exchange = self
        self.markets = {
            "EPIC/USDT:USDT": {"id": "EPICUSDT", "active": True},
            "BTC/USDT:USDT": {"id": "BTCUSDT", "active": True},
        }

    def futures_symbol(self, symbol):
        if "/" not in symbol:
            return f"{symbol}/USDT:USDT"
        return symbol if ":USDT" in symbol else f"{symbol}:USDT"

    async def load_markets(self):
        return self.markets


class FakeMexcClient:
    def futures_symbol(self, symbol):
        return f"{symbol}_USDT"

    async def get_contract_details(self):
        return [
            {"symbol": "EPIC_USDT", "state": 0, "isHidden": False},
            {"symbol": "BTC_USDT", "state": 0, "isHidden": False},
        ]


class SignalSymbolResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_current_binance_market_without_hardcoding_exchange(self):
        symbol = await resolve_signal_symbol(FakeBinanceClient(), "EPICUSDT")
        self.assertEqual(symbol, "EPIC/USDT:USDT")

    async def test_resolves_current_mexc_contract_without_hardcoding_exchange(self):
        symbol = await resolve_signal_symbol(FakeMexcClient(), "EPIC/USDT")
        self.assertEqual(symbol, "EPIC_USDT")


if __name__ == "__main__":
    unittest.main()

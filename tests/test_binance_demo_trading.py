import importlib
import sys
import types
import unittest


class FakeBinanceUsdm:
    instances = []

    NetworkError = RuntimeError
    DDoSProtection = RuntimeError

    def __init__(self, config):
        self.config = config
        self.demo_enabled = False
        self.sandbox_called = False
        FakeBinanceUsdm.instances.append(self)

    def enable_demo_trading(self, enabled):
        self.demo_enabled = enabled

    def set_sandbox_mode(self, enabled):
        self.sandbox_called = enabled
        raise AssertionError("deprecated sandbox mode should not be used")


class BinanceDemoTradingTest(unittest.TestCase):
    def setUp(self):
        FakeBinanceUsdm.instances.clear()
        for name in [
            "bot.exchange.binance_client",
            "ccxt",
            "ccxt.async_support",
        ]:
            sys.modules.pop(name, None)

        ccxt_pkg = types.ModuleType("ccxt")
        ccxt_async = types.ModuleType("ccxt.async_support")
        ccxt_async.binanceusdm = FakeBinanceUsdm
        ccxt_async.NetworkError = RuntimeError
        ccxt_async.DDoSProtection = RuntimeError
        ccxt_pkg.async_support = ccxt_async
        sys.modules["ccxt"] = ccxt_pkg
        sys.modules["ccxt.async_support"] = ccxt_async

    def test_testnet_uses_binance_demo_trading_instead_of_sandbox(self):
        module = importlib.import_module("bot.exchange.binance_client")

        module.BinanceClient("key", "secret", testnet=True)

        exchange = FakeBinanceUsdm.instances[0]
        self.assertTrue(exchange.demo_enabled)
        self.assertFalse(exchange.sandbox_called)


if __name__ == "__main__":
    unittest.main()

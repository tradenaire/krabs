import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.exchange.binance_client import BinanceClient
from bot.signals.execution import execute_signal
from bot.signals.model import ParsedSignal, TpTarget


class FakeExchange:
    def __init__(self):
        self.orders = []

    async def load_markets(self):
        return None

    async def fetch_open_orders(self, symbol):
        return []

    def amount_to_precision(self, symbol, amount):
        return f"{float(amount):.8f}".rstrip("0").rstrip(".")

    def price_to_precision(self, symbol, price):
        return f"{float(price):.8f}".rstrip("0").rstrip(".")

    async def create_order(self, symbol, order_type, side, amount, price, params):
        order = {
            "id": str(len(self.orders) + 1),
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": amount,
            "price": price,
            "params": params,
            "info": {},
        }
        self.orders.append(order)
        return order


class FakeClient:
    def __init__(self):
        self.open_args = None
        self.multi_tpsl_args = None
        self.multi_tpsl_calls = []
        self.position = {"symbol": "EPIC/USDT:USDT", "side": "short", "contracts": 10.0, "entry_price": 0.2101}

    def futures_symbol(self, symbol):
        if "/" in symbol:
            return symbol
        return f"{symbol}/USDT:USDT"

    async def place_futures_order(self, symbol, side, amount_usdt, leverage, margin_mode=None):
        self.open_args = (symbol, side, amount_usdt, leverage, margin_mode)
        return {"id": "open-1", "price": 0.2101, "leverage": leverage}

    async def get_position(self, symbol):
        return dict(self.position, symbol=self.futures_symbol(symbol))

    async def set_multi_tp_sl(self, symbol, tp_targets, sl_price, pos_data=None):
        self.multi_tpsl_args = (symbol, tp_targets, sl_price, pos_data)
        self.multi_tpsl_calls.append(self.multi_tpsl_args)
        return [{"type": "TP"}, {"type": "TP"}, {"type": "TP"}, {"type": "SL"}]


class SignalExecutionTests(unittest.IsolatedAsyncioTestCase):
    def _signal(self):
        return ParsedSignal(
            symbol="EPIC",
            side="short",
            entry_min=0.2098,
            entry_max=0.2104,
            stop=0.2167,
            tps=(TpTarget(0.1978, 50), TpTarget(0.1942, 25), TpTarget(0.1903, 25)),
            leverage=3,
        )

    async def test_binance_multi_tp_places_three_reduce_only_tps_and_one_close_sl(self):
        client = BinanceClient("key", "secret", testnet=False)
        fake_exchange = FakeExchange()
        client._exchange = fake_exchange

        results = await client.set_multi_tp_sl(
            "EPIC",
            self._signal().tps,
            0.2167,
            pos_data={"side": "short", "contracts": 10.0},
        )

        self.assertEqual([r["type"] for r in results], ["TP", "TP", "TP", "SL"])
        self.assertEqual([o["type"] for o in fake_exchange.orders], ["TAKE_PROFIT_MARKET"] * 3 + ["STOP_MARKET"])
        self.assertEqual([o["side"] for o in fake_exchange.orders], ["buy", "buy", "buy", "buy"])
        self.assertEqual([o["amount"] for o in fake_exchange.orders[:3]], [5.0, 2.5, 2.5])
        for order in fake_exchange.orders[:3]:
            self.assertTrue(order["params"]["reduceOnly"])
            self.assertNotIn("closePosition", order["params"])
        self.assertIsNone(fake_exchange.orders[3]["amount"])
        self.assertTrue(fake_exchange.orders[3]["params"]["closePosition"])

    async def test_execute_signal_opens_with_signal_side_and_leverage(self):
        client = FakeClient()

        result = await execute_signal(client, app=None, signal=self._signal(), margin=2)

        self.assertEqual(client.open_args, ("EPIC/USDT:USDT", "sell", 2, 3, None))
        self.assertEqual(client.multi_tpsl_args[0], "EPIC/USDT:USDT")
        self.assertEqual(client.multi_tpsl_args[2], 0.2167)
        self.assertEqual(result["symbol"], "EPIC/USDT:USDT")
        self.assertEqual(result["orders"], 4)

    async def test_execute_signal_disables_default_exits_when_opening_through_service(self):
        client = FakeClient()
        app = SimpleNamespace(bot_data={"config": SimpleNamespace(default_leverage=5)})
        calls = []

        async def fake_execute_open(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return {"id": "open-1", "entry_price": 0.2101, "leverage": 3}

        with patch("bot.services.trading.execute_open", fake_execute_open):
            await execute_signal(client, app=app, signal=self._signal(), margin=2)

        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["kwargs"].get("setup_exits"), False)
        self.assertEqual(len(client.multi_tpsl_calls), 1)

    async def test_execute_signal_rejects_immediate_trigger_tp_before_multi_tp_call(self):
        client = FakeClient()
        client.position = {
            "symbol": "HYPE/USDT:USDT",
            "side": "short",
            "contracts": 100.0,
            "entry_price": 10.0,
            "mark_price": 9.8,
        }
        signal = ParsedSignal(
            symbol="HYPE",
            side="short",
            entry_min=9.9,
            entry_max=10.1,
            stop=10.5,
            tps=(TpTarget(10.1, 50), TpTarget(9.4, 25), TpTarget(9.0, 25)),
            leverage=5,
        )

        with self.assertRaisesRegex(RuntimeError, "TP1.*сработал бы сразу"):
            await execute_signal(client, app=None, signal=signal, margin=2)

        self.assertEqual(client.multi_tpsl_calls, [])


if __name__ == "__main__":
    unittest.main()

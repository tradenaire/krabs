import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.exchange.binance_client import BinanceClient
from bot.services.ladder import setup_on_open
from bot.signals.execution import execute_signal
from bot.signals.model import ParsedSignal, TpTarget


class FakeExchange:
    def __init__(self):
        self.orders = []
        self.algo_orders = []
        self.cancelled_orders = []
        self.cancelled_algo_symbols = []
        self.fail_algo_endpoint = False
        self.conditional_orders_as_algo = False

    async def load_markets(self):
        return None

    def market(self, symbol):
        return {"id": symbol.replace("/", "").replace(":USDT", "")}

    async def fetch_open_orders(self, symbol):
        return [
            order for order in self.orders
            if order.get("symbol") == symbol and order.get("open", True)
        ]

    async def cancel_order(self, order_id, symbol):
        self.cancelled_orders.append((order_id, symbol))
        for order in self.orders:
            if order.get("id") == order_id:
                order["open"] = False
        return {"id": order_id}

    async def fapiPrivateGetOpenAlgoOrders(self, params):
        if self.fail_algo_endpoint:
            raise RuntimeError("algo endpoint unavailable")
        symbol = params.get("symbol")
        return [
            order for order in self.algo_orders
            if order.get("symbol") == symbol and order.get("algoStatus", "NEW") == "NEW"
        ]

    async def fapiPrivateDeleteAlgoOpenOrders(self, params):
        if self.fail_algo_endpoint:
            raise RuntimeError("algo endpoint unavailable")
        symbol = params.get("symbol")
        self.cancelled_algo_symbols.append(symbol)
        count = 0
        for order in self.algo_orders:
            if order.get("symbol") == symbol and order.get("algoStatus", "NEW") == "NEW":
                order["algoStatus"] = "CANCELED"
                count += 1
        return {"success": True, "canceled": count}

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
        if self.conditional_orders_as_algo and order_type in ("TAKE_PROFIT_MARKET", "STOP_MARKET"):
            algo = {
                "algoId": str(len(self.algo_orders) + 1),
                "algoType": "CONDITIONAL",
                "orderType": order_type,
                "symbol": symbol.replace("/", "").replace(":USDT", ""),
                "side": side.upper(),
                "triggerPrice": str(params.get("stopPrice")),
                "closePosition": bool(params.get("closePosition")),
                "reduceOnly": bool(params.get("reduceOnly")),
                "algoStatus": "NEW",
            }
            self.algo_orders.append(algo)
            order["info"] = algo
        else:
            self.orders.append(order)
        return order


class BinanceAlgoReadbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_tp_sl_orders_reads_short_binance_open_algo_orders(self):
        client = BinanceClient("key", "secret", testnet=False)
        fake_exchange = FakeExchange()
        fake_exchange.algo_orders = [
            {
                "algoId": "tp1",
                "algoType": "CONDITIONAL",
                "orderType": "TAKE_PROFIT_MARKET",
                "symbol": "LIGHTUSDT",
                "side": "BUY",
                "triggerPrice": "0.10850",
                "reduceOnly": True,
            },
            {
                "algoId": "tp2",
                "algoType": "CONDITIONAL",
                "orderType": "TAKE_PROFIT_MARKET",
                "symbol": "LIGHTUSDT",
                "side": "BUY",
                "triggerPrice": "0.10280",
                "reduceOnly": True,
            },
            {
                "algoId": "tp3",
                "algoType": "CONDITIONAL",
                "orderType": "TAKE_PROFIT_MARKET",
                "symbol": "LIGHTUSDT",
                "side": "BUY",
                "triggerPrice": "0.09550",
                "reduceOnly": True,
            },
            {
                "algoId": "sl",
                "algoType": "CONDITIONAL",
                "orderType": "STOP_MARKET",
                "symbol": "LIGHTUSDT",
                "side": "BUY",
                "triggerPrice": "0.12180",
                "closePosition": True,
            },
        ]
        client._exchange = fake_exchange

        orders = await client.get_tp_sl_orders("LIGHT")

        self.assertEqual([order["id"] for order in orders], ["tp1", "tp2", "tp3", "sl"])
        self.assertEqual({order["symbol"] for order in orders}, {"LIGHT/USDT:USDT"})
        self.assertEqual([order["trigger_type"] for order in orders], [2, 2, 2, 1])
        self.assertEqual([order["trigger_price"] for order in orders], [0.1085, 0.1028, 0.0955, 0.1218])

    async def test_get_tp_sl_orders_reads_long_binance_open_algo_orders(self):
        client = BinanceClient("key", "secret", testnet=False)
        fake_exchange = FakeExchange()
        fake_exchange.algo_orders = [
            {
                "algoId": "tp1",
                "orderType": "TAKE_PROFIT_MARKET",
                "symbol": "RENDERUSDT",
                "side": "SELL",
                "triggerPrice": "2.50",
            },
            {
                "algoId": "sl",
                "orderType": "STOP_MARKET",
                "symbol": "RENDERUSDT",
                "side": "SELL",
                "triggerPrice": "1.10",
            },
        ]
        client._exchange = fake_exchange

        orders = await client.get_tp_sl_orders("RENDER")

        self.assertEqual([order["symbol"] for order in orders], ["RENDER/USDT:USDT", "RENDER/USDT:USDT"])
        self.assertEqual([order["trigger_type"] for order in orders], [1, 2])

    async def test_cancel_tp_sl_orders_cancels_normal_and_algo_orders(self):
        client = BinanceClient("key", "secret", testnet=False)
        fake_exchange = FakeExchange()
        fake_exchange.orders = [
            {"id": "normal-tp", "symbol": "LIGHT/USDT:USDT", "type": "TAKE_PROFIT_MARKET", "side": "buy"},
        ]
        fake_exchange.algo_orders = [
            {"algoId": "algo-tp", "orderType": "TAKE_PROFIT_MARKET", "symbol": "LIGHTUSDT", "side": "BUY"},
        ]
        client._exchange = fake_exchange

        count = await client.cancel_tp_sl_orders("LIGHT")

        self.assertEqual(count, 2)
        self.assertEqual(fake_exchange.cancelled_orders, [("normal-tp", "LIGHT/USDT:USDT")])
        self.assertEqual(fake_exchange.cancelled_algo_symbols, ["LIGHTUSDT"])

    async def test_cancel_tp_sl_orders_ignores_missing_algo_endpoint(self):
        client = BinanceClient("key", "secret", testnet=False)
        fake_exchange = FakeExchange()
        fake_exchange.fail_algo_endpoint = True
        fake_exchange.orders = [
            {"id": "normal-sl", "symbol": "LIGHT/USDT:USDT", "type": "STOP_MARKET", "side": "buy"},
        ]
        client._exchange = fake_exchange

        count = await client.cancel_tp_sl_orders("LIGHT")

        self.assertEqual(count, 1)
        self.assertEqual(fake_exchange.cancelled_orders, [("normal-sl", "LIGHT/USDT:USDT")])

    async def test_ladder_setup_accepts_binance_algo_readback(self):
        client = BinanceClient("key", "secret", testnet=False)
        fake_exchange = FakeExchange()
        fake_exchange.conditional_orders_as_algo = True
        client._exchange = fake_exchange
        app = SimpleNamespace(bot_data={"config": SimpleNamespace(tp_partial_pct=50)})
        calls = []

        async def fake_upsert(*args, **kwargs):
            calls.append((args, kwargs))

        with patch("bot.infra.db.upsert_tp_ladder", fake_upsert):
            await setup_on_open(
                client,
                app,
                symbol="LIGHT/USDT:USDT",
                side="short",
                entry=0.1145,
                leverage=20,
                contracts=1746.0,
                pick={"tp1": "0.1085", "tp2": "0.1028", "tp3": "0.0955", "sl": "0.1218"},
            )

        self.assertEqual(len(fake_exchange.algo_orders), 4)
        self.assertEqual(len(calls), 1)


class FakeClient:
    def __init__(self):
        self.open_args = None
        self.multi_tpsl_args = None
        self.multi_tpsl_calls = []
        self.close_calls = []
        self.tpsl_orders = [
            {"symbol": "EPIC/USDT:USDT", "trigger_price": 0.1978, "trigger_type": 2},
            {"symbol": "EPIC/USDT:USDT", "trigger_price": 0.1942, "trigger_type": 2},
            {"symbol": "EPIC/USDT:USDT", "trigger_price": 0.1903, "trigger_type": 2},
            {"symbol": "EPIC/USDT:USDT", "trigger_price": 0.2167, "trigger_type": 1},
        ]
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

    async def get_tp_sl_orders(self, symbol):
        fsym = self.futures_symbol(symbol)
        return [order for order in self.tpsl_orders if order.get("symbol") == fsym]

    async def close_futures_position(self, symbol):
        self.close_calls.append(symbol)
        return {"status": "closed"}


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

    async def test_execute_signal_records_ladder_for_balance_display(self):
        client = FakeClient()
        calls = []

        async def fake_upsert(*args, **kwargs):
            calls.append((args, kwargs))

        with patch("bot.infra.db.upsert_tp_ladder", fake_upsert):
            await execute_signal(client, app=None, signal=self._signal(), margin=2)

        self.assertEqual(len(calls), 1)
        args, _kwargs = calls[0]
        self.assertEqual(args[:4], ("EPIC/USDT:USDT", "short", 0.2101, 3))
        self.assertEqual(args[4:8], (0.1978, 0.1942, 0.1903, 0.2167))

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

    async def test_execute_signal_closes_fresh_entry_if_exchange_readback_has_no_tpsl(self):
        client = FakeClient()
        client.tpsl_orders = []

        with self.assertRaisesRegex(RuntimeError, "expected 3 TP"):
            await execute_signal(client, app=None, signal=self._signal(), margin=2)

        self.assertEqual(client.close_calls, ["EPIC/USDT:USDT"])


if __name__ == "__main__":
    unittest.main()

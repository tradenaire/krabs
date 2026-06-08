import unittest
from types import SimpleNamespace

from bot.services.ladder import compute_levels, rebuild, setup_on_open


class LadderFilteredTpTests(unittest.TestCase):
    def test_compute_levels_accepts_filtered_pick_with_two_remaining_tps(self):
        tps, sl = compute_levels(
            entry=100.0,
            leverage=10,
            side="long",
            pick={"tp1": "", "tp2": "115", "tp3": "130", "sl": "95"},
            config=SimpleNamespace(tp_ladder_pcts="50,120,250"),
        )

        self.assertEqual(tps, [115.0, 130.0])
        self.assertEqual(sl, 95.0)


class FakeLadderClient:
    def __init__(self):
        self.tp_calls = []
        self.sl_calls = []

    def futures_symbol(self, symbol):
        return symbol if "/" in symbol else f"{symbol}/USDT:USDT"

    async def place_reduce_sl(self, *args, **kwargs):
        self.sl_calls.append((args, kwargs))

    async def place_reduce_tp(self, *args, **kwargs):
        self.tp_calls.append((args, kwargs))

    async def get_tp_sl_orders(self, symbol):
        return []


class FakeRebuildClient:
    def __init__(self):
        self.orders = []
        self.cancel_calls = []

    async def cancel_tp_sl_orders(self, symbol):
        self.cancel_calls.append(symbol)
        self.orders.clear()

    async def place_reduce_sl(self, symbol, side, price, qty=None):
        trigger_type = 2 if side == "long" else 1
        self.orders.append({"trigger_price": price, "trigger_type": trigger_type})

    async def place_reduce_tp(self, symbol, side, qty, price):
        trigger_type = 1 if side == "long" else 2
        self.orders.append({"trigger_price": price, "trigger_type": trigger_type})

    async def get_tp_sl_orders(self, symbol):
        return self.orders


class LadderSetupValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_rejects_negative_sl_before_exchange_calls(self):
        client = FakeLadderClient()
        app = SimpleNamespace(bot_data={"config": SimpleNamespace(tp_ladder_pcts="50,120,250", sl_pct=500, tp_partial_pct=50)})

        with self.assertRaisesRegex(ValueError, "SL.*positive"):
            await setup_on_open(
                client,
                app,
                symbol="RENDER/USDT:USDT",
                side="long",
                entry=1.679,
                leverage=1,
                contracts=299,
                pick=None,
            )

        self.assertEqual(client.sl_calls, [])
        self.assertEqual(client.tp_calls, [])

    async def test_rebuild_allows_breakeven_sl_against_current_mark(self):
        client = FakeRebuildClient()
        app = SimpleNamespace(bot_data={"config": SimpleNamespace(tp_partial_pct=50)})

        await rebuild(
            client,
            app,
            symbol="EPIC/USDT:USDT",
            ladder={
                "side": "long",
                "entry_price": 100.0,
                "tp1": 110.0,
                "tp2": 120.0,
                "tp3": 130.0,
                "sl": 90.0,
                "filled1": 1,
                "filled2": 0,
                "filled3": 0,
            },
            contracts=2.0,
            breakeven=True,
            reference=111.0,
        )

        self.assertEqual(client.cancel_calls, ["EPIC/USDT:USDT"])
        self.assertIn({"trigger_price": 100.0, "trigger_type": 2}, client.orders)


if __name__ == "__main__":
    unittest.main()

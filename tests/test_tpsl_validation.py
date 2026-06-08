import unittest

from bot.services.tpsl import validate_exit_prices, verify_exit_orders


class TpslValidationTests(unittest.TestCase):
    def test_rejects_negative_long_sl_before_exchange_call(self):
        with self.assertRaisesRegex(ValueError, "SL.*positive"):
            validate_exit_prices(
                symbol="RENDER/USDT:USDT",
                side="long",
                reference=1.637,
                tp_prices=[2.5, 3.6, 5.8],
                sl_price=-6.716,
            )

    def test_rejects_long_tp_below_or_equal_reference(self):
        with self.assertRaisesRegex(ValueError, "TP1.*above"):
            validate_exit_prices(
                symbol="HOME/USDT:USDT",
                side="long",
                reference=0.02868,
                tp_prices=[0.028, 0.043, 0.063],
                sl_price=0.02,
            )

    def test_accepts_three_long_tps_and_one_sl_on_correct_sides(self):
        validate_exit_prices(
            symbol="HOME/USDT:USDT",
            side="long",
            reference=0.02868,
            tp_prices=[0.043, 0.063, 0.101],
            sl_price=0.02,
        )


class FakeOrderClient:
    def __init__(self, orders):
        self.orders = orders

    async def get_tp_sl_orders(self, symbol):
        return self.orders


class TpslVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_exact_three_tps_and_one_sl(self):
        client = FakeOrderClient([
            {"trigger_price": 1.2, "trigger_type": 1},
            {"trigger_price": 1.4, "trigger_type": 1},
            {"trigger_price": 1.6, "trigger_type": 1},
            {"trigger_price": 0.8, "trigger_type": 2},
        ])

        result = await verify_exit_orders(
            client,
            "EPIC/USDT:USDT",
            "long",
            tp_prices=[1.2, 1.4, 1.6],
            sl_price=0.8,
        )

        self.assertEqual(result["tp_count"], 3)
        self.assertEqual(result["sl_count"], 1)

    async def test_fails_when_exchange_has_no_orders(self):
        client = FakeOrderClient([])

        with self.assertRaisesRegex(RuntimeError, "expected 3 TP"):
            await verify_exit_orders(
                client,
                "EPIC/USDT:USDT",
                "long",
                tp_prices=[1.2, 1.4, 1.6],
                sl_price=0.8,
            )


if __name__ == "__main__":
    unittest.main()

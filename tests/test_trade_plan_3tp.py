import unittest
from bot.services.trade_plan import build_three_tp_plan, format_three_tp_plan


class ThreeTpPlanTests(unittest.TestCase):
    def test_short_plan_calculates_distances_and_profit(self):
        plan = build_three_tp_plan(
            symbol="EPIC/USDT:USDT",
            side="short",
            entry=100.0,
            reference=100.0,
            leverage=10,
            margin=10.0,
            tp_prices=[95.0, 90.0, 80.0],
            sl_price=105.0,
        )

        self.assertEqual([level.index for level in plan.levels], [1, 2, 3])
        self.assertEqual([level.share_pct for level in plan.levels], [50.0, 25.0, 25.0])
        self.assertEqual([round(level.pnl_pct, 1) for level in plan.levels], [50.0, 100.0, 200.0])
        self.assertEqual([round(level.profit_usdt, 2) for level in plan.levels], [2.5, 2.5, 5.0])
        self.assertEqual(round(plan.sl_pnl_pct, 1), -50.0)
        self.assertFalse(plan.filtered)

    def test_long_plan_keeps_only_valid_tps_from_live_reference(self):
        plan = build_three_tp_plan(
            symbol="EPIC/USDT:USDT",
            side="long",
            entry=100.0,
            reference=101.0,
            leverage=10,
            margin=10.0,
            tp_prices=[100.5, 115.0, 130.0],
            sl_price=95.0,
        )

        self.assertTrue(plan.filtered)
        self.assertEqual([level.index for level in plan.levels], [2, 3])
        self.assertEqual([round(level.share_pct, 1) for level in plan.levels], [50.0, 50.0])
        self.assertGreater(plan.levels[0].price, 101.0)

    def test_invalid_stop_blocks_plan(self):
        with self.assertRaisesRegex(ValueError, "SL"):
            build_three_tp_plan(
                symbol="EPIC/USDT:USDT",
                side="short",
                entry=100.0,
                reference=100.0,
                leverage=10,
                margin=10.0,
                tp_prices=[95.0, 90.0, 80.0],
                sl_price=99.0,
            )

    def test_formatter_includes_available_targets_and_warning(self):
        plan = build_three_tp_plan(
            symbol="EPIC/USDT:USDT",
            side="long",
            entry=100.0,
            reference=101.0,
            leverage=10,
            margin=10.0,
            tp_prices=[100.5, 115.0, 130.0],
            sl_price=95.0,
        )

        text = format_three_tp_plan(plan, title="Preview")

        self.assertIn("Preview", text)
        self.assertIn("TP2", text)
        self.assertIn("TP3", text)
        self.assertIn("SL", text)
        self.assertIn("only valid", text)
        self.assertNotIn("TP1:", text)

    def test_filtered_plan_preserves_existing_shares_when_rechecked(self):
        plan = build_three_tp_plan(
            symbol="EPIC/USDT:USDT",
            side="long",
            entry=100.0,
            reference=101.0,
            leverage=10,
            margin=10.0,
            tp_prices=[115.0, 130.0],
            tp_shares=[50.0, 50.0],
            sl_price=95.0,
        )

        self.assertEqual([round(level.share_pct, 1) for level in plan.levels], [50.0, 50.0])


if __name__ == "__main__":
    unittest.main()

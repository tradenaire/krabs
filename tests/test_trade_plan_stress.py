import unittest

from bot.services.trade_plan import build_three_tp_plan


class TradePlanStressTests(unittest.TestCase):
    def test_many_reference_prices_keep_only_valid_tps_and_normalize_shares(self):
        cases = [
            ("long", [105.0, 112.0, 125.0], 95.0, [99.0, 100.0, 106.0, 113.0, 124.0]),
            ("short", [95.0, 88.0, 75.0], 105.0, [101.0, 100.0, 94.0, 87.0, 76.0]),
        ]

        for side, tps, sl, references in cases:
            for reference in references:
                with self.subTest(side=side, reference=reference):
                    try:
                        plan = build_three_tp_plan(
                            symbol="EPIC/USDT:USDT",
                            side=side,
                            entry=100.0,
                            reference=reference,
                            leverage=10,
                            margin=10.0,
                            tp_prices=tps,
                            sl_price=sl,
                        )
                    except ValueError as e:
                        self.assertIn("No valid TP", str(e))
                        if side == "long":
                            self.assertTrue(all(tp <= reference for tp in tps))
                        else:
                            self.assertTrue(all(tp >= reference for tp in tps))
                        continue

                    if side == "long":
                        self.assertTrue(all(level.price > reference for level in plan.levels))
                    else:
                        self.assertTrue(all(level.price < reference for level in plan.levels))
                    self.assertAlmostEqual(sum(level.share_pct for level in plan.levels), 100.0, places=6)
                    self.assertTrue(all(level.profit_usdt >= 0 for level in plan.levels))


if __name__ == "__main__":
    unittest.main()

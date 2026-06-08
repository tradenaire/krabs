import unittest
from types import SimpleNamespace

from bot.services.ladder import compute_levels


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


if __name__ == "__main__":
    unittest.main()

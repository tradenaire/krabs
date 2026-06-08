import unittest

from bot.engines.ladder import _expected_open_tp_count, _next_unfilled_tp_slots


class LadderEngineFilteredSlotsTests(unittest.TestCase):
    def test_expected_open_count_ignores_empty_filtered_tp_slots(self):
        ladder = {
            "tp1": 0.0,
            "tp2": 115.0,
            "tp3": 130.0,
            "filled1": 0,
            "filled2": 0,
            "filled3": 0,
        }

        self.assertEqual(_expected_open_tp_count(ladder), 2)
        self.assertEqual(_next_unfilled_tp_slots(ladder, 1), [2])

    def test_expected_open_count_respects_real_filled_slots(self):
        ladder = {
            "tp1": 0.0,
            "tp2": 115.0,
            "tp3": 130.0,
            "filled1": 0,
            "filled2": 1,
            "filled3": 0,
        }

        self.assertEqual(_expected_open_tp_count(ladder), 1)
        self.assertEqual(_next_unfilled_tp_slots(ladder, 1), [3])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from bot.services.protection import classify_protection, protection_summary_line


class ProtectionAuditTests(unittest.TestCase):
    def test_classifies_missing_partial_single_and_ladder_protection(self):
        long_pos = {"symbol": "H/USDT:USDT", "side": "long"}

        missing = classify_protection(long_pos, [], db_records=[{"symbol": "H/USDT:USDT"}])
        self.assertEqual(missing.status, "MISSING_ALL")
        self.assertEqual(missing.tp_count, 0)
        self.assertEqual(missing.sl_count, 0)
        self.assertTrue(missing.needs_repair)

        single = classify_protection(
            {"symbol": "EPIC/USDT:USDT", "side": "long"},
            [
                {"symbol": "EPIC/USDT:USDT", "trigger_type": 1, "trigger_price": 0.58},
                {"symbol": "EPIC/USDT:USDT", "trigger_type": 2, "trigger_price": 0.35},
            ],
            db_records=[{"symbol": "EPIC/USDT:USDT"}],
        )
        self.assertEqual(single.status, "OK_1TP_1SL")
        self.assertFalse(single.needs_repair)

        ladder = classify_protection(
            {"symbol": "AKE/USDT:USDT", "side": "long"},
            [
                {"symbol": "AKE/USDT:USDT", "trigger_type": 1, "trigger_price": 0.000282},
                {"symbol": "AKE/USDT:USDT", "trigger_type": 1, "trigger_price": 0.000296},
                {"symbol": "AKE/USDT:USDT", "trigger_type": 1, "trigger_price": 0.000315},
                {"symbol": "AKE/USDT:USDT", "trigger_type": 2, "trigger_price": 0.000247},
            ],
            db_records=[{"symbol": "AKE/USDT:USDT"}],
        )
        self.assertEqual(ladder.status, "OK_3TP_1SL")
        self.assertFalse(ladder.needs_repair)

        partial = classify_protection(
            {"symbol": "FIDA/USDT:USDT", "side": "long"},
            [{"symbol": "FIDA/USDT:USDT", "trigger_type": 1, "trigger_price": 0.0264}],
            db_records=[{"symbol": "FIDA/USDT:USDT"}],
        )
        self.assertEqual(partial.status, "PARTIAL")
        self.assertTrue(partial.needs_repair)

    def test_classifies_db_missing_and_duplicate(self):
        missing_db = classify_protection(
            {"symbol": "H/USDT:USDT", "side": "long"},
            [],
            db_records=[],
        )
        self.assertEqual(missing_db.status, "DB_MISSING")
        self.assertTrue(missing_db.needs_repair)

        duplicate = classify_protection(
            {"symbol": "AKE/USDT:USDT", "side": "long"},
            [],
            db_records=[{"symbol": "AKE/USDT:USDT"}, {"symbol": "AKE/USDT:USDT"}],
        )
        self.assertEqual(duplicate.status, "DB_DUPLICATE")
        self.assertTrue(duplicate.needs_repair)

    def test_summary_line_is_specific(self):
        missing = classify_protection(
            {"symbol": "H/USDT:USDT", "side": "long"},
            [],
            db_records=[{"symbol": "H/USDT:USDT"}],
        )
        self.assertIn("0 TP / 0 SL", protection_summary_line(missing))
        self.assertIn("требуется repair", protection_summary_line(missing))

        single = classify_protection(
            {"symbol": "EPIC/USDT:USDT", "side": "long"},
            [
                {"symbol": "EPIC/USDT:USDT", "trigger_type": 1, "trigger_price": 0.58},
                {"symbol": "EPIC/USDT:USDT", "trigger_type": 2, "trigger_price": 0.35},
            ],
            db_records=[{"symbol": "EPIC/USDT:USDT"}],
        )
        self.assertIn("single mode: 1 TP / 1 SL", protection_summary_line(single))


if __name__ == "__main__":
    unittest.main()

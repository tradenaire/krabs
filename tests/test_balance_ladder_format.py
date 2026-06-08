import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.pos_format import format_position_block


class BalanceLadderFormatTests(unittest.TestCase):
    def _pos(self):
        return {
            "symbol": "EPIC/USDT:USDT",
            "side": "short",
            "entry_price": 100.0,
            "mark_price": 96.0,
            "liquidation_price": 150.0,
            "unrealized_pnl": 4.0,
            "percentage": 40.0,
            "margin": 10.0,
            "leverage": 10,
        }

    def test_position_block_prefers_active_ladder_and_breakeven_sl(self):
        ladder = {
            "symbol": "EPIC/USDT:USDT",
            "side": "short",
            "entry_price": 100.0,
            "leverage": 10,
            "tp1": 95.0,
            "tp2": 90.0,
            "tp3": 80.0,
            "sl": 105.0,
            "filled1": 1,
            "filled2": 0,
            "filled3": 0,
            "sl_at_breakeven": 1,
        }
        config = SimpleNamespace(
            max_reentry_cycles=3,
            averaging_enabled=True,
            averaging_threshold=-100,
            averaging_amount=0.5,
            max_averaging_count=10,
        )

        with patch("bot.db.get_tp_ladder", return_value=ladder):
            text = format_position_block(
                self._pos(),
                db_rec={"tp_pct": 500, "sl_pct": 500, "averaging_count": 0, "total_invested": 10.0},
                re_rec=None,
                config=config,
                tp_sl_pcts={},
            )

        self.assertIn("TP1", text)
        self.assertIn("TP2", text)
        self.assertIn("TP3", text)
        self.assertIn("filled", text)
        self.assertIn("breakeven", text)
        self.assertIn("100", text)

    def test_position_block_states_when_averaging_disabled(self):
        config = SimpleNamespace(
            max_reentry_cycles=3,
            averaging_enabled=False,
            averaging_threshold=-100,
            averaging_amount=0.5,
            max_averaging_count=10,
        )

        with patch("bot.db.get_tp_ladder", return_value=None):
            text = format_position_block(
                self._pos(),
                db_rec={"tp_pct": 500, "sl_pct": 500, "averaging_count": 0, "total_invested": 10.0},
                re_rec=None,
                config=config,
                tp_sl_pcts={},
            )

        self.assertIn("Averaging: disabled", text)
        self.assertNotIn("PnL <=", text)


if __name__ == "__main__":
    unittest.main()

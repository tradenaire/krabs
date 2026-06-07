import unittest

from bot.signals.parser import SignalParseError, parse_signal
from bot.signals.preview import build_signal_confirmation_text


class SignalParserTests(unittest.TestCase):
    def test_parses_chartscan_short_signal_with_three_tps(self):
        signal = parse_signal(
            """
            EPIC USDT
            SHORT
            Medium 66%
            1:2.0 3x
            Entry
            $0.2098 - $0.211
            Stop
            $0.2167
            TP1 $0.1978 TP2 $0.1942 TP3 $0.1903
            """
        )

        self.assertEqual(signal.symbol, "EPIC")
        self.assertEqual(signal.side, "short")
        self.assertEqual(signal.entry_min, 0.2098)
        self.assertEqual(signal.entry_max, 0.211)
        self.assertEqual(signal.stop, 0.2167)
        self.assertEqual([tp.price for tp in signal.tps], [0.1978, 0.1942, 0.1903])
        self.assertEqual([tp.share_pct for tp in signal.tps], [50.0, 25.0, 25.0])
        self.assertEqual(signal.leverage, 3)
        self.assertEqual(signal.confidence, 66)

    def test_parses_russian_labels_and_validates_short_geometry(self):
        signal = parse_signal(
            """
            EPIC/USDT
            ШОРТ
            Вход 0.2098 - 0.2104
            Стоп 0.2167
            ТП1 0.1978
            ТП2 0.1942
            ТП3 0.1903
            x3
            """
        )

        self.assertAlmostEqual(signal.entry_mid, 0.2101)
        self.assertEqual(signal.order_side, "sell")

    def test_rejects_signal_with_stop_on_wrong_side(self):
        with self.assertRaises(SignalParseError):
            parse_signal(
                """
                EPIC USDT SHORT
                Entry 0.2100
                SL 0.2000
                TP1 0.1978 TP2 0.1942 TP3 0.1903
                """
            )

    def test_confirmation_text_matches_trade_review_shape(self):
        signal = parse_signal(
            """
            EPIC USDT SHORT
            Entry 0.2098 / 0.2104
            SL 0.2167
            TP1 0.1978 TP2 0.1942 TP3 0.1903
            3x
            Confidence 96%
            """
        )

        text = build_signal_confirmation_text(signal, margin=1)

        self.assertIn("Проверь распознанный сигнал:", text)
        self.assertIn("EPIC SHORT", text)
        self.assertIn("Entry: 0.2098 / 0.2104", text)
        self.assertIn("SL: 0.2167", text)
        self.assertIn("TP1: 0.1978 — 50%", text)
        self.assertIn("TP2: 0.1942 — 25%", text)
        self.assertIn("TP3: 0.1903 — 25%", text)
        self.assertIn("Margin: $1", text)
        self.assertIn("Leverage in signal: x3", text)
        self.assertIn("Confidence: 96%", text)


if __name__ == "__main__":
    unittest.main()

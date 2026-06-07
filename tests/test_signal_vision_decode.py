import unittest

from bot.signals.vision import build_signal_variables, parse_vision_signal_json


class SignalVisionDecodeTests(unittest.TestCase):
    def test_parses_model_json_into_valid_signal_variables(self):
        result = parse_vision_signal_json(
            """
            ```json
            {
              "symbol": "EPIC",
              "side": "SHORT",
              "entry_min": 0.2098,
              "entry_max": 0.2104,
              "sl": 0.2167,
              "tps": [
                {"price": 0.1978, "share_pct": 50},
                {"price": 0.1942, "share_pct": 25},
                {"price": 0.1903, "share_pct": 25}
              ],
              "leverage": 3,
              "confidence": 96,
              "warning": "low contrast screenshot"
            }
            ```
            """
        )

        self.assertEqual(result.signal.symbol, "EPIC")
        self.assertEqual(result.signal.side, "short")
        self.assertEqual(result.signal.entry_min, 0.2098)
        self.assertEqual(result.signal.entry_max, 0.2104)
        self.assertEqual(result.signal.stop, 0.2167)
        self.assertEqual([tp.price for tp in result.signal.tps], [0.1978, 0.1942, 0.1903])
        self.assertEqual([tp.share_pct for tp in result.signal.tps], [50.0, 25.0, 25.0])
        self.assertEqual(result.warning, "low contrast screenshot")

    def test_builds_order_variables_for_execution_algorithm(self):
        result = parse_vision_signal_json(
            """
            {"symbol":"EPIC","side":"SHORT","entry":[0.2098,0.2104],
             "stop":0.2167,"tps":[0.1978,0.1942,0.1903],"leverage":3}
            """
        )

        variables = build_signal_variables(result.signal, margin=2)

        self.assertEqual(variables["symbol"], "EPIC")
        self.assertEqual(variables["order_side"], "sell")
        self.assertEqual(variables["close_side"], "buy")
        self.assertEqual(variables["margin"], 2)
        self.assertEqual(variables["take_profit_orders"][0], {"price": 0.1978, "share_pct": 50.0})
        self.assertEqual(variables["stop_loss_order"], {"price": 0.2167, "close_position": True})

    def test_accepts_model_leverage_and_confidence_strings(self):
        result = parse_vision_signal_json(
            """
            {"symbol":"EPIC","side":"SHORT","entry":[0.2098,0.2104],
             "stop":0.2167,"tps":[0.1978,0.1942,0.1903],
             "leverage":"3x","confidence":"96%"}
            """
        )

        self.assertEqual(result.signal.leverage, 3)
        self.assertEqual(result.signal.confidence, 96)


if __name__ == "__main__":
    unittest.main()

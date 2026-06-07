import unittest

from bot.signals.model import ParsedSignal, TpTarget
from bot.signals.store import clear_signal, get_signal, save_signal


class SignalStoreTests(unittest.TestCase):
    def test_saves_retrieves_and_clears_signal_in_user_data(self):
        user_data = {}
        signal = ParsedSignal(
            symbol="EPIC",
            side="short",
            entry_min=0.2098,
            entry_max=0.2104,
            stop=0.2167,
            tps=(TpTarget(0.1978, 50), TpTarget(0.1942, 25), TpTarget(0.1903, 25)),
            leverage=3,
        )

        signal_id = save_signal(user_data, signal)

        self.assertLessEqual(len(signal_id), 12)
        self.assertIs(get_signal(user_data, signal_id), signal)
        clear_signal(user_data, signal_id)
        self.assertIsNone(get_signal(user_data, signal_id))


if __name__ == "__main__":
    unittest.main()

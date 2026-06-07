import unittest

from bot.handlers.signals import prepare_signal_confirmation
from bot.signals.store import get_signal


class SignalHandlerFormatTests(unittest.TestCase):
    def test_prepares_confirmation_with_margin_buttons_and_pending_signal(self):
        user_data = {}

        signal_id, text, keyboard = prepare_signal_confirmation(
            """
            EPIC USDT SHORT
            Entry 0.2098 / 0.2104
            SL 0.2167
            TP1 0.1978 TP2 0.1942 TP3 0.1903
            3x
            Confidence 96%
            """,
            user_data,
        )

        self.assertIsNotNone(get_signal(user_data, signal_id))
        self.assertIn("Проверь распознанный сигнал:", text)
        button_texts = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertIn("Открыть $1", button_texts)
        self.assertIn("Открыть $2", button_texts)
        self.assertIn("Открыть $5", button_texts)
        self.assertIn("Открыть $10", button_texts)
        self.assertIn("Проверить/исправить", button_texts)
        self.assertIn("Отмена", button_texts)


if __name__ == "__main__":
    unittest.main()

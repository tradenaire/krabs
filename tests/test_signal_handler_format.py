import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.handlers.signals import format_vision_decode_warning, prepare_signal_confirmation, signal_callback
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
            warning="model is unsure about TP3",
        )

        self.assertIsNotNone(get_signal(user_data, signal_id))
        self.assertIn("Проверь распознанный сигнал:", text)
        self.assertIn("⚠️ Предупреждение: model is unsure about TP3", text)
        button_texts = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertIn("Открыть $1", button_texts)
        self.assertIn("Открыть $2", button_texts)
        self.assertIn("Открыть $5", button_texts)
        self.assertIn("Открыть $10", button_texts)
        self.assertIn("Проверить/исправить", button_texts)
        self.assertIn("Отмена", button_texts)

    def test_formats_vision_errors_in_plain_russian_without_raw_exception(self):
        text = format_vision_decode_warning(
            ValueError("invalid literal for int() with base 10: '3x'"),
            "openai/gpt-5.5",
        )

        self.assertIn("⚠️", text)
        self.assertIn("Не открываю сделку по скрину", text)
        self.assertIn("Пришли сигнал текстом", text)
        self.assertNotIn("invalid literal", text)


class SignalCallbackFormatTests(unittest.IsolatedAsyncioTestCase):
    async def test_signal_open_success_lists_each_tp_and_profit(self):
        user_data = {}
        signal_id, _text, _keyboard = prepare_signal_confirmation(
            """
            EPIC USDT SHORT
            Entry 100
            SL 105
            TP1 95
            TP2 90
            TP3 80
            10x
            """,
            user_data,
        )

        class FakeQuery:
            def __init__(self):
                self.data = f"sig_open:{signal_id}:10"
                self.edits = []

            async def answer(self, *args, **kwargs):
                return None

            async def edit_message_text(self, text, **kwargs):
                self.edits.append((text, kwargs))

        async def fake_execute_signal(*args, **kwargs):
            signal = kwargs["signal"] if "signal" in kwargs else args[2]
            return {
                "symbol": "EPIC/USDT:USDT",
                "side": signal.side,
                "margin": 10.0,
                "leverage": 10,
                "entry_price": 100.0,
                "tps": signal.tps,
                "sl_price": signal.stop,
                "orders": 4,
            }

        q = FakeQuery()
        context = SimpleNamespace(user_data=user_data, bot_data={"exchange": object()}, application=None)
        update = SimpleNamespace(callback_query=q)

        with patch("bot.handlers.signals.execute_signal", fake_execute_signal):
            await signal_callback(update, context)

        final_text = q.edits[-1][0]
        self.assertIn("TP1", final_text)
        self.assertIn("TP2", final_text)
        self.assertIn("TP3", final_text)
        self.assertIn("SL", final_text)
        self.assertIn("profit", final_text)

    async def test_signal_open_filters_bad_tp_and_asks_confirmation(self):
        user_data = {}
        signal_id, _text, _keyboard = prepare_signal_confirmation(
            """
            EPIC USDT LONG
            Entry 100
            SL 95
            TP1 100.5
            TP2 115
            TP3 130
            10x
            """,
            user_data,
        )

        class FakeQuery:
            def __init__(self):
                self.data = f"sig_open:{signal_id}:10"
                self.edits = []

            async def answer(self, *args, **kwargs):
                return None

            async def edit_message_text(self, text, **kwargs):
                self.edits.append((text, kwargs))

        class FakeClient:
            async def get_ticker(self, symbol):
                return {"last": 101.0}

            async def _exchange(self):
                return None

        async def fake_execute_signal(*args, **kwargs):
            raise AssertionError("execute_signal should wait for filtered confirmation")

        q = FakeQuery()
        context = SimpleNamespace(
            user_data=user_data,
            bot_data={
                "exchange": FakeClient(),
                "config": SimpleNamespace(tp_ladder_pcts="50,120,250"),
            },
            application=None,
        )
        update = SimpleNamespace(callback_query=q)

        with patch("bot.handlers.signals.execute_signal", fake_execute_signal):
            await signal_callback(update, context)

        text, kwargs = q.edits[-1]
        self.assertIn("only valid", text)
        self.assertNotIn("TP1:", text)
        self.assertIn("TP2", text)
        self.assertIn("TP3", text)
        keyboard = kwargs["reply_markup"]
        self.assertIn("sig_filtered_open", keyboard.inline_keyboard[0][0].callback_data)

    async def test_signal_open_blocks_when_no_valid_tp_remains(self):
        user_data = {}
        signal_id, _text, _keyboard = prepare_signal_confirmation(
            """
            EPIC USDT LONG
            Entry 100
            SL 95
            TP1 101
            TP2 102
            TP3 103
            10x
            """,
            user_data,
        )

        class FakeQuery:
            def __init__(self):
                self.data = f"sig_open:{signal_id}:10"
                self.edits = []

            async def answer(self, *args, **kwargs):
                return None

            async def edit_message_text(self, text, **kwargs):
                self.edits.append((text, kwargs))

        class FakeClient:
            async def get_ticker(self, symbol):
                return {"last": 150.0}

        async def fake_execute_signal(*args, **kwargs):
            raise AssertionError("execute_signal should not open when no TP remains")

        q = FakeQuery()
        context = SimpleNamespace(
            user_data=user_data,
            bot_data={"exchange": FakeClient(), "config": SimpleNamespace(tp_ladder_pcts="50,120,250")},
            application=None,
        )
        update = SimpleNamespace(callback_query=q)

        with patch("bot.handlers.signals.execute_signal", fake_execute_signal):
            await signal_callback(update, context)

        self.assertIn("No valid TP", q.edits[-1][0])

    async def test_filtered_signal_confirm_rechecks_market_before_opening(self):
        user_data = {}
        signal_id, _text, _keyboard = prepare_signal_confirmation(
            """
            EPIC USDT LONG
            Entry 100
            SL 95
            TP1 115
            TP2 130
            10x
            """,
            user_data,
        )

        class FakeQuery:
            def __init__(self):
                self.data = f"sig_filtered_open:{signal_id}:10"
                self.edits = []

            async def answer(self, *args, **kwargs):
                return None

            async def edit_message_text(self, text, **kwargs):
                self.edits.append((text, kwargs))

        class FakeClient:
            async def get_ticker(self, symbol):
                return {"last": 120.0}

        async def fake_execute_signal(*args, **kwargs):
            raise AssertionError("execute_signal should wait for updated filtered confirmation")

        q = FakeQuery()
        context = SimpleNamespace(
            user_data=user_data,
            bot_data={"exchange": FakeClient(), "config": SimpleNamespace(tp_ladder_pcts="50,120,250")},
            application=None,
        )
        update = SimpleNamespace(callback_query=q)

        with patch("bot.handlers.signals.execute_signal", fake_execute_signal):
            await signal_callback(update, context)

        text, kwargs = q.edits[-1]
        self.assertIn("only valid", text)
        self.assertNotIn("TP1:", text)
        self.assertIn("TP2", text)
        self.assertIn("sig_filtered_open", kwargs["reply_markup"].inline_keyboard[0][0].callback_data)


if __name__ == "__main__":
    unittest.main()

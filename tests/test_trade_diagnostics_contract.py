from __future__ import annotations

import unittest
from unittest.mock import patch

from bot.handlers.scan import open_callback
from bot.handlers.signals import prepare_signal_confirmation, signal_callback
from bot.handlers.trading import (
    close_final_callback,
    close_reentry_callback,
    min_open_callback,
    repair_tpsl_callback,
    repair_tpsl_handler,
    short_handler,
)
from bot.handlers.monitor_callbacks import monitor_close_confirm_callback
from bot.handlers.assistant import assistant_handler, nlp_close_callback

from tests.tg_harness import (
    FakeCallbackQuery,
    FakeConfig,
    FakeFuturesClient,
    FakeTelegramContext,
    FakeTelegramMessage,
    FakeTelegramUpdate,
)


RAW_TP_IMMEDIATE = (
    'TP1 place failed for HYPE/USDT:USDT: binanceusdm '
    '{"code":-2021,"msg":"Order would immediately trigger."}'
)

RAW_EXISTING_CLOSE_POSITION = (
    'SL place failed for RENDER/USDT:USDT: binanceusdm '
    '{"code":-4130,"msg":"An open stop or take profit order with GTE and closePosition in the direction is existing."}'
)


def _assert_no_raw_binance_leak(testcase: unittest.TestCase, text: str) -> None:
    testcase.assertNotIn('{"code"', text)
    testcase.assertNotIn("binanceusdm", text)
    testcase.assertNotIn("Order would immediately trigger", text)
    testcase.assertNotIn("closePosition in the direction is existing", text)


class TradeDiagnosticsContractTests(unittest.IsolatedAsyncioTestCase):
    def _context(self) -> FakeTelegramContext:
        return FakeTelegramContext(
            bot_data={
                "config": FakeConfig(),
                "exchange": FakeFuturesClient(),
            }
        )

    async def test_signal_open_callback_hides_raw_binance_error_from_user(self):
        context = self._context()
        signal_id, _text, _keyboard = prepare_signal_confirmation(
            """
            HYPE USDT SHORT
            Entry 9.90 / 10.10
            SL 10.50
            TP1 9.40 TP2 9.10 TP3 8.80
            5x
            """,
            context.user_data,
        )
        query = FakeCallbackQuery(f"sig_open:{signal_id}:1")
        update = FakeTelegramUpdate(callback_query=query)

        async def fail_open(*args, **kwargs):
            raise RuntimeError(RAW_TP_IMMEDIATE)

        with patch("bot.handlers.signals.execute_signal", fail_open):
            await signal_callback(update, context)

        user_text = query.edits[-1][0]
        self.assertIn("HYPE", user_text)
        self.assertIn("TP1", user_text)
        self.assertIn("сработал бы сразу", user_text)
        _assert_no_raw_binance_leak(self, user_text)

    async def test_manual_short_command_hides_existing_close_position_error(self):
        context = self._context()
        context.args = ["RENDER", "1", "x5"]
        message = FakeTelegramMessage("/short RENDER 1 x5")
        update = FakeTelegramUpdate(message=message)

        async def resolve_symbol(client, ticker):
            return "RENDER/USDT:USDT"

        async def fail_open(*args, **kwargs):
            raise RuntimeError(RAW_EXISTING_CLOSE_POSITION)

        with patch("bot.ai.scanner.mexc_find_futures_symbol", resolve_symbol), \
             patch("bot.handlers.trading.execute_open", fail_open):
            await short_handler(update, context)

        user_text = message.replies[-1][0]
        self.assertIn("RENDER", user_text)
        self.assertIn("уже есть защитный стоп/тейк", user_text)
        self.assertIn("старые TP/SL", user_text)
        _assert_no_raw_binance_leak(self, user_text)

    async def test_min_open_confirmation_hides_raw_binance_error_from_user(self):
        context = self._context()
        context.user_data["pending_min_open"] = {
            "symbol": "RENDER/USDT:USDT",
            "side": "sell",
            "margin": 1.0,
            "leverage": 5,
            "tp_pct": 500.0,
            "sl_pct": 500.0,
        }
        query = FakeCallbackQuery("min_open_yes")
        update = FakeTelegramUpdate(callback_query=query)

        async def fail_open(*args, **kwargs):
            raise RuntimeError(RAW_EXISTING_CLOSE_POSITION)

        with patch("bot.handlers.trading.execute_open", fail_open):
            await min_open_callback(update, context)

        user_text = query.edits[-1][0]
        self.assertIn("RENDER", user_text)
        self.assertIn("уже есть защитный стоп/тейк", user_text)
        _assert_no_raw_binance_leak(self, user_text)

    async def test_scan_open_button_hides_raw_binance_error_from_user(self):
        context = self._context()
        query = FakeCallbackQuery("open_sell_RENDER/USDT:USDT")
        update = FakeTelegramUpdate(callback_query=query)

        async def fail_execute(*args, **kwargs):
            raise RuntimeError(RAW_EXISTING_CLOSE_POSITION)

        with patch("bot.handlers.scan._do_execute_open", fail_execute):
            await open_callback(update, context)

        user_text = query.message.replies[-1][0]
        self.assertIn("RENDER", user_text)
        self.assertIn("уже есть защитный стоп/тейк", user_text)
        _assert_no_raw_binance_leak(self, user_text)

    async def test_nlp_open_text_hides_raw_binance_error_from_user(self):
        context = self._context()
        message = FakeTelegramMessage("открой RENDER")
        update = FakeTelegramUpdate(message=message)

        async def fail_open(*args, **kwargs):
            raise RuntimeError(RAW_EXISTING_CLOSE_POSITION)

        with patch("bot.handlers.trading.execute_open", fail_open):
            await assistant_handler(update, context)

        user_text = message.replies[-1][0]
        self.assertIn("RENDER", user_text)
        self.assertIn("уже есть защитный стоп/тейк", user_text)
        _assert_no_raw_binance_leak(self, user_text)

    async def test_repair_tpsl_preview_does_not_mutate_exchange(self):
        class RepairClient(FakeFuturesClient):
            def __init__(self):
                self.cancel_calls = []
                self.sl_calls = []
                self.tp_calls = []

            async def get_positions(self):
                return [{
                    "symbol": "H/USDT:USDT",
                    "side": "long",
                    "entry_price": 0.12088,
                    "mark_price": 0.1664,
                    "contracts": 1654.0,
                    "leverage": 20,
                    "margin": 13.76,
                }]

            async def get_tp_sl_orders(self, symbol=None):
                return []

            async def cancel_tp_sl_orders(self, symbol):
                self.cancel_calls.append(symbol)
                return 0

            async def place_reduce_sl(self, *args, **kwargs):
                self.sl_calls.append((args, kwargs))

            async def place_reduce_tp(self, *args, **kwargs):
                self.tp_calls.append((args, kwargs))

        client = RepairClient()
        context = FakeTelegramContext(
            args=["H"],
            bot_data={"config": FakeConfig(tp_ladder_pcts="50,120,250", sl_pct=500), "exchange": client},
        )
        message = FakeTelegramMessage("/repair_tpsl H")
        update = FakeTelegramUpdate(message=message)

        await repair_tpsl_handler(update, context)

        text = message.replies[-1][0]
        self.assertIn("Repair preview", text)
        self.assertIn("H LONG", text)
        self.assertIn("сейчас: 0 TP / 0 SL", text)
        self.assertIn("будет: 3 TP / 1 SL", text)
        self.assertIn("Confirm repair", text)
        self.assertEqual(client.cancel_calls, [])
        self.assertEqual(client.sl_calls, [])
        self.assertEqual(client.tp_calls, [])

    async def test_repair_tpsl_confirm_cancels_sets_ladder_and_verifies(self):
        class RepairClient(FakeFuturesClient):
            def __init__(self):
                self.cancel_calls = []
                self.sl_calls = []
                self.tp_calls = []
                self.orders = []

            async def get_position(self, symbol):
                return {
                    "symbol": "H/USDT:USDT",
                    "side": "long",
                    "entry_price": 0.12088,
                    "mark_price": 0.1664,
                    "contracts": 1654.0,
                    "leverage": 20,
                    "margin": 13.76,
                }

            async def get_tp_sl_orders(self, symbol=None):
                return self.orders

            async def cancel_tp_sl_orders(self, symbol):
                self.cancel_calls.append(symbol)
                self.orders = []
                return 0

            async def place_reduce_sl(self, symbol, side, trigger_price, qty=None):
                self.sl_calls.append((symbol, side, trigger_price, qty))
                self.orders.append({"symbol": "H/USDT:USDT", "trigger_type": 2, "trigger_price": trigger_price})
                return {"id": "sl"}

            async def place_reduce_tp(self, symbol, side, qty, trigger_price):
                self.tp_calls.append((symbol, side, qty, trigger_price))
                self.orders.append({"symbol": "H/USDT:USDT", "trigger_type": 1, "trigger_price": trigger_price})
                return {"id": f"tp{len(self.tp_calls)}"}

            def futures_symbol(self, symbol: str) -> str:
                return "H/USDT:USDT"

        client = RepairClient()
        context = FakeTelegramContext(
            bot_data={"config": FakeConfig(tp_ladder_pcts="50,120,250", sl_pct=500, tp_partial_pct=50), "exchange": client},
        )
        query = FakeCallbackQuery("repair_tpsl_confirm_H/USDT:USDT")
        update = FakeTelegramUpdate(callback_query=query)

        async def fake_upsert_ladder(*args, **kwargs):
            return None

        with patch("bot.infra.db.upsert_tp_ladder", fake_upsert_ladder), \
             patch("bot.db.get_open_position", return_value={"symbol": "H/USDT:USDT"}):
            await repair_tpsl_callback(update, context)

        text = query.edits[-1][0]
        self.assertIn("Repair complete", text)
        self.assertIn("TP_count=3", text)
        self.assertIn("SL_count=1", text)
        self.assertEqual(client.cancel_calls, ["H/USDT:USDT"])
        self.assertEqual(len(client.tp_calls), 3)
        self.assertEqual(len(client.sl_calls), 1)

    async def test_close_final_button_explains_reason_pnl_and_no_reentry(self):
        context = self._context()
        query = FakeCallbackQuery("close_final_HYPE/USDT:USDT")
        update = FakeTelegramUpdate(callback_query=query)

        async def close_position(*args, **kwargs):
            return {
                "symbol": "HYPE/USDT:USDT",
                "side": "short",
                "pnl": 10.0,
                "margin": 20.0,
                "entry_price": 10.0,
                "exit_price": 9.5,
                "leverage": 10,
                "cycles_left": None,
                "close_reason": "manual",
            }

        with patch("bot.handlers.trading._svc_close", close_position):
            await close_final_callback(update, context)

        user_text = query.edits[-1][0]
        self.assertIn("Причина: ручное закрытие", user_text)
        self.assertIn("PnL:", user_text)
        self.assertIn("+$10.00", user_text)
        self.assertIn("Перезаход: нет", user_text)
        self.assertIn("пользователь выбрал закрыть насовсем", user_text)

    async def test_close_reentry_button_explains_reentry_yes_and_cycles(self):
        context = self._context()
        query = FakeCallbackQuery("close_reentry_HYPE/USDT:USDT")
        update = FakeTelegramUpdate(callback_query=query)

        async def close_position(*args, **kwargs):
            return {
                "symbol": "HYPE/USDT:USDT",
                "side": "short",
                "pnl": -4.5,
                "margin": 15.0,
                "entry_price": 10.0,
                "exit_price": 10.3,
                "leverage": 10,
                "cycles_left": 2,
                "close_reason": "manual_reentry",
            }

        with patch("bot.handlers.trading._svc_close", close_position):
            await close_reentry_callback(update, context)

        user_text = query.edits[-1][0]
        self.assertIn("Причина: ручное закрытие", user_text)
        self.assertIn("PnL:", user_text)
        self.assertIn("-$4.50", user_text)
        self.assertIn("Перезаход: да", user_text)
        self.assertIn("пользователь выбрал закрытие с перезаходом", user_text)

    async def test_monitor_close_button_uses_same_detailed_close_message(self):
        context = self._context()
        query = FakeCallbackQuery("mon_close_confirm_HYPE/USDT:USDT")
        update = FakeTelegramUpdate(callback_query=query)

        async def close_position(*args, **kwargs):
            return {
                "symbol": "HYPE/USDT:USDT",
                "side": "short",
                "pnl": 2.25,
                "margin": 10.0,
                "entry_price": 10.0,
                "exit_price": 9.8,
                "leverage": 10,
                "cycles_left": None,
                "close_reason": "manual",
            }

        with patch("bot.services.trading.close_position", close_position):
            await monitor_close_confirm_callback(update, context)

        user_text = query.edits[-1][0]
        self.assertIn("Причина: ручное закрытие", user_text)
        self.assertIn("PnL:", user_text)
        self.assertIn("Перезаход: нет", user_text)

    async def test_nlp_close_button_uses_same_detailed_close_message(self):
        class PositionClient(FakeFuturesClient):
            async def get_positions(self):
                return [{"symbol": "HYPE/USDT:USDT"}]

        context = FakeTelegramContext(
            bot_data={
                "config": FakeConfig(),
                "exchange": PositionClient(),
            }
        )
        query = FakeCallbackQuery("nlp_close_HYPE")
        update = FakeTelegramUpdate(callback_query=query)

        async def close_position(*args, **kwargs):
            return {
                "symbol": "HYPE/USDT:USDT",
                "side": "short",
                "pnl": 2.25,
                "margin": 10.0,
                "entry_price": 10.0,
                "exit_price": 9.8,
                "leverage": 10,
                "cycles_left": None,
                "close_reason": "manual",
            }

        with patch("bot.services.trading.close_position", close_position):
            await nlp_close_callback(update, context)

        user_text = query.edits[-1][0]
        self.assertIn("Причина: ручное закрытие", user_text)
        self.assertIn("PnL:", user_text)
        self.assertIn("Перезаход: нет", user_text)

    def test_exchange_error_formatter_contract_for_2021_and_4130(self):
        from bot.services.exchange_errors import format_open_error

        immediate = format_open_error(
            RuntimeError(RAW_TP_IMMEDIATE),
            symbol="HYPE/USDT:USDT",
            side="short",
            stage="TP1",
            entry_price=10.0,
            mark_price=9.8,
            trigger_price=10.1,
        )
        self.assertIn("сработал бы сразу", immediate)
        self.assertIn("Для SHORT тейк должен быть ниже текущей цены", immediate)
        _assert_no_raw_binance_leak(self, immediate)

        existing = format_open_error(
            RuntimeError(RAW_EXISTING_CLOSE_POSITION),
            symbol="RENDER/USDT:USDT",
            side="long",
            stage="SL",
            entry_price=5.0,
            mark_price=5.1,
            trigger_price=4.8,
        )
        self.assertIn("уже есть защитный стоп/тейк", existing)
        self.assertIn("старые TP/SL будут отменены", existing)
        _assert_no_raw_binance_leak(self, existing)

    def test_exchange_error_formatter_explains_unconfirmed_tpsl_readback(self):
        from bot.services.exchange_errors import format_open_error

        text = format_open_error(
            RuntimeError("LIGHT/USDT:USDT: expected 3 TP orders, found 0; missing 0.1085, 0.1028, 0.0955"),
            symbol="LIGHT/USDT:USDT",
            side="short",
        )

        self.assertIn("LIGHT", text)
        self.assertIn("TP/SL", text)
        self.assertIn("вход был закрыт", text)
        self.assertIn("чтобы не оставить без TP/SL", text)
        self.assertIn("ожидал 3 TP", text)
        self.assertIn("нашёл 0", text)
        self.assertNotIn("expected 3 TP orders", text)
        self.assertNotIn("missing 0.1085", text)

    def test_close_message_builder_contract_for_tp_sl_and_reentry(self):
        from bot.services.trade_messages import CloseFacts, ReentryFacts, format_close_message

        tp_text = format_close_message(
            CloseFacts(
                symbol="HYPE/USDT:USDT",
                side="short",
                reason_code="tp",
                reason_label="тейк-профит TP1",
                entry_price=10.0,
                exit_price=9.5,
                leverage=10,
                margin=20.0,
                realized_pnl=10.0,
                hold_seconds=90,
            ),
            ReentryFacts(
                enabled=True,
                will_reenter=True,
                why="TP разрешает перезаход",
                cycle_next=1,
                max_cycles=3,
            ),
        )
        self.assertIn("Причина: тейк-профит TP1", tp_text)
        self.assertIn("PnL:", tp_text)
        self.assertIn("+$10.00", tp_text)
        self.assertIn("Перезаход: да", tp_text)
        self.assertIn("Почему: TP разрешает перезаход", tp_text)

        sl_text = format_close_message(
            CloseFacts(
                symbol="RENDER/USDT:USDT",
                side="long",
                reason_code="sl",
                reason_label="стоп-лосс",
                entry_price=5.0,
                exit_price=4.8,
                leverage=5,
                margin=25.0,
                realized_pnl=-5.0,
            ),
            ReentryFacts(
                enabled=False,
                will_reenter=False,
                why="reentry_on_sl выключен",
            ),
        )
        self.assertIn("Причина: стоп-лосс", sl_text)
        self.assertIn("-$5.00", sl_text)
        self.assertIn("Перезаход: нет", sl_text)
        self.assertIn("reentry_on_sl выключен", sl_text)

    def test_close_message_builder_says_pnl_unknown_when_close_price_missing(self):
        from bot.services.trade_messages import CloseFacts, ReentryFacts, format_close_message

        text = format_close_message(
            CloseFacts(
                symbol="EPIC/USDT:USDT",
                side="long",
                reason_code="unknown",
                reason_label="позиция закрыта на бирже",
                entry_price=0.4686,
                exit_price=0,
                leverage=20,
                margin=10.0,
                realized_pnl=None,
            ),
            ReentryFacts(
                enabled=False,
                will_reenter=False,
                why="Binance не отдал цену закрытия",
            ),
        )

        self.assertIn("PnL: `неизвестен`", text)
        self.assertIn("Binance/история не дали цену закрытия", text)
        self.assertIn("бот не выдумывает прибыль или убыток", text)
        self.assertNotIn("$0.00", text)
        self.assertNotIn("+0.0%", text)


if __name__ == "__main__":
    unittest.main()

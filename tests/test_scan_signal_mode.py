import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.ai.analyst import AnalystResult
from bot.handlers.scan import scan_handler, scan_preview_callback, _do_execute_open


class FakeStatus:
    def __init__(self, text):
        self.text = text
        self.edits = []
        self.deleted = False

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))
        self.text = text

    async def delete(self):
        self.deleted = True


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return FakeStatus(text)


class ScanSignalModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_uses_both_mode_and_builds_markets_cards(self):
        message = FakeMessage()
        context = SimpleNamespace(
            args=[],
            user_data={},
            bot_data={
                "config": SimpleNamespace(
                    openrouter_api_key="sk-test",
                    openrouter_model="google/gemini-3.1-pro-preview-customtools:online",
                    exchange_provider="binance_testnet",
                    default_trade_usdt=0.20,
                    averaging_amount=0.10,
                    default_leverage=0,
                ),
                "exchange": object(),
            },
        )
        update = SimpleNamespace(message=message)

        async def fake_scan_overbought(*args, **kwargs):
            return [], 0

        async def fake_snapshot(*args, **kwargs):
            return {
                "provider": "binance_testnet",
                "balance": {"free_usdt": 100.0, "total_usdt": 100.0},
                "positions": [],
                "tp_sl_orders": [],
                "candidates": [],
                "errors": [],
            }

        async def fake_analysis(*args, **kwargs):
            self.assertEqual(kwargs["mode"], "both")
            self.assertEqual(kwargs["model"], "openai/gpt-5.5:online")
            return AnalystResult(
                text="""
COIN: EPIC
SIDE: SHORT
PRICE: $4200
TECH: squeeze setup
FUND: ETF flow
FUNDING: +0.012
ENTRY: $4180-4200
TP1: $4100
TP2: $4000
TP3: $3900
SL: $4300
RISK: 4/10

SENTIMENT: one short idea"
                """,
                model=kwargs["model"],
            )

        async def fake_find_symbol(_client, ticker):
            return f"{ticker}/USDT:USDT"

        async def fake_analyze(*args, **kwargs):
            return {
                "symbol": "EPIC/USDT:USDT",
                "direction": "short",
                "rsi": 47.0,
                "daily_change_pct": -2.3,
                "ema_trend": "down",
                "bb_position": 0.62,
                "volume_24h": 1240000,
                "reasons": ["reason 1", "reason 2"],
                "funding_rate": 0.00012,
            }

        with patch("bot.handlers.scan.scan_overbought", fake_scan_overbought), \
             patch("bot.handlers.scan.build_research_snapshot", fake_snapshot), \
             patch("bot.handlers.scan.deep_short_analysis", fake_analysis), \
             patch("bot.handlers.scan.mexc_find_futures_symbol", fake_find_symbol), \
             patch("bot.handlers.scan.analyze_single_coin", fake_analyze):
            await scan_handler(update, context)

        reply_texts = [msg for msg, _ in message.replies]
        self.assertTrue(any("EPIC" in text for text in reply_texts))
        self.assertTrue(any("AI top-5 long + top-5 short" in text for text in reply_texts))
        self.assertTrue(any("TP1" in text and "TP2" in text and "TP3" in text and "SL" in text for text in reply_texts))

        card_payloads = [(text, kwargs) for text, kwargs in message.replies if "reply_markup" in kwargs]
        self.assertTrue(card_payloads)
        keyboard = card_payloads[0][1]["reply_markup"]
        callback = keyboard.inline_keyboard[0][0].callback_data
        self.assertIn("scan_preview_", callback)

    async def test_scan_preview_shows_full_trade_plan_before_opening(self):
        class FakeQuery:
            data = "scan_preview_abc123"

            def __init__(self):
                self.messages = []

            async def answer(self, *args, **kwargs):
                return None

            @property
            def message(self):
                return self

            async def reply_text(self, text, **kwargs):
                self.messages.append((text, kwargs))

        class FakeClient:
            async def get_ticker(self, symbol):
                return {"last": 100.0}

        q = FakeQuery()
        context = SimpleNamespace(
            bot_data={
                "config": SimpleNamespace(tp_ladder_pcts="50,120,250", default_trade_usdt=10.0),
                "exchange": FakeClient(),
            },
            user_data={
                "scan_picks": {
                    "abc123": {
                        "symbol": "EPIC/USDT:USDT",
                        "side": "sell",
                        "direction": "short",
                        "margin": 10.0,
                        "leverage": 10,
                        "pick": {"tp1": "$95", "tp2": "$90", "tp3": "$80", "sl": "$105"},
                    }
                }
            },
        )
        update = SimpleNamespace(callback_query=q)

        await scan_preview_callback(update, context)

        text = q.messages[0][0]
        self.assertIn("SHORT", text)
        self.assertIn("TP1", text)
        self.assertIn("TP2", text)
        self.assertIn("TP3", text)
        self.assertIn("SL", text)
        self.assertIn("profit", text)
        keyboard = q.messages[0][1]["reply_markup"]
        self.assertIn("scan_confirm_abc123", keyboard.inline_keyboard[0][0].callback_data)

    async def test_scan_preview_filters_invalid_tp_before_confirmation(self):
        class FakeQuery:
            data = "scan_preview_bad1"

            def __init__(self):
                self.messages = []

            async def answer(self, *args, **kwargs):
                return None

            @property
            def message(self):
                return self

            async def reply_text(self, text, **kwargs):
                self.messages.append((text, kwargs))

        class FakeClient:
            async def get_ticker(self, symbol):
                return {"last": 101.0}

        q = FakeQuery()
        context = SimpleNamespace(
            bot_data={
                "config": SimpleNamespace(tp_ladder_pcts="50,120,250", default_trade_usdt=10.0),
                "exchange": FakeClient(),
            },
            user_data={
                "scan_picks": {
                    "bad1": {
                        "symbol": "EPIC/USDT:USDT",
                        "side": "buy",
                        "direction": "long",
                        "margin": 10.0,
                        "leverage": 10,
                        "pick": {"tp1": "$100.5", "tp2": "$115", "tp3": "$130", "sl": "$95"},
                    }
                }
            },
        )
        update = SimpleNamespace(callback_query=q)

        await scan_preview_callback(update, context)

        text = q.messages[0][0]
        self.assertIn("only valid", text)
        self.assertNotIn("TP1:", text)
        self.assertIn("TP2", text)
        self.assertIn("TP3", text)

    async def test_scan_confirm_rechecks_market_and_opens_updated_plan_without_extra_confirm(self):
        class FakeQuery:
            data = "scan_confirm_race1"

            def __init__(self):
                self.messages = []

            async def answer(self, *args, **kwargs):
                return None

            @property
            def message(self):
                return self

            async def reply_text(self, text, **kwargs):
                self.messages.append((text, kwargs))

        class FakeClient:
            async def get_ticker(self, symbol):
                return {"last": 101.0}

        open_calls = []

        async def fake_open(*args, **kwargs):
            open_calls.append((args, kwargs))

        q = FakeQuery()
        context = SimpleNamespace(
            bot_data={
                "config": SimpleNamespace(tp_ladder_pcts="50,120,250", default_trade_usdt=10.0),
                "exchange": FakeClient(),
            },
            user_data={
                "scan_picks": {
                    "race1": {
                        "symbol": "EPIC/USDT:USDT",
                        "side": "buy",
                        "direction": "long",
                        "margin": 10.0,
                        "leverage": 10,
                        "pick": {"tp1": "$100.5", "tp2": "$115", "tp3": "$130", "sl": "$95"},
                        "preview_fingerprint": "old-plan",
                    }
                }
            },
            application=None,
        )
        update = SimpleNamespace(callback_query=q)

        with patch("bot.handlers.scan._do_execute_open", fake_open):
            from bot.handlers.scan import scan_confirm_callback
            await scan_confirm_callback(update, context)

        self.assertEqual(q.messages, [])
        self.assertEqual(len(open_calls), 1)
        self.assertEqual(open_calls[0][1]["pick"]["tp1"], "")
        self.assertEqual(float(open_calls[0][1]["pick"]["tp2"]), 115.0)
        self.assertEqual(float(open_calls[0][1]["pick"]["tp3"]), 130.0)

    async def test_scan_confirm_opens_when_entry_reference_changed(self):
        from bot.services.trade_plan import build_three_tp_plan, plan_fingerprint

        old_plan = build_three_tp_plan(
            symbol="EPIC/USDT:USDT",
            side="long",
            entry=100.0,
            reference=100.0,
            leverage=10,
            margin=10.0,
            tp_prices=[115.0, 130.0, 150.0],
            sl_price=95.0,
        )

        class FakeQuery:
            data = "scan_confirm_move1"

            def __init__(self):
                self.messages = []

            async def answer(self, *args, **kwargs):
                return None

            @property
            def message(self):
                return self

            async def reply_text(self, text, **kwargs):
                self.messages.append((text, kwargs))

        class FakeClient:
            async def get_ticker(self, symbol):
                return {"last": 101.0}

        open_calls = []

        async def fake_open(*args, **kwargs):
            open_calls.append((args, kwargs))

        q = FakeQuery()
        context = SimpleNamespace(
            bot_data={
                "config": SimpleNamespace(tp_ladder_pcts="50,120,250", default_trade_usdt=10.0),
                "exchange": FakeClient(),
            },
            user_data={
                "scan_picks": {
                    "move1": {
                        "symbol": "EPIC/USDT:USDT",
                        "side": "buy",
                        "direction": "long",
                        "margin": 10.0,
                        "leverage": 10,
                        "pick": {"tp1": "$115", "tp2": "$130", "tp3": "$150", "sl": "$95"},
                        "preview_fingerprint": plan_fingerprint(old_plan),
                    }
                }
            },
            application=None,
        )
        update = SimpleNamespace(callback_query=q)

        with patch("bot.handlers.scan._do_execute_open", fake_open):
            from bot.handlers.scan import scan_confirm_callback
            await scan_confirm_callback(update, context)

        self.assertEqual(q.messages, [])
        self.assertEqual(len(open_calls), 1)
        self.assertEqual(float(open_calls[0][1]["pick"]["tp1"]), 115.0)
        self.assertEqual(float(open_calls[0][1]["pick"]["tp2"]), 130.0)
        self.assertEqual(float(open_calls[0][1]["pick"]["tp3"]), 150.0)

    async def test_scan_open_summary_lists_three_tps_when_pick_is_used(self):
        class FakeQuery:
            def __init__(self):
                self.messages = []

            @property
            def message(self):
                return self

            async def reply_text(self, text, **kwargs):
                self.messages.append((text, kwargs))

        async def fake_execute_open(*args, **kwargs):
            return {
                "symbol": "EPIC/USDT:USDT",
                "side": "sell",
                "margin": 10.0,
                "leverage": 10,
                "entry_price": 100.0,
                "liquidation_price": 150.0,
                "tp_price": 95.0,
                "sl_price": 105.0,
            }

        q = FakeQuery()
        app = SimpleNamespace(bot_data={"config": SimpleNamespace(tp_ladder_pcts="50,120,250")})
        with patch("bot.handlers.trading.execute_open", fake_execute_open):
            await _do_execute_open(
                q,
                client=object(),
                app=app,
                symbol="EPIC/USDT:USDT",
                side="sell",
                margin=10.0,
                leverage=10,
                pick={"tp1": "95", "tp2": "90", "tp3": "80", "sl": "105"},
                exit_mode_override="ladder",
            )

        final_text = q.messages[-1][0]
        self.assertIn("TP1", final_text)
        self.assertIn("TP2", final_text)
        self.assertIn("TP3", final_text)
        self.assertIn("profit", final_text)


if __name__ == "__main__":
    unittest.main()

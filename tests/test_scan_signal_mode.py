import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.ai.analyst import AnalystResult
from bot.handlers.scan import scan_handler


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

        card_payloads = [(text, kwargs) for text, kwargs in message.replies if "reply_markup" in kwargs]
        self.assertTrue(card_payloads)
        keyboard = card_payloads[0][1]["reply_markup"]
        callback = keyboard.inline_keyboard[0][0].callback_data
        self.assertIn("open_", callback)


if __name__ == "__main__":
    unittest.main()

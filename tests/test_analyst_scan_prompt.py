import unittest
from pathlib import Path

from bot.ai.analyst import (
    DEFAULT_MODEL,
    _build_system_prompt,
    _build_user_msg,
    _clean_model_text,
    _max_output_tokens,
    normalize_openrouter_model,
    parse_analyst_blocks,
)
from bot.signals.parser import parse_signal


class AnalystScanPromptTest(unittest.TestCase):
    def test_parse_blocks_keeps_trade_side(self):
        text = """
COIN: ETH
SIDE: LONG
PRICE: $4200
TECH: RSI 32, bounce setup
FUND: ETF inflow
FUNDING: -0.010% (long earns funding)
ENTRY: $4180-4220
SL: $4050
RISK: 4/10

COIN: SOL
SIDE: SHORT
PRICE: $180
TECH: RSI 78, rejection
FUND: unlock pressure
FUNDING: +0.045% (longs overheated)
ENTRY: $178-182
SL: $188
RISK: 6/10

SENTIMENT: choppy market.
"""

        picks = parse_analyst_blocks(text, n=6)

        self.assertEqual(picks[0]["ticker"], "ETH")
        self.assertEqual(picks[0]["side"], "long")
        self.assertEqual(picks[1]["ticker"], "SOL")
        self.assertEqual(picks[1]["side"], "short")

    def test_parse_blocks_supports_scan_markdown_table(self):
        text = """
| # | Тикер | Тип | Вход по рынку | TP1 50% | TP2 25% | TP3 25% | Stop Loss | Рекомендуемое плечо | Риск 1-10 | Краткий триггер |
| - | --- | --- | ------------- | ------- | ------- | ------- | --------- | ------------------- | --------- | --------------- |
| 1 | BTC | Short | 65000-66000 | 64000 | 63000 | 62000 | 67000 | 5x | 4 | Пробой уровня объема |
| 2 | ETH | Long | 3400-3480 | 3580 | 3720 | 3850 | 3220 | 4x | 5 | Пробой поддержки |
"""

        picks = parse_analyst_blocks(text, n=4)

        self.assertEqual(picks[0]["ticker"], "BTC")
        self.assertEqual(picks[0]["side"], "short")
        self.assertEqual(picks[0]["entry"], "65000-66000")
        self.assertEqual(picks[0]["tp1"], "64000")
        self.assertEqual(picks[0]["tp2"], "63000")
        self.assertEqual(picks[0]["tp3"], "62000")
        self.assertEqual(picks[0]["sl"], "67000")
        self.assertEqual(picks[0]["risk"], "4")

        self.assertEqual(picks[1]["ticker"], "ETH")
        self.assertEqual(picks[1]["side"], "long")
        self.assertEqual(picks[1]["risk"], "5")

    def test_user_message_requests_long_and_short_research_from_binance_snapshot(self):
        msg = _build_user_msg(
            [
                {
                    "symbol": "ETH/USDT:USDT",
                    "direction": "long",
                    "rsi": 31.5,
                    "daily_change_pct": -12.4,
                    "funding_rate": -0.0001,
                },
                {
                    "symbol": "SOL/USDT:USDT",
                    "direction": "short",
                    "rsi": 77.1,
                    "daily_change_pct": 18.2,
                    "funding_rate": 0.00045,
                },
            ],
            n=3,
            account_context={
                "provider": "binance_testnet",
                "free_usdt": 125.5,
                "total_usdt": 140.0,
                "positions": [
                    {
                        "symbol": "BTC/USDT:USDT",
                        "side": "short",
                        "unrealized_pnl": 2.3,
                        "percentage": 4.6,
                    }
                ],
            },
        )

        self.assertIn("Need 3 LONG and 3 SHORT opportunities", msg)
        self.assertIn("Run full online research, then verify everything by Binance API snapshot.", msg)
        self.assertIn("BINANCE API SNAPSHOT", msg)
        self.assertIn("provider=binance_testnet", msg)
        self.assertIn("free_usdt=125.50", msg)
        self.assertIn("BTC short", msg)
        self.assertNotIn("MEXC", msg)

    def test_default_model_uses_openrouter_gpt_55_online(self):
        self.assertEqual(DEFAULT_MODEL, "openai/gpt-5.5:online")

    def test_model_normalization_forces_manual_scan_to_gpt_55_online(self):
        self.assertEqual(
            normalize_openrouter_model("gpt-5.5:online", force_default=True),
            "openai/gpt-5.5:online",
        )
        self.assertEqual(
            normalize_openrouter_model("google/gemini-3.1-pro-preview-customtools:online", force_default=True),
            "openai/gpt-5.5:online",
        )

    def test_clean_model_text_strips_reasoning_and_trailing_extra(self):
        raw = """
<think>private reasoning that must never reach Telegram</think>
Reasoning: more hidden chain
COIN: ETH
SIDE: LONG
PRICE: $4200
TECH: RSI 32
FUND: ETF inflow
FUNDING: -0.010%
ENTRY: $4180-4220
SL: $4050
RISK: 4/10
SENTIMENT: concise market note.

Extra explanation that should be dropped.
"""

        cleaned = _clean_model_text(raw, max_chars=500)

        self.assertTrue(cleaned.startswith("COIN: ETH"))
        self.assertIn("SENTIMENT: concise market note.", cleaned)
        self.assertNotIn("private reasoning", cleaned)
        self.assertNotIn("Reasoning:", cleaned)
        self.assertNotIn("Extra explanation", cleaned)

    def test_output_token_budget_stays_bounded_for_top_three_scan(self):
        self.assertLessEqual(_max_output_tokens(3, "both"), 1500)

    def test_system_prompt_for_both_includes_scan_prompt_sections(self):
        prompt = _build_system_prompt(mode="both", n=5)
        template = Path("SCAN-PROMPT.md").read_text(encoding="utf-8")

        self.assertIn("ТОП-5 ШОРТОВ", prompt)
        self.assertIn("ТОП-5 ЛОНГОВ", prompt)
        self.assertIn("OUTPUT FORMAT", template)
        self.assertIn("OUTPUT FORMAT", prompt)
        self.assertIn("BOT OUTPUT CONTRACT", prompt)
        self.assertIn("Никаких markdown-таблиц", prompt)
        self.assertIn("COIN: TICKER", prompt)
        self.assertIn("Ровно 5 блоков SIDE: LONG и ровно 5 блоков SIDE: SHORT.", prompt)

    def test_scan_prompt_file_is_readable_not_mojibake(self):
        template = Path("SCAN-PROMPT.md").read_text(encoding="utf-8")

        self.assertIn("Ты — старший аналитик", template)
        self.assertIn("ТОП-5 монет", template)
        for marker in ("РўС‹", "Рџ", "вЂ", "Ð", "Ñ"):
            self.assertNotIn(marker, template)

    def test_user_message_prioritizes_online_research_before_exchange_verification(self):
        msg = _build_user_msg([], n=6, mode="both")

        self.assertIn("Need 6 LONG and 6 SHORT opportunities", msg)
        self.assertIn("Run full online research, then verify everything by Binance API snapshot.", msg)
        self.assertIn("Priority: do not rely only on scanner output; online + snapshot checks are mandatory.", msg)

    def test_signal_mode_prompt_requests_one_parseable_signal_not_coin_list(self):
        system = _build_system_prompt(mode="signal", n=1)
        user = _build_user_msg([], n=1, mode="signal")

        self.assertIn("SIGNAL:", system)
        self.assertIn("TP1", system)
        self.assertIn("TP2", system)
        self.assertIn("TP3", system)
        self.assertIn("online research", user)

    def test_clean_signal_text_is_parseable_by_existing_signal_parser(self):
        raw = """
<think>hidden analysis</think>
Some intro that should not be sent.
SIGNAL:
EPIC USDT
SHORT
Entry 0.2098 - 0.2104
SL 0.2167
TP1 0.1978
TP2 0.1942
TP3 0.1903
x3
Confidence 96%
Risk 4/10
Reason: funding positive, rejection from local high

Extra chat prose should be dropped.
"""

        cleaned = _clean_model_text(raw, max_chars=1000)
        signal = parse_signal(cleaned)

        self.assertTrue(cleaned.startswith("SIGNAL:"))
        self.assertEqual(signal.symbol, "EPIC")
        self.assertEqual(signal.side, "short")
        self.assertEqual(len(signal.tps), 3)
        self.assertEqual([tp.share_pct for tp in signal.tps], [50.0, 25.0, 25.0])
        self.assertNotIn("hidden analysis", cleaned)
        self.assertNotIn("Extra chat prose", cleaned)


if __name__ == "__main__":
    unittest.main()

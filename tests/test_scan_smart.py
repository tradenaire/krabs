"""Smart scan safety gates.

These tests intentionally avoid network calls: scan must fail closed based on
local MEXC-derived facts, not on Grok/LLM claims.
"""
from pathlib import Path

from bot.ai.analyst import _build_user_msg, parse_analyst_blocks
from bot.ai.scanner import validate_short_pick


def _valid_short_candidate(**overrides):
    item = {
        "symbol": "BTC/USDT:USDT",
        "direction": "short",
        "price": 70000.0,
        "funding_rate": 0.0004,
        "trend_change_short": True,
        "msb_short": True,
        "risk_score": 6,
        "timeframes": {
            "1h": {"rsi": 72.1, "ema_trend": "BEARISH", "msb_short": True},
            "4h": {"rsi": 74.0, "ema_trend": "BEARISH", "msb_short": False},
            "1d": {"rsi": 69.0, "ema_trend": "BULLISH", "msb_short": False},
        },
        "reasons": ["RSI разворачивается вниз"],
    }
    item.update(overrides)
    return item


def test_validate_short_pick_allows_only_confirmed_short():
    status, errors = validate_short_pick(_valid_short_candidate())
    assert status == "VALIDATED"
    assert errors == []


def test_validate_short_pick_rejects_long_or_missing_msb():
    status, errors = validate_short_pick(_valid_short_candidate(direction="long", msb_short=False))
    assert status == "REJECTED"
    assert "локальный сигнал не SHORT" in errors
    assert "bearish MSB не подтверждён" in errors


def test_parse_analyst_blocks_sanitizes_ticker_and_rejects_bad_risk():
    text = """
COIN: BTC/USDT
PRICE: $70000
TECH: test
FUND: test
FUNDING: +0.01%
ENTRY: 70000-70500
SL: 72000
RISK: 6/10

COIN: FAKE
PRICE: $1
TECH: test
FUND: test
FUNDING: x
ENTRY: x
SL: x
RISK: 99/10
SENTIMENT: x
"""
    parsed = parse_analyst_blocks(text, n=5)
    assert [p["ticker"] for p in parsed] == ["BTC"]
    assert parsed[0]["risk_num"] == 6


def test_parse_analyst_blocks_accepts_underscore_mexc_symbol():
    text = """
COIN: ETH_USDT
PRICE: $3000
TECH: test
FUND: test
FUNDING: +0.01%
ENTRY: 3000-3010
SL: 3100
RISK: 5/10
"""
    parsed = parse_analyst_blocks(text, n=5)
    assert parsed[0]["ticker"] == "ETH"


def test_prompt_contains_mexc_snapshot_gate_fields():
    prompt = _build_user_msg([_valid_short_candidate(score=88)], n=1)
    assert "mexc_symbol=BTC/USDT:USDT" in prompt
    assert "gate=" in prompt
    assert "risk=6/10" in prompt
    assert "msb=True" in prompt


def test_scan_avg_callback_not_shadowed_by_scan_wizard_prefix():
    main_py = Path("bot/main.py").read_text(encoding="utf-8")
    assert '"scan"' not in main_py.split("for _p in", 1)[1].split("):", 1)[0]
    assert "scan_avg_callback" in main_py

"""Smart scan safety gates.

These tests intentionally avoid network calls: scan must fail closed based on
local MEXC-derived facts, not on Grok/LLM claims.
"""
import asyncio
from pathlib import Path

from bot.ai.analyst import _build_user_msg, parse_analyst_blocks
from bot.ai.scanner import _enrich_multi_timeframe, format_coin_card, validate_short_pick
from bot.exchange.client import calc_min_order_margin


def _ohlcv_rows(close: float = 100.0) -> list[list[float]]:
    rows = []
    for i in range(100):
        price = close + i * 0.1
        rows.append([i, price, price + 1, price - 1, price, 1000 + i])
    return rows


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


def test_validate_short_pick_rejects_long_but_not_missing_confirmations():
    status, errors = validate_short_pick(_valid_short_candidate(direction="long", msb_short=False))
    assert status == "REJECTED"
    assert "локальный сигнал не SHORT" in errors
    assert "bearish MSB не подтверждён" not in errors


def test_validate_short_pick_allows_short_without_trend_or_msb():
    status, errors = validate_short_pick(
        _valid_short_candidate(trend_change_short=False, msb_short=False, risk_score=7)
    )
    assert status == "VALIDATED"
    assert errors == []


def test_validate_short_pick_still_rejects_high_risk():
    status, errors = validate_short_pick(_valid_short_candidate(risk_score=8))
    assert status == "REJECTED"
    assert "risk 8/10 выше лимита" in errors


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


def test_parse_analyst_blocks_accepts_web_research_markdown_table():
    text = """
| Тикер | Текущая цена | Техническая причина (RSI/Graph) | Фундаментальный триггер (Новости/Разблокировки) | Рекомендуемая зона входа и Стоп-лосс | Риск (1-10) |
|---|---:|---|---|---|---:|
| HYPE | $46.12 | RSI 4H 77, 1D 73; отклонение от EMA20 | token unlock через 4 дня | вход $45-47, SL $49.2 | 6 |
| FAKE/USDT | $1 | no data | no data | x | 99 |

Sentiment Analysis: рынок перегрет, но нужен MEXC-фильтр.
"""
    parsed = parse_analyst_blocks(text, n=5)
    assert [p["ticker"] for p in parsed] == ["HYPE"]
    assert parsed[0]["risk_num"] == 6
    assert "RSI 4H 77" in parsed[0]["tech"]
    assert "token unlock" in parsed[0]["fund"]
    assert "SL $49.2" in parsed[0]["entry"]


def test_prompt_contains_mexc_snapshot_gate_fields():
    prompt = _build_user_msg([_valid_short_candidate(score=88)], n=1)
    assert "mexc_symbol=BTC/USDT:USDT" in prompt
    assert "gate=" in prompt
    assert "risk=6/10" in prompt
    assert "msb=True" in prompt


def test_web_first_prompt_starts_from_market_research_before_mexc_match():
    prompt = _build_user_msg([], n=5, web_first=True)
    assert "web-search" in prompt
    assert "MEXC futures" in prompt
    assert "не придумывай COIN" not in prompt


def test_scan_card_strips_domains_and_shows_profit_per_dollar():
    card = format_coin_card(
        _valid_short_candidate(
            symbol="HYPE/USDT:USDT",
            reasons=["RSI перегрет"],
            volume_24h=404_900_000,
            bb_position=0.50,
            ema_trend="медвежий",
        ),
        1,
        ai_note="FXStreet says reversal is likely (fxstreet.com) https://www.fxstreet.com/news/x",
        max_lev=10,
        margin=5.0,
        tp_pct=500.0,
    )
    assert "fxstreet.com" not in card.lower()
    assert "https://" not in card.lower()
    assert "Доход с `$1`: `+$5.00`" in card


def test_scan_avg_callback_not_shadowed_by_scan_wizard_prefix():
    main_py = Path("bot/main.py").read_text(encoding="utf-8")
    assert '"scan"' not in main_py.split("for _p in", 1)[1].split("):", 1)[0]
    assert "scan_avg_callback" in main_py


def test_calc_min_order_margin_rounds_up_contracts_before_margin():
    price = 46.12
    contract_size = 0.01
    leverage = 50
    min_notional = 5.0

    margin = calc_min_order_margin(price, contract_size, leverage, min_notional=min_notional)
    naive_margin = min_notional / leverage * 1.05

    assert margin > naive_margin
    assert margin == calc_min_order_margin(price, contract_size, leverage, min_notional=min_notional)


def test_enrich_reuses_existing_1h_ohlcv():
    class FakeMexc:
        def __init__(self):
            self.calls = []

        async def fetch_ohlcv(self, symbol, timeframe, limit=100):
            self.calls.append(timeframe)
            return _ohlcv_rows()

    class FakeExchange:
        def __init__(self):
            self._exchange = FakeMexc()

    exchange = FakeExchange()
    analysis = _valid_short_candidate(symbol="HYPE/USDT:USDT")

    result = asyncio.run(
        _enrich_multi_timeframe(exchange, "HYPE/USDT:USDT", analysis, _ohlcv_rows())
    )

    assert exchange._exchange.calls == ["4h", "1d"]
    assert result["timeframes"]["1h"]["rsi"] is not None

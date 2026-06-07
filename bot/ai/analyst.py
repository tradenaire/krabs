"""LLM analyst — market research through OpenRouter online-capable models."""
from __future__ import annotations

import datetime as _dt
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from bot.ai.research_snapshot import format_research_snapshot

logger = logging.getLogger(__name__)

OPENROUTER_PRICES = {
    "openai/gpt-5.5:online":       {"input": 5.00, "output": 30.00},
    "openai/gpt-5.5":              {"input": 5.00, "output": 30.00},
    "x-ai/grok-4-fast:online":      {"input": 0.20, "output": 0.50},
    "anthropic/claude-sonnet-4.6":  {"input": 3.00, "output": 15.00},
    "google/gemini-3.1-pro-preview-customtools:online": {"input": 1.25, "output": 5.00},
}

DEFAULT_MODEL = "openai/gpt-5.5:online"
FALLBACK_MODEL = "google/gemini-3.1-pro-preview-customtools:online"
_LEGACY_SCAN_MODELS = {
    "x-ai/grok-4-fast:online",
    "google/gemini-3.1-pro-preview-customtools:online",
}
_SCAN_PROMPT_PATH = Path(__file__).resolve().parents[2] / "SCAN-PROMPT.md"

SYSTEM_PROMPT = """# ROLE
Ты — старший крипто-аналитик и деривативный трейдер крупного хедж-фонда. Специализация — Binance USD-M futures, funding, open interest, ликвидность, импульс/mean reversion и риск по текущему счету.

# TASK
{task}

# ANALYSIS ALGORITHM
Для каждой монеты проанализируй:
1. Данные биржи: funding, 24h change/volume, price action, ликвидность, open interest и стакан, если доступно через online/tools.
2. Техническую картину: RSI, EMA20/50, Bollinger, MACD, Stoch, разворотные свечи.
3. Фундаментал и новости: unlocks, листинги/делистинги, взломы, SEC/регуляторы, ETF/news catalysts.
4. Риск с учетом BINANCE API SNAPSHOT из user message: баланс, открытые позиции, направление уже открытых сделок, перегруз по одной стороне.

# API DATA RULE
Бот передает тебе snapshot, полученный напрямую через exchange API. Считай его источником истины по счету и локальным рыночным данным. Не выдумывай баланс, позиции или API-ключи. Если online-поиск расходится со snapshot, явно предпочитай snapshot для цены/позиции/funding.

# FUNDING RATE И РИСК
Funding Rate напрямую влияет на качество стороны:
- Для SHORT: funding > +0.03% выгоден; funding < 0% ухудшает сделку.
- Для LONG: funding < -0.03% выгоден; funding > 0% ухудшает сделку.
- Экстремальный funding без подтверждения price action повышает RISK.

# OUTPUT FORMAT
КРИТИЧНО: никаких markdown-таблиц. Вместо этого выдай блоки COIN:

COIN: TICKER
SIDE: LONG или SHORT
PRICE: $X.XX
TECH: RSI 4H XX, описание
FUND: фундаментальный тезис/катализатор
FUNDING: +X.XXX% (оценка для выбранной стороны)
ENTRY: $X.XX–X.XX
SL: $X.XX
RISK: N/10

После блоков:
SENTIMENT: 2-3 предложения об общем настроении.

Правила:
- {count_rule}
- Тикер без /USDT. Каждое поле — одна строка.
- Не добавляй reasoning, thinking, анализ хода мыслей, дисклеймеры, списки вне формата и markdown-таблицы.
- Ответ должен быть коротким: только блоки COIN и одна строка SENTIMENT."""

SIGNAL_SYSTEM_PROMPT = """# ROLE
Ты — профессиональный crypto futures analyst и signal generator для Binance USD-M futures.
Твоя задача — не общаться как чат и не писать длинный анализ. Твоя задача — найти одну лучшую сделку и вернуть готовый торговый сигнал, который бот сможет открыть.

# DATA ACCESS
СНАЧАЛА проведи online research по рынку: тренды, новости, unlocks, funding, liquidity, volume, BTC context, risk-on/risk-off.
ПОТОМ используй BINANCE API SNAPSHOT от бота как источник истины для цены, funding, доступности инструмента, баланса и открытых позиций.
Если web/search и Binance snapshot расходятся, для торговых чисел доверяй Binance snapshot.

# TASK
Найди 1 лучшую позицию: LONG или SHORT.
Сигнал должен иметь Entry range, SL и ровно 3 TP.
Не возвращай список монет. Не возвращай рассуждения. Не возвращай markdown table.
Верни только один сигнал и короткое основание.

# RISK RULES
- Для SHORT: Entry выше TP, SL выше Entry.
- Для LONG: Entry ниже TP, SL ниже Entry.
- TP должно быть ровно 3: TP1, TP2, TP3.
- TP shares подразумеваются ботом: TP1 50%, TP2 25%, TP3 25%.
- SL должен быть технически обоснован: за локальным high для SHORT или за локальным low для LONG.
- Entry range должен быть узким и реалистичным по текущей цене/ликвидности.
- Leverage должен быть умеренным: x3-x10, если нет сильной причины.
- Confidence: 0-100%.
- Risk: 1-10, где 10 = опасно.

# OUTPUT FORMAT
Верни строго этот формат:

SIGNAL:
SYMBOL USDT
SIDE
Entry PRICE_MIN - PRICE_MAX
SL PRICE
TP1 PRICE
TP2 PRICE
TP3 PRICE
xLEV
Confidence NN%
Risk N/10
Reason: одна короткая строка, почему именно эта позиция

# EXAMPLE SHORT
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
Reason: funding positive, rejection from local high, volume spike fading

# EXAMPLE LONG
SIGNAL:
ETH USDT
LONG
Entry 3420 - 3440
SL 3365
TP1 3510
TP2 3580
TP3 3660
x5
Confidence 82%
Risk 5/10
Reason: oversold bounce, funding favorable, strong spot bid support
"""


@dataclass
class AnalystResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    web_searches: int = 0
    error: str | None = None


def _parse_legacy_analyst_blocks(text: str, n: int = 20) -> list[dict]:
    if not text:
        return []
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip().upper().startswith("SENTIMENT:"):
            if current:
                blocks.append(current)
                current = []
            break
        if line.strip().upper().startswith("COIN:"):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)

    parsed: list[dict] = []
    for b in blocks:
        fields: dict[str, str] = {}
        for line in b:
            m = re.match(r"\s*(COIN|SIDE|PRICE|TECH|FUND|FUNDING|ENTRY|SL|RISK)\s*:\s*(.+)", line, re.IGNORECASE)
            if m:
                fields[m.group(1).upper()] = m.group(2).strip()
        ticker = fields.get("COIN", "").strip().upper()
        if not ticker:
            continue
        side_raw = fields.get("SIDE", "SHORT").strip().lower()
        side = "long" if side_raw.startswith(("long", "buy", "лонг")) else "short"
        parsed.append({
            "ticker":   ticker,
            "side":     side,
            "price":    fields.get("PRICE", ""),
            "tech":     fields.get("TECH", ""),
            "fund":     fields.get("FUND", ""),
            "funding":  fields.get("FUNDING", ""),
            "entry":    fields.get("ENTRY", ""),
            "sl":       fields.get("SL", ""),
            "risk":     fields.get("RISK", ""),
        })
    return parsed[:n]


def parse_analyst_blocks(text: str, n: int = 20) -> list[dict]:
    legacy = _parse_legacy_analyst_blocks(text, n)
    if not text:
        return []

    parsed: list[dict] = list(legacy)
    if len(parsed) >= n:
        return parsed[:n]

    def _split_cells(line: str) -> list[str]:
        values = line.strip().strip("|")
        if not values:
            return []
        return [re.sub(r"\s+", " ", c.strip()) for c in values.split("|")]

    def _normalize_header(raw: str) -> str:
        return re.sub(r"\W+", "", raw.lower())

    def _header_to_field(header: str) -> str | None:
        h = _normalize_header(header)
        if h in {"#", "n", "num", "number"}:
            return "index"
        if "side" in h or "type" in h:
            return "side"
        if "ticker" in h or "coin" in h or "pair" in h:
            return "ticker"
        if "entry" in h:
            return "entry"
        if "tp1" in h or "tp 1" in h:
            return "tp1"
        if "tp2" in h or "tp 2" in h:
            return "tp2"
        if "tp3" in h or "tp 3" in h:
            return "tp3"
        if ("stop" in h and "loss" in h) or "sl" in h:
            return "sl"
        if "risk" in h:
            return "risk"
        if "comment" in h or "reason" in h or "rationale" in h:
            return "comment"
        if "fund" in h or "funding" in h:
            return "funding"
        if "price" in h:
            return "price"
        if h in ("плечо", "leverage", "рекомендуемое"):
            return "fund"
        return None

    def _looks_like_scan_prompt_table(row: list[str]) -> bool:
        if len(row) < 8:
            return False
        if row[0] != "#":
            return False
        return row[1] != "" and any(cell for cell in row[2:])

    def _side(raw: str) -> str:
        value = (raw or "").strip().lower()
        if value.startswith(("long", "buy", "лонг")):
            return "long"
        if value.startswith(("short", "sell", "шорт")):
            return "short"
        return "short"

    def _parse_table_row(row: list[str], header_map: dict[int, str]) -> dict | None:
        if len(row) <= 2:
            return None
        values: dict[str, str] = {}
        for idx, field in header_map.items():
            if idx < len(row):
                values[field] = row[idx]
        if "ticker" not in values:
            return None
        ticker = values.get("ticker", "").strip().split("/")[0].upper()
        if not ticker:
            return None
        return {
            "ticker": ticker,
            "side": _side(values.get("side", "short")),
            "price": values.get("entry", ""),
            "tech": values.get("comment", ""),
            "fund": values.get("fund", ""),
            "funding": values.get("funding", ""),
            "entry": values.get("entry", ""),
            "sl": values.get("sl", ""),
            "risk": values.get("risk", ""),
            "tp1": values.get("tp1", ""),
            "tp2": values.get("tp2", ""),
            "tp3": values.get("tp3", ""),
            "comment": values.get("comment", ""),
        }

    header_map: dict[int, str] = {}
    in_table = False
    for line in text.splitlines():
        if "|" not in line:
            if in_table and not line.strip():
                in_table = False
            continue

        row = _split_cells(line)
        if not row:
            continue

        if not in_table:
            mapped = {idx: _header_to_field(cell) for idx, cell in enumerate(row)}
            mapped = {idx: f for idx, f in mapped.items() if f}
            if _looks_like_scan_prompt_table(row):
                mapped = {0: "index", 1: "ticker", 2: "side", 3: "entry", 4: "tp1", 5: "tp2", 6: "tp3", 7: "sl", 8: "fund", 9: "risk", 10: "comment"}

            if mapped and "side" in mapped.values() and "ticker" in mapped.values():
                header_map = mapped
                in_table = True
            continue

        if all(re.fullmatch(r"-+", c.replace(" ", "")) for c in row):
            continue

        parsed_row = _parse_table_row(row, header_map)
        if parsed_row:
            parsed.append(parsed_row)
            if len(parsed) >= n:
                break

    unique: list[dict] = []
    seen: set[str] = set()
    for item in parsed:
        key = f"{item['ticker']}:{item['side']}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= n:
            break
    return unique


def extract_sentiment(text: str) -> str:
    m = re.search(r"SENTIMENT\s*:\s*(.+)", text or "", re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else ""


def format_usage_footer(r: AnalystResult) -> str:
    parts = [f"`{r.model}`",
             f"in={r.input_tokens:,} out={r.output_tokens:,}",
             f"*${r.cost_usd:.4f}*"]
    if r.web_searches:
        parts.insert(-1, f"searches={r.web_searches}")
    return " · ".join(parts).replace(",", " ")


def _calc_cost(model: str, in_tok: int, out_tok: int) -> float:
    prices = OPENROUTER_PRICES.get(model, {})
    return (in_tok * prices.get("input", 0) + out_tok * prices.get("output", 0)) / 1_000_000


def normalize_openrouter_model(model: str | None, force_default: bool = False) -> str:
    value = (model or "").strip()
    if value in ("gpt-5.5", "gpt-5.5:online"):
        return DEFAULT_MODEL
    if value == "openai/gpt-5.5":
        return "openai/gpt-5.5"
    if value == "openai/gpt-5.5:online":
        return value
    if force_default and (not value or value in _LEGACY_SCAN_MODELS):
        return DEFAULT_MODEL
    return value or DEFAULT_MODEL


def _max_output_tokens(n: int, mode: str) -> int:
    if mode == "signal":
        return 900
    per_pick = 360 if mode == "both" else 260
    return max(900, min(2400, n * per_pick))


def _clean_model_text(raw: str, max_chars: int = 6000) -> str:
    text = (raw or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    text = re.sub(r"```(?:text|markdown)?\s*", "", text, flags=re.IGNORECASE).replace("```", "").strip()

    lines = []
    started = False
    sentiment_seen = False
    for line in text.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if not started:
            if upper.startswith(("COIN:", "SIGNAL:")):
                started = True
            else:
                continue
        if sentiment_seen and stripped:
            break
        if re.match(r"^(REASONING|THINKING|ANALYSIS|CHAIN OF THOUGHT|РАССУЖДЕНИ|МЫСЛИ|АНАЛИЗ)\s*:", stripped, re.IGNORECASE):
            continue
        lines.append(line)
        if upper.startswith(("SENTIMENT:", "REASON:")):
            sentiment_seen = True

    cleaned = "\n".join(lines).strip() if started else text
    return cleaned[:max_chars].strip()


def _fmt_account_context(account_context: dict | None) -> str:
    if not account_context:
        return "provider=unknown\nfree_usdt=unknown\ntotal_usdt=unknown\npositions=none"

    provider = account_context.get("provider") or "unknown"
    free = account_context.get("free_usdt")
    total = account_context.get("total_usdt")
    positions = account_context.get("positions") or []

    lines = [
        f"provider={provider}",
        f"free_usdt={float(free):.2f}" if isinstance(free, (int, float)) else "free_usdt=unknown",
        f"total_usdt={float(total):.2f}" if isinstance(total, (int, float)) else "total_usdt=unknown",
    ]
    if positions:
        pos_rows = []
        for p in positions[:10]:
            sym = str(p.get("symbol", "")).split("/")[0]
            side = p.get("side", "unknown")
            pnl = float(p.get("unrealized_pnl") or 0)
            pct = float(p.get("percentage") or 0)
            pos_rows.append(f"{sym} {side} pnl=${pnl:+.2f}/{pct:+.1f}%")
        lines.append("positions=" + "; ".join(pos_rows))
    else:
        lines.append("positions=none")
    return "\n".join(lines)


def _prompt_parts(mode: str, n: int) -> tuple[str, str]:
    if mode == "signal":
        return "Найди 1 лучшую позицию и верни один готовый SIGNAL.", "Ровно один блок SIGNAL."
    if mode == "both":
        task = (
            f"Проведи экспертное исследование рынка на {{today}} и выдели РОВНО {n} монет "
            f"для LONG и РОВНО {n} монет для SHORT на Binance USD-M futures."
        )
        count_rule = f"Ровно {n} блоков SIDE: LONG и ровно {n} блоков SIDE: SHORT."
    else:
        task = (
            f"Проведи комплексное исследование текущего состояния рынка на {{today}} "
            f"и выдели РОВНО {n} монет для SHORT на Binance USD-M futures."
        )
        count_rule = f"Только {n} блоков, все с SIDE: SHORT."
    return task, count_rule


@lru_cache(maxsize=1)
def _load_scan_prompt() -> str:
    try:
        raw = _SCAN_PROMPT_PATH.read_bytes()
    except Exception:
        return ""
    for encoding in ("utf-8", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _build_system_prompt(mode: str, n: int) -> str:
    if mode == "signal":
        return SIGNAL_SYSTEM_PROMPT
    if mode == "both":
        prompt = _load_scan_prompt()
        if prompt:
            today = _dt.date.today().strftime("%Y-%m-%d")
            task_tmpl, count_rule = _prompt_parts(mode, n)
            return f"{prompt}\n\n{task_tmpl.format(today=today)}\n{count_rule}"
    today = _dt.date.today().strftime("%Y-%m-%d")
    task_tmpl, count_rule = _prompt_parts(mode, n)
    return SYSTEM_PROMPT.format(
        today=today,
        task=task_tmpl.format(today=today),
        count_rule=count_rule,
    )


def _build_user_msg_legacy(candidates: list[dict], n: int = 5,
                    account_context: dict | None = None,
                    mode: str = "both",
                    research_snapshot: dict | None = None) -> str:
    today = _dt.date.today().strftime("%Y-%m-%d")
    if candidates:
        rows = []
        for c in candidates[:max(18, n * 6)]:
            ticker = c.get("symbol", "").split("/")[0]
            funding = c.get("funding_rate", 0) or 0
            funding_str = f", funding={funding*100:+.4f}%" if funding != 0 else ""
            direction = (c.get("direction") or "watch").upper()
            rows.append(
                f"- {ticker} {direction}: RSI={c.get('rsi', 0)}, "
                f"24h={c.get('daily_change_pct', 0):+.1f}%{funding_str}"
            )
        ctx = "Локальный scanner по Binance/exchange API отметил эти монеты:\n" + "\n".join(rows)
    else:
        ctx = "(локальный scanner не нашёл кандидатов — исследуй полный рынок через online/tools, но учитывай account snapshot)"

    if mode == "both":
        request = f"Сегодня {today}. Выдай ТОП-{n} LONG и ТОП-{n} SHORT."
    elif mode == "signal":
        request = (
            f"Сегодня {today}. СНАЧАЛА проведи online research, затем по Binance snapshot "
            f"выбери одну лучшую позицию и верни один готовый SIGNAL с Entry, SL, TP1, TP2, TP3."
        )
    else:
        request = f"Сегодня {today}. Выдай ТОП-{n} SHORT."
    if mode == "both":
        request += (
            " СНАЧАЛА проведи online research по полному рынку, а не только по локальному scanner. "
            "Дай запас кандидатов: бот ПОТОМ сверит тикеры, OHLCV, funding, стакан и доступность на Binance."
        )

    api_snapshot = (
        format_research_snapshot(research_snapshot)
        if research_snapshot
        else f"BINANCE API SNAPSHOT:\n{_fmt_account_context(account_context)}"
    )
    return f"{request}\n\n{api_snapshot}\n\nLOCAL SCANNER SUMMARY:\n{ctx}"


def _build_user_msg(candidates: list[dict], n: int = 5,
                    account_context: dict | None = None,
                    mode: str = "both",
                    research_snapshot: dict | None = None) -> str:
    today = _dt.date.today().strftime("%Y-%m-%d")
    if candidates:
        rows = []
        for c in candidates[:max(18, n * 6)]:
            ticker = c.get("symbol", "").split("/")[0]
            funding = c.get("funding_rate", 0) or 0
            funding_str = f", funding={funding*100:+.4f}%" if funding != 0 else ""
            direction = (c.get("direction") or "watch").upper()
            rows.append(
                f"- {ticker} {direction}: RSI={c.get('rsi', 0)}, "
                f"24h={c.get('daily_change_pct', 0):+.1f}%{funding_str}"
            )
        ctx = "scanner summary:\n" + "\n".join(rows)
    else:
        ctx = "No local scanner candidates available, fallback to online-only research."

    if mode == "both":
        request = (
            f"{today}. Need {n} LONG and {n} SHORT opportunities. "
            "Run full online research, then verify everything by Binance API snapshot."
        )
    elif mode == "signal":
        return _build_user_msg_legacy(
            candidates,
            n=n,
            account_context=account_context,
            mode=mode,
            research_snapshot=research_snapshot,
        )
    else:
        request = f"{today}. Need {n} SHORT only."
    if mode == "both":
        request += (
            " Priority: do not rely only on scanner output; online + snapshot checks are mandatory. "
            "Squeeze the answer strictly to scan format expectations."
        )

    api_snapshot = (
        format_research_snapshot(research_snapshot)
        if research_snapshot
        else f"BINANCE API SNAPSHOT:\n{_fmt_account_context(account_context)}"
    )
    return f"{request}\n\n{api_snapshot}\n\nLOCAL SCANNER SUMMARY:\n{ctx}"


async def deep_short_analysis(candidates: list[dict], api_key: str,
                               model: str = DEFAULT_MODEL, n: int = 5,
                               account_context: dict | None = None,
                               mode: str = "short",
                               research_snapshot: dict | None = None) -> AnalystResult:
    if not api_key:
        return AnalystResult(text="", model=model, error="no api_key")

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
        )
        today = _dt.date.today().strftime("%Y-%m-%d")
        system = _build_system_prompt(mode, n)
        user_msg = _build_user_msg(
            candidates,
            n=n,
            account_context=account_context,
            mode=mode,
            research_snapshot=research_snapshot,
        )

        result = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=_max_output_tokens(n, mode),
            temperature=0.1,
            extra_body={
                "reasoning": {"exclude": True},
                "include_reasoning": False,
                "reasoning_effort": "none",
                "verbosity": "low",
            },
        )
        raw = _clean_model_text(result.choices[0].message.content or "")
        if not raw:
            return AnalystResult(text="", model=model, error="empty model response")

        usage = getattr(result, "usage", None)
        in_tok = getattr(usage, "prompt_tokens", 0) if usage else 0
        out_tok = getattr(usage, "completion_tokens", 0) if usage else 0
        cost = _calc_cost(model, in_tok, out_tok)

        return AnalystResult(
            text=raw, model=model,
            input_tokens=in_tok, output_tokens=out_tok,
            cost_usd=cost,
            web_searches=1 if model.endswith(":online") else 0,
        )
    except Exception as e:
        logger.warning("Analyst (%s) failed: %s", model, e)
        return AnalystResult(text="", model=model, error=str(e))


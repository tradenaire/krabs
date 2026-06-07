"""LLM analyst — ТОП-5 шортов через OpenRouter (Grok Online) или Claude."""
from __future__ import annotations

import datetime as _dt
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

OPENROUTER_PRICES = {
    "x-ai/grok-4-fast:online":      {"input": 0.20, "output": 0.50},
    "anthropic/claude-sonnet-4.6":  {"input": 3.00, "output": 15.00},
    "google/gemini-3.1-pro-preview-customtools:online": {"input": 1.25, "output": 5.00},
}

DEFAULT_MODEL = "x-ai/grok-4-fast:online"
FALLBACK_MODEL = "google/gemini-3.1-pro-preview-customtools:online"

SYSTEM_PROMPT = """# ROLE
Ты — старший аналитик отдела количественного анализа в крупном хедж-фонде. Твоя задача — находить НАИМЕНЕЕ РИСКОВЫЕ сделки по всему рынку крипто-фьючерсов, в ОБЕ стороны: и в шорт (Short), и в лонг (Long).

# TASK
Проведи комплексное исследование текущего состояния рынка на {today} и выдели РОВНО {n} САМЫХ НАДЁЖНЫХ сделок (минимальный риск), отсортированных от наименее рискованной к более рискованной. Для каждой определи направление (long или short).

# ANALYSIS ALGORITHM
Для каждой монеты проанализируй:
1. Тех. картина: RSI 4H/1D, EMA20/50, дивергенции, перекупленность/перепроданность.
   - Шорт: перегрев (RSI>70), отбой от сопротивления, медвежья дивергенция.
   - Лонг: перепроданность (RSI<30), отбой от поддержки, бычья дивергенция.
2. Фундаментал: новости, взломы, SEC, token unlocks в ближайшие 7 дней.
3. Биржевые данные: Funding Rate, Open Interest, объёмы.

# FUNDING RATE И РИСК
- Шорт: Funding > +0.03% → шортить выгодно (RISK ниже); Funding < 0% → шорт платит, RISK выше.
- Лонг: Funding < 0% → лонг получает выплаты (RISK ниже); Funding сильно положительный → лонг платит, RISK выше.

# RISK
RISK N/10 — оценка риска сделки (1 = самая надёжная, 10 = очень рискованная). Приоритет — низкий RISK.

# TAKE PROFITS
Для каждой сделки рассчитай ТРИ цели тейк-профита (TP1<TP2<TP3 для лонга по удалению от entry; для шорта цены ниже entry, TP1 ближе всех). TP1 — консервативная близкая цель, TP3 — амбициозная. SL — за ближайшим инвалидирующим уровнем.

# OUTPUT FORMAT
КРИТИЧНО: никаких markdown-таблиц. Вместо этого {n} блоков COIN (отсортированы по возрастанию RISK):

COIN: TICKER
SIDE: long|short
PRICE: $X.XX
TECH: RSI 4H XX, описание
FUND: фундаментал
FUNDING: +X.XXX% (оценка)
ENTRY: $X.XX–X.XX
TP1: $X.XX
TP2: $X.XX
TP3: $X.XX
SL: $X.XX
RISK: N/10

После блоков:
SENTIMENT: 2-3 предложения об общем настроении рынка.

Правила:
- Ровно {n} блоков. Тикер без /USDT. Каждое поле — одна строка. SIDE строго long или short."""


@dataclass
class AnalystResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    web_searches: int = 0
    error: str | None = None


def parse_analyst_blocks(text: str, n: int = 20) -> list[dict]:
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
            m = re.match(r"\s*(COIN|SIDE|PRICE|TECH|FUND|FUNDING|ENTRY|TP1|TP2|TP3|SL|RISK)\s*:\s*(.+)",
                         line, re.IGNORECASE)
            if m:
                fields[m.group(1).upper()] = m.group(2).strip()
        ticker = fields.get("COIN", "").strip().upper()
        if not ticker:
            continue
        side_raw = fields.get("SIDE", "").strip().lower()
        side = "long" if side_raw.startswith("long") else ("short" if side_raw.startswith("short") else "")
        parsed.append({
            "ticker":   ticker,
            "side":     side,
            "price":    fields.get("PRICE", ""),
            "tech":     fields.get("TECH", ""),
            "fund":     fields.get("FUND", ""),
            "funding":  fields.get("FUNDING", ""),
            "entry":    fields.get("ENTRY", ""),
            "tp1":      fields.get("TP1", ""),
            "tp2":      fields.get("TP2", ""),
            "tp3":      fields.get("TP3", ""),
            "sl":       fields.get("SL", ""),
            "risk":     fields.get("RISK", ""),
        })
    return parsed[:n]


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


def _build_user_msg(candidates: list[dict], n: int = 5) -> str:
    today = _dt.date.today().strftime("%Y-%m-%d")
    if candidates:
        rows = []
        for c in candidates[:max(15, n * 3)]:
            ticker = c.get("symbol", "").split("/")[0]
            funding = c.get("funding_rate", 0) or 0
            funding_str = f", funding={funding*100:+.4f}%" if funding != 0 else ""
            rows.append(
                f"- {ticker}: RSI={c.get('rsi', 0)}, 24h={c.get('daily_change_pct', 0):+.1f}%{funding_str}"
            )
        ctx = "Наш локальный сканер отметил эти монеты (могут быть кандидаты и в лонг, и в шорт):\n" + "\n".join(rows)
    else:
        ctx = "(локальный сканер не нашёл кандидатов — иди от полного рынка)"
    return (f"Сегодня {today}. Выдай ТОП-{n} НАИМЕНЕЕ РИСКОВЫХ сделок по всему рынку, "
            f"в обе стороны (long/short), отсортированных по возрастанию RISK.\n\n{ctx}")


async def deep_short_analysis(candidates: list[dict], api_key: str,
                               model: str = DEFAULT_MODEL, n: int = 5) -> AnalystResult:
    if not api_key:
        return AnalystResult(text="", model=model, error="no api_key")

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
        )
        today = _dt.date.today().strftime("%Y-%m-%d")
        system = SYSTEM_PROMPT.format(today=today, n=n)
        user_msg = _build_user_msg(candidates, n=n)

        result = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=max(2500, n * 500),
            temperature=0.2,
        )
        raw = (result.choices[0].message.content or "").strip()
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

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

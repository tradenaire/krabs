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


def _normalize_coin(value: str) -> str:
    ticker = (value or "").strip().upper()
    ticker = re.split(r"[/_\-\s]", ticker, maxsplit=1)[0]
    return re.sub(r"[^A-Z0-9]", "", ticker)

SYSTEM_PROMPT = """# ROLE
Ты — старший аналитик отдела количественного анализа в крупном хедж-фонде. Специализация — поиск активов с высоким потенциалом падения (Short opportunities).

# TASK
Проведи комплексное исследование текущего состояния рынка на {today} и выдели РОВНО {n} монет для шорта.

# HARD DATA RULES
- Выбирай ТОЛЬКО из MEXC futures-кандидатов, которые пользователь передал в сообщении.
- Не придумывай тикеры вне списка и не меняй написание COIN.
- Цена, RSI, funding, trend/MSB и risk из локального MEXC snapshot имеют приоритет над твоей оценкой.
- Если у монеты нет подтверждения смены тренда/MSB, не ставь её выше монеты с подтверждением.
- Если данных не хватает, напиши это в TECH/FUNDING, но формат не ломай.

# ANALYSIS ALGORITHM
Для каждой монеты проанализируй:
1. Технический перегрев: RSI 4H и 1D >70, отклонение от EMA20/50
2. Фундаментальный негатив: взломы, SEC, token unlocks в ближайшие 7 дней
3. Биржевые данные: Funding Rate, Exchange Inflow, Open Interest

# FUNDING RATE И РИСК
Funding Rate напрямую влияет на качество шорта:
- Funding > +0.03% → лонги перегреты, шортить выгодно (снижай RISK на 1-2 пункта)
- Funding 0..+0.03% → нейтрально
- Funding < 0% → шорт платит funding, невыгодно (повышай RISK на 1-2 пункта)
- Funding < -0.05% → шорт очень дорогой, только при сильном техническом сигнале (RISK не ниже 7/10)

# OUTPUT FORMAT
КРИТИЧНО: никаких markdown-таблиц. Вместо этого {n} блоков COIN:

COIN: TICKER
PRICE: $X.XX
TECH: RSI 4H XX, описание
FUND: фундаментальный негатив
FUNDING: +X.XXX% (оценка: выгодно/нейтрально/дорогой шорт)
ENTRY: $X.XX–X.XX
SL: $X.XX
RISK: N/10

После блоков:
SENTIMENT: 2-3 предложения об общем настроении.

Правила:
- Только {n} блоков. Тикер без /USDT. Каждое поле — одна строка."""


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
            m = re.match(r"\s*(COIN|PRICE|TECH|FUND|FUNDING|ENTRY|SL|RISK)\s*:\s*(.+)", line, re.IGNORECASE)
            if m:
                fields[m.group(1).upper()] = m.group(2).strip()
        ticker = _normalize_coin(fields.get("COIN", ""))
        if not ticker:
            continue
        risk_text = fields.get("RISK", "")
        risk_match = re.search(r"(\d+)", risk_text)
        risk_num = int(risk_match.group(1)) if risk_match else None
        if risk_num is not None and not 1 <= risk_num <= 10:
            logger.info("skip LLM candidate %s: risk out of range: %s", ticker, risk_text)
            continue
        parsed.append({
            "ticker":   ticker,
            "price":    fields.get("PRICE", ""),
            "tech":     fields.get("TECH", ""),
            "fund":     fields.get("FUND", ""),
            "funding":  fields.get("FUNDING", ""),
            "entry":    fields.get("ENTRY", ""),
            "sl":       fields.get("SL", ""),
            "risk":     risk_text,
            "risk_num": risk_num,
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
            tf = c.get("timeframes", {}) or {}
            tf_parts = []
            for name in ("1h", "4h", "1d"):
                item = tf.get(name) or {}
                if item.get("rsi") is not None:
                    tf_parts.append(
                        f"{name}:RSI={item.get('rsi')},EMA={item.get('ema_trend')},"
                        f"MSB={item.get('msb_short')}"
                    )
            tf_str = "; ".join(tf_parts) if tf_parts else "tf=нет"
            reasons = "; ".join((c.get("reasons") or [])[:3])
            rows.append(
                f"- {ticker}: mexc_symbol={c.get('symbol')}, price={c.get('price')}, "
                f"score={c.get('score')}, gate={c.get('validation_status', 'UNKNOWN')}, "
                f"risk={c.get('risk_score', '?')}/10, trend_change={c.get('trend_change_short')}, "
                f"msb={c.get('msb_short')}, 24h={c.get('daily_change_pct', 0):+.1f}%{funding_str}, "
                f"{tf_str}. Reasons: {reasons}"
            )
        ctx = (
            "Наш локальный MEXC-first сканер отметил эти монеты. "
            "Выбирай только из них, COIN должен совпадать с тикером в начале строки:\n"
            + "\n".join(rows)
        )
    else:
        ctx = "(локальный MEXC-first сканер не нашёл кандидатов; верни SENTIMENT и не придумывай COIN)"
    return f"Сегодня {today}. Выдай ТОП-{n} монет для шорта.\n\n{ctx}"


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

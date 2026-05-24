"""LLM analyst — ТОП-5 шортов через OpenAI-compatible AI provider."""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

OPENROUTER_PRICES = {
    "PROXY-CODEX-5.4":             {"input": 0.00, "output": 0.00},
    "google/gemini-3.1-flash-lite":      {"input": 0.20, "output": 0.50},
    "anthropic/claude-sonnet-4.6":  {"input": 3.00, "output": 15.00},
    "google/gemini-3.1-pro-preview-customtools:online": {"input": 1.25, "output": 5.00},
}

DEFAULT_MODEL = "PROXY-CODEX-5.4"
DEFAULT_BASE_URL = "https://codex.aimulticast.org/v1"
FALLBACK_MODEL = DEFAULT_MODEL

TRUSTED_CRYPTO_DOMAINS = (
    "sec.gov",
    "justice.gov",
    "cftc.gov",
    "binance.com",
    "mexc.com",
    "coinbase.com",
    "token.unlocks.app",
    "cryptorank.io",
    "defillama.com",
    "coingecko.com",
    "coinmarketcap.com",
    "coindesk.com",
    "theblock.co",
    "cointelegraph.com",
    "decrypt.co",
    "rekt.news",
    "certik.com",
    "slowmist.com",
)

NEGATIVE_WEB_KEYWORDS = (
    "unlock", "hack", "hacked", "exploit", "exploited", "lawsuit", "sued",
    "sec", "cftc", "justice department", "doj", "delisting", "delist",
    "investigation", "charges", "charged", "breach", "stolen", "drained",
    "halt", "suspension",
)

WEB_RESEARCH_HEADER = """# TRUSTED WEB CONTEXT
Ниже результаты DuckDuckGo только с доверенных доменов. Используй их как проверяемый веб-контекст.
Если по монете нет результатов ниже, не выдумывай новости/SEC/unlocks/hacks."""


def _normalize_coin(value: str) -> str:
    ticker = (value or "").strip().upper()
    ticker = re.split(r"[/_\-\s]", ticker, maxsplit=1)[0]
    return re.sub(r"[^A-Z0-9]", "", ticker)

SYSTEM_PROMPT = """# ROLE
Ты — старший аналитик отдела количественного анализа и криминалистики блокчейна в крупном хедж-фонде.
Твоя специализация — поиск неэффективностей рынка и активов с высоким потенциалом падения (short opportunities).

# CONTEXT
Рынок криптовалют волатилен. Нужно найти до {n} наиболее перспективных активов для коротких позиций
на основе комбинации технического перегрева, негативного фундаментального фона, биржевых данных и ончейн-сигналов.

# TASK
Проведи комплексное исследование текущего состояния рынка на {today} и выдели лучшие монеты для шорта.
Используй web-search самостоятельно. Локальный MEXC snapshot из сообщения пользователя — полезный контекст,
но не ограничение. Если локальный scanner пустой, всё равно ищи кандидатов через интернет.
Бот после твоего ответа сам проверит тикеры на MEXC futures.

# ANALYSIS ALGORITHM
Для каждой потенциальной монеты проанализируй:
1. Технический перегрев:
   - RSI на 4H и 1D, особенно >70 с признаками разворота или медвежьей дивергенции.
   - Отклонение от EMA20/EMA50.
2. Фундаментальный негатив:
   - Последние новости: exploits/hacks, SEC/regulatory claims, технические сбои сети, delistings.
   - Крупные token unlocks в ближайшие 7 дней.
3. Биржевые и ончейн данные:
   - Funding Rate: аномально высокий positive funding как признак перегрева лонгов.
   - Exchange inflow: рост ввода монет на биржи как риск фиксации прибыли китами.
   - Open Interest: высокая нагрузка OI на фоне слабой цены.

# SCORING
RISK 1/10 = лучший short setup с сильным подтверждением. RISK 10/10 = очень рискованно.
Если данных мало, всё равно дай лучшие candidates и честно отметь слабые места.

# OUTPUT FORMAT
CRITICAL: response must be machine-parseable. Each field must be on its own line. Do not concatenate fields like COIN: BTCPRICE: ...
КРИТИЧНО: никаких markdown-таблиц. Верни до {n} блоков COIN:

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
- Тикер без /USDT. Каждое поле — одна строка.
- Если совсем нет уверенности, всё равно верни 1-3 наиболее вероятных кандидата с высоким RISK."""


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
    field_names = "COIN|PRICE|TECH|FUNDING|FUND|ENTRY|SL|RISK|SENTIMENT"
    text = re.sub(rf"(?<!^)\s*(?=({field_names})\s*:)", "\n", text.strip(), flags=re.IGNORECASE)
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
            m = re.match(r"\s*(COIN|PRICE|TECH|FUNDING|FUND|ENTRY|SL|RISK)\s*:\s*(.+)", line, re.IGNORECASE)
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


def _usage_cost_usd(usage) -> float | None:
    if not usage:
        return None
    for name in ("cost", "cost_usd", "total_cost"):
        value = getattr(usage, name, None)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    if isinstance(usage, dict):
        for name in ("cost", "cost_usd", "total_cost"):
            if usage.get(name) is not None:
                try:
                    return float(usage[name])
                except (TypeError, ValueError):
                    pass
    return None


def _build_user_msg(candidates: list[dict], n: int = 5, exclude_tickers: set[str] | None = None) -> str:
    today = _dt.date.today().strftime("%Y-%m-%d")
    exclude_tickers = {t.upper() for t in (exclude_tickers or set())}
    if candidates:
        rows = []
        for c in candidates[:max(15, n * 3)]:
            ticker = c.get("symbol", "").split("/")[0].upper()
            if ticker in exclude_tickers:
                continue
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
        ctx = (
            "Локальный MEXC-first сканер не нашёл кандидатов. "
            "Сначала используй web-search и текущий рынок, найди short candidates сам, "
            "верни COIN-блоки; бот затем проверит каждый тикер на MEXC futures."
        )
    exclude = f"\n\nEXCLUDE already-open coins: {', '.join(sorted(exclude_tickers))}. Do not return any COIN from EXCLUDE." if exclude_tickers else ""
    return f"Scan top-{n} crypto shorts.\n\n{ctx}{exclude}"


def _trusted_host(url: str) -> str:
    host = urlparse(url).netloc.lower().lstrip("www.")
    for domain in TRUSTED_CRYPTO_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return domain
    return ""


def _is_usable_trusted_result(url: str, title: str, body: str, ticker: str) -> str:
    domain = _trusted_host(url)
    if not domain:
        return ""
    path = urlparse(url).path.lower()
    if domain == "binance.com" and "/square" in path:
        return ""
    haystack = f"{title} {body} {url}".upper()
    if not re.search(rf"(?<![A-Z0-9]){re.escape(ticker.upper())}(?![A-Z0-9])", haystack):
        return ""
    text = f"{title} {body}".lower()
    if not any(k in text for k in NEGATIVE_WEB_KEYWORDS):
        return ""
    return domain


def _candidate_tickers(candidates: list[dict], n: int) -> list[str]:
    tickers: list[str] = []
    for c in candidates[:max(8, n * 2)]:
        ticker = _normalize_coin(str(c.get("symbol", "")).split("/")[0])
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    return tickers[:10]


def _duckduckgo_research_sync(candidates: list[dict], n: int) -> tuple[str, int]:
    tickers = _candidate_tickers(candidates, n)
    if not tickers:
        return "", 0

    try:
        from ddgs import DDGS
    except Exception as e:
        logger.warning("DuckDuckGo search unavailable: %s", e)
        return "", 0

    lines: list[str] = []
    seen_urls: set[str] = set()
    search_count = 0
    keywords = "crypto unlock OR hack OR exploit OR lawsuit OR delisting"

    try:
        with DDGS() as ddgs:
            for ticker in tickers:
                trusted_rows: list[str] = []
                for row in ddgs.text(f"{ticker} {keywords}", max_results=10):
                    url = str(row.get("href") or row.get("url") or "")
                    title = re.sub(r"\s+", " ", str(row.get("title") or "")).strip()
                    body = re.sub(r"\s+", " ", str(row.get("body") or "")).strip()
                    domain = _is_usable_trusted_result(url, title, body, ticker)
                    if not domain or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    trusted_rows.append(f"- {domain}: {title} — {body[:260]} ({url})")
                    if len(trusted_rows) >= 3:
                        break
                search_count += 1
                if trusted_rows:
                    lines.append(f"{ticker}:")
                    lines.extend(trusted_rows)
    except Exception as e:
        logger.warning("DuckDuckGo search failed: %s", e)
        return "", search_count

    if not lines:
        return "", search_count
    return WEB_RESEARCH_HEADER + "\n" + "\n".join(lines), search_count


async def _duckduckgo_research(candidates: list[dict], n: int) -> tuple[str, int]:
    return await asyncio.to_thread(_duckduckgo_research_sync, candidates, n)


async def deep_short_analysis(candidates: list[dict], api_key: str,
                               model: str = DEFAULT_MODEL, n: int = 5,
                               exclude_tickers: set[str] | None = None,
                               base_url: str = DEFAULT_BASE_URL,
                               web_search: bool = True) -> AnalystResult:
    if not api_key:
        return AnalystResult(text="", model=model, error="no api_key")

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        today = _dt.date.today().strftime("%Y-%m-%d")
        system = SYSTEM_PROMPT.format(today=today, n=n)
        user_msg = _build_user_msg(candidates, n=n, exclude_tickers=exclude_tickers)
        web_context = ""
        web_searches = 0
        if web_search:
            web_context, web_searches = await _duckduckgo_research(candidates, n=n)
            if web_context:
                user_msg += "\n\n" + web_context

        request_kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            "max_tokens": max(6000, n * 900),
            "temperature": 0.2,
        }
        if "openrouter.ai" in (base_url or ""):
            request_kwargs["extra_body"] = {
                "usage": {"include": True},
                "reasoning": {"effort": "low", "exclude": True},
            }
        try:
            result = await client.chat.completions.create(**request_kwargs)
        except Exception as e:
            if "reasoning" not in str(e).lower():
                raise
            request_kwargs.pop("extra_body", None)
            if "openrouter.ai" in (base_url or ""):
                request_kwargs["extra_body"] = {"usage": {"include": True}}
            result = await client.chat.completions.create(**request_kwargs)
        raw = (result.choices[0].message.content or "").strip()
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

        usage = getattr(result, "usage", None)
        in_tok = getattr(usage, "prompt_tokens", 0) if usage else 0
        out_tok = getattr(usage, "completion_tokens", 0) if usage else 0
        cost = _usage_cost_usd(usage)
        if cost is None:
            cost = _calc_cost(model, in_tok, out_tok)

        return AnalystResult(
            text=raw, model=model,
            input_tokens=in_tok, output_tokens=out_tok,
            cost_usd=cost,
            web_searches=web_searches or (1 if model.endswith(":online") else 0),
        )
    except Exception as e:
        logger.warning("Analyst (%s) failed: %s", model, e)
        return AnalystResult(text="", model=model, error=str(e))

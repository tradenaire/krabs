"""Technical coin scanner — RSI, MACD, BB, Stoch, EMA, volume."""
import logging
import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)


async def scan_overbought(exchange, rsi_threshold: float = 65.0,
                          daily_change_threshold: float = 10.0,
                          max_symbols: int = 80) -> tuple[list[dict], int]:
    await exchange._exchange.load_markets()

    tradeable_symbols: set[str] = set()
    try:
        contracts = await exchange.get_contract_details()
        for c in contracts:
            if c.get("state") == 0 and not c.get("isHidden"):
                tradeable_symbols.add(c.get("symbol", ""))
    except Exception as e:
        logger.warning("Contract details failed: %s", e)

    swap_markets = [
        s for s, m in exchange._exchange.markets.items()
        if m.get("type") == "swap" and m.get("settle") == "USDT"
        and m.get("active", False)
        and (not tradeable_symbols or m.get("id", "") in tradeable_symbols)
    ]
    total = len(swap_markets)
    logger.info("Scanning %d swap markets", total)

    all_tickers: dict = {}
    for i in range(0, len(swap_markets), 200):
        try:
            batch = await exchange._exchange.fetch_tickers(swap_markets[i:i+200])
            all_tickers.update(batch)
        except Exception as e:
            logger.warning("Ticker batch error: %s", e)

    candidates = [
        (sym, ticker, float(ticker.get("percentage", 0) or 0))
        for sym, ticker in all_tickers.items()
        if abs(float(ticker.get("percentage", 0) or 0)) >= daily_change_threshold
        and float(ticker.get("quoteVolume", 0) or 0) >= 10_000_000
    ]
    candidates.sort(key=lambda x: abs(x[2]), reverse=True)
    candidates = candidates[:max_symbols]

    results = []
    for sym, ticker, daily_change in candidates:
        try:
            ohlcv = await exchange._exchange.fetch_ohlcv(sym, "1h", limit=100)
            if not ohlcv or len(ohlcv) < 30:
                continue
        except Exception:
            continue
        analysis = _deep_analyze(sym, ohlcv, ticker, daily_change)
        if analysis:
            results.append(analysis)

    results.sort(key=lambda x: x["score"], reverse=True)

    verified = []
    for r in results:
        if len(verified) >= 10:
            break
        try:
            ob = await exchange._exchange.fetch_order_book(r["symbol"], limit=5)
            if ob.get("bids") and ob.get("asks"):
                verified.append(r)
        except Exception:
            pass

    return verified, total


async def analyze_single_coin(exchange, symbol: str) -> dict | None:
    try:
        ticker = await exchange._exchange.fetch_ticker(symbol)
        ohlcv = await exchange._exchange.fetch_ohlcv(symbol, "1h", limit=100)
        if not ohlcv or len(ohlcv) < 30:
            return None
    except Exception as e:
        logger.info("analyze_single_coin(%s): %s", symbol, e)
        return None
    daily_change = float(ticker.get("percentage", 0) or 0)
    return _deep_analyze(symbol, ohlcv, ticker, daily_change, min_score=0)


async def mexc_find_futures_symbol(exchange, ticker: str) -> str | None:
    try:
        await exchange._exchange.load_markets()
    except Exception:
        return None
    for sym in [f"{ticker}/USDT:USDT", f"{ticker}/USDT"]:
        m = exchange._exchange.markets.get(sym)
        if m and m.get("active") and m.get("type") == "swap" and m.get("settle") == "USDT":
            return sym
    mexc_id = f"{ticker}_USDT"
    for sym, m in exchange._exchange.markets.items():
        if m.get("id") == mexc_id and m.get("active") \
                and m.get("type") == "swap" and m.get("settle") == "USDT":
            return sym
    return None


def _deep_analyze(symbol: str, ohlcv: list, ticker: dict, daily_change: float,
                  min_score: int = 20) -> dict | None:
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    price = float(ticker.get("last", 0) or 0)
    if price == 0:
        return None

    rsi = ta.rsi(df["close"], length=14)
    ema20 = ta.ema(df["close"], length=20)
    ema50 = ta.ema(df["close"], length=50)
    stoch = ta.stoch(df["high"], df["low"], df["close"], k=14, d=3)
    macd_df = ta.macd(df["close"])
    bb = ta.bbands(df["close"], length=20)

    if rsi is None or rsi.empty:
        return None

    rsi_now = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else 50
    rsi_prev = float(rsi.iloc[-2]) if len(rsi) > 1 and pd.notna(rsi.iloc[-2]) else rsi_now

    macd_hist = 0
    if macd_df is not None and len(macd_df) > 0:
        v, s = (float(macd_df.iloc[-1, 0]) if pd.notna(macd_df.iloc[-1, 0]) else 0,
                float(macd_df.iloc[-1, 2]) if pd.notna(macd_df.iloc[-1, 2]) else 0)
        macd_hist = v - s

    bb_pos = 0.5
    if bb is not None and len(bb) > 0:
        bb_upper = float(bb.iloc[-1, 0]) if pd.notna(bb.iloc[-1, 0]) else price
        bb_lower = float(bb.iloc[-1, 2]) if pd.notna(bb.iloc[-1, 2]) else price
        r = bb_upper - bb_lower
        if r > 0:
            bb_pos = (price - bb_lower) / r

    ema_20 = float(ema20.iloc[-1]) if ema20 is not None and pd.notna(ema20.iloc[-1]) else price
    ema_50 = float(ema50.iloc[-1]) if ema50 is not None and pd.notna(ema50.iloc[-1]) else price
    ema_bullish = ema_20 > ema_50
    price_below_ema20 = price < ema_20

    stoch_k, stoch_d, stoch_bearish_cross = 50.0, 50.0, False
    if stoch is not None and not stoch.empty and len(stoch) > 1:
        kc = [c for c in stoch.columns if "STOCHk" in c]
        dc = [c for c in stoch.columns if "STOCHd" in c]
        if kc and dc:
            stoch_k = float(stoch[kc[0]].iloc[-1]) if pd.notna(stoch[kc[0]].iloc[-1]) else 50
            stoch_d = float(stoch[dc[0]].iloc[-1]) if pd.notna(stoch[dc[0]].iloc[-1]) else 50
            kp = float(stoch[kc[0]].iloc[-2]) if pd.notna(stoch[kc[0]].iloc[-2]) else stoch_k
            dp = float(stoch[dc[0]].iloc[-2]) if pd.notna(stoch[dc[0]].iloc[-2]) else stoch_d
            stoch_bearish_cross = (stoch_k < stoch_d) and (kp >= dp)

    funding_rate = float(ticker.get("info", {}).get("fundingRate", 0) or 0)
    vol_now = float(df["volume"].iloc[-1])
    vol_avg = float(df["volume"].iloc[-20:].mean())
    vol_spike = vol_now / vol_avg if vol_avg > 0 else 1

    last = df.iloc[-1]
    body = abs(last["close"] - last["open"])
    upper_wick = last["high"] - max(last["close"], last["open"])
    lower_wick = min(last["close"], last["open"]) - last["low"]
    long_upper_wick = upper_wick > body * 2 if body > 0 else False
    long_lower_wick = lower_wick > body * 2 if body > 0 else False

    price_higher = float(df["close"].iloc[-1]) > float(df["close"].iloc[-5])
    rsi_lower = rsi_now < float(rsi.iloc[-5]) if len(rsi) > 5 and pd.notna(rsi.iloc[-5]) else False
    bearish_divergence = price_higher and rsi_lower

    # Price proximity to local high (last 20 candles) — fresh vs mid-correction
    high_20 = float(df["high"].iloc[-20:].max())
    near_local_high = high_20 > 0 and (high_20 - price) / high_20 <= 0.05

    # RSI turning down from overbought
    rsi_prev2 = float(rsi.iloc[-3]) if len(rsi) > 3 and pd.notna(rsi.iloc[-3]) else rsi_prev
    rsi_turning_down = rsi_now < rsi_prev and rsi_prev >= 70

    # At least one reversal signal required for short (not just overbought)
    reversal_confirmed = (
        bearish_divergence
        or long_upper_wick
        or stoch_bearish_cross
        or rsi_turning_down
    )

    score = 0
    direction = "short"
    reasons: list[str] = []

    if daily_change > 0:
        # Mandatory reversal filter — skip if no confirmation signal
        if not reversal_confirmed:
            return None

        if rsi_now >= 80:
            score += 30; reasons.append(f"RSI {rsi_now:.0f} — сильно перекуплена")
        elif rsi_now >= 70:
            score += 15; reasons.append(f"RSI {rsi_now:.0f} — перекуплена")
        if rsi_now < rsi_prev and rsi_prev >= 75:
            score += 20; reasons.append("RSI разворачивается вниз")
        if bearish_divergence:
            score += 25; reasons.append("Медвежья дивергенция RSI")
        if long_upper_wick:
            score += 15; reasons.append("Длинная верхняя тень — отбой")
        if macd_hist < 0 and daily_change > 20:
            score += 15; reasons.append("MACD пересёк вниз")
        if bb_pos > 0.95:
            score += 10; reasons.append("Цена у верхней Боллинджер")
        if vol_spike > 2 and rsi_now >= 70:
            score += 10; reasons.append(f"Объём x{vol_spike:.1f} — кульминация")
        if stoch_k > 80 and stoch_d > 80:
            score += 15; reasons.append(f"Stoch {stoch_k:.0f}/{stoch_d:.0f} — перекуплен")
        if stoch_bearish_cross and stoch_k > 70:
            score += 20; reasons.append("Stoch %K пересёк %D вниз")
        if price_below_ema20:
            score += 10; reasons.append("Цена ниже EMA20")
        if not ema_bullish:
            score += 10; reasons.append("EMA20 < EMA50 — медвежий тренд")
        if funding_rate > 0.0003:
            score += 15; reasons.append(f"Funding +{funding_rate*100:.3f}% — лонги перегреты")
        elif funding_rate > 0.0001:
            score += 5; reasons.append(f"Funding +{funding_rate*100:.3f}%")
        elif funding_rate < -0.0003:
            score -= 15; reasons.append(f"Funding {funding_rate*100:.3f}% — шорт дорогой")
        if ema_bullish and rsi_now < 80:
            score -= 10; reasons.append("EMA бычий тренд — осторожно")
        if near_local_high:
            score += 10; reasons.append(f"Цена у локального хая (-{(high_20-price)/high_20*100:.1f}%)")

    elif daily_change < -10:
        direction = "long"
        if rsi_now <= 25:
            score += 30; reasons.append(f"RSI {rsi_now:.0f} — сильно перепродана")
        elif rsi_now <= 35:
            score += 15; reasons.append(f"RSI {rsi_now:.0f} — перепродана")
        if rsi_now > rsi_prev and rsi_prev <= 30:
            score += 20; reasons.append("RSI разворачивается вверх")
        if long_lower_wick:
            score += 15; reasons.append("Длинная нижняя тень — отбой")
        if macd_hist > 0:
            score += 10; reasons.append("MACD пересёк вверх")

    if vol_spike > 1.5:
        score += 5

    if score < min_score:
        return None

    potential_pct = min((rsi_now - 50) * 0.5, 30) if direction == "short" \
        else min((50 - rsi_now) * 0.5, 30)

    return {
        "symbol": symbol,
        "direction": direction,
        "rsi": round(rsi_now, 1),
        "daily_change_pct": round(daily_change, 1),
        "price": price,
        "volume_24h": float(ticker.get("quoteVolume", 0) or 0),
        "vol_spike": round(vol_spike, 1),
        "bb_position": round(bb_pos, 2),
        "macd_hist": round(macd_hist, 6),
        "ema_trend": "бычий" if ema_bullish else "медвежий",
        "stoch_k": round(stoch_k, 1),
        "stoch_d": round(stoch_d, 1),
        "funding_rate": round(funding_rate, 6),
        "potential_pct": round(potential_pct, 1),
        "score": score,
        "reasons": reasons,
        "reversal_confirmed": reversal_confirmed,
        "near_local_high": near_local_high,
    }


def format_coin_card(r: dict, index: int, ai_note: str = "",
                     max_lev: int = 0, margin: float = 0.0) -> str:
    coin = r["symbol"].split("/")[0]
    dir_emoji = "🔻" if r["direction"] == "short" else "🔺"
    reasons_text = "\n".join(f"    • {x}" for x in r["reasons"][:4])
    vol_24h = r.get("volume_24h", 0)
    vol_str = f"${vol_24h/1e6:.1f}M" if vol_24h >= 1e6 else f"${vol_24h/1e3:.0f}K"
    note_line = f"\n   📰 {ai_note}" if ai_note else ""
    lev_line = ""
    if max_lev > 0 and margin > 0:
        notional = margin * max_lev
        lev_line = f"\n   ⚙️ Плечо `×{max_lev}` | Маржа `${margin:.2f}` | Поза `~${notional:.0f}`"
    return (
        f"{index}. {dir_emoji} *{coin}*\n"
        f"   RSI `{r['rsi']}` | 24ч `{r['daily_change_pct']:+.1f}%` | Объём `{vol_str}`\n"
        f"   Тренд: {r['ema_trend']} | BB: `{r['bb_position']:.0%}`{note_line}"
        f"{lev_line}\n"
        f"   *Почему:*\n{reasons_text}"
    )

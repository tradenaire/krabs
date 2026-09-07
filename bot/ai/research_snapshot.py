"""Build safe exchange/account snapshots for LLM market research."""
from __future__ import annotations

import datetime as _dt
import asyncio
import math
from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _symbol_coin(symbol: str) -> str:
    return str(symbol or "").split("/")[0]


def _safe_text(value: Any, max_len: int = 160) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text[:max_len]


async def _try(errors: list[str], label: str, coro, default):
    try:
        return await asyncio.wait_for(coro, timeout=5)
    except Exception as e:
        errors.append(f"{label}: {type(e).__name__}")
        return default


def _extract_balance(balance: dict) -> dict:
    if not balance:
        return {}
    usdt = balance.get("USDT", {}) if isinstance(balance, dict) else {}
    raw = balance.get("_raw", {}) if isinstance(balance, dict) else {}
    free = _f(usdt.get("free"), _f(raw.get("availableOpen"), _f(raw.get("availableBalance"))))
    total = _f(usdt.get("total"), _f(raw.get("equity"), _f(raw.get("cashBalance"))))
    used = _f(usdt.get("used"), _f(raw.get("positionMargin")))
    return {
        "free_usdt": round(free, 4),
        "total_usdt": round(total, 4),
        "used_usdt": round(used, 4),
    }


def _normalize_position(pos: dict) -> dict:
    return {
        "symbol": pos.get("symbol", ""),
        "side": pos.get("side", ""),
        "entry_price": _f(pos.get("entry_price")),
        "mark_price": _f(pos.get("mark_price")),
        "liquidation_price": _f(pos.get("liquidation_price")),
        "unrealized_pnl": round(_f(pos.get("unrealized_pnl")), 4),
        "percentage": round(_f(pos.get("percentage")), 2),
        "margin": round(_f(pos.get("margin")), 4),
        "leverage": int(_f(pos.get("leverage"), 1) or 1),
        "contracts": _f(pos.get("contracts")),
    }


def _normalize_order(order: dict) -> dict:
    return {
        "symbol": order.get("symbol", ""),
        "trigger_price": _f(order.get("trigger_price")),
        "trigger_type": order.get("trigger_type"),
        "side": order.get("side"),
        "state": order.get("state"),
    }


async def _fetch_order_book(client, symbol: str, errors: list[str]) -> dict:
    exchange = getattr(client, "_exchange", None)
    fetch_order_book = getattr(exchange, "fetch_order_book", None)
    if not fetch_order_book:
        return {}
    order_book = await _try(errors, f"order_book {symbol}", fetch_order_book(symbol, limit=5), {})
    bids = order_book.get("bids") or []
    asks = order_book.get("asks") or []
    best_bid = _f(bids[0][0]) if bids else 0.0
    best_ask = _f(asks[0][0]) if asks else 0.0
    bid_size = _f(bids[0][1]) if bids else 0.0
    ask_size = _f(asks[0][1]) if asks else 0.0
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "best_bid_size": bid_size,
        "best_ask_size": ask_size,
        "spread_pct": round(((best_ask - best_bid) / best_ask * 100), 4) if best_bid and best_ask else 0.0,
    }


async def _fetch_open_interest(client, symbol: str, errors: list[str]) -> float:
    exchange = getattr(client, "_exchange", None)
    fetch_open_interest = getattr(exchange, "fetch_open_interest", None)
    if not fetch_open_interest:
        return 0.0
    data = await _try(errors, f"open_interest {symbol}", fetch_open_interest(symbol), {})
    return _f(
        data.get("openInterestAmount"),
        _f(data.get("openInterestValue"), _f(data.get("openInterest"))),
    )


async def _enrich_candidate(client, candidate: dict, errors: list[str]) -> dict:
    symbol = candidate.get("symbol", "")
    row = {
        "symbol": symbol,
        "coin": _symbol_coin(symbol),
        "direction": candidate.get("direction", "watch"),
        "score": candidate.get("score", 0),
        "rsi": candidate.get("rsi", 0),
        "daily_change_pct": candidate.get("daily_change_pct", 0),
        "local_price": candidate.get("price", 0),
        "volume_24h": candidate.get("volume_24h", 0),
        "funding_rate": candidate.get("funding_rate", 0),
        "reasons": list(candidate.get("reasons", []))[:4],
    }

    ticker_fn = getattr(client, "get_ticker", None)
    if ticker_fn:
        ticker = await _try(errors, f"ticker {symbol}", ticker_fn(symbol), {})
        if ticker:
            row["api_price"] = _f(ticker.get("last"), _f(ticker.get("markPrice")))
            row["api_change_pct"] = _f(ticker.get("percentage"), row["daily_change_pct"])
            row["api_quote_volume"] = _f(ticker.get("quoteVolume"), _f(ticker.get("baseVolume")))

    funding_fn = getattr(client, "get_funding_rate", None)
    if funding_fn:
        funding = await _try(errors, f"funding {symbol}", funding_fn(symbol), {})
        if funding:
            row["funding_rate"] = _f(funding.get("rate"), row["funding_rate"])
            row["next_funding_time"] = funding.get("next_funding_time")

    row.update(await _fetch_order_book(client, symbol, errors))
    row["open_interest"] = await _fetch_open_interest(client, symbol, errors)
    return row


async def build_research_snapshot(client, config, candidates: list[dict],
                                  max_candidates: int = 12) -> dict:
    """Collect non-secret account and market data for a manual /scan request."""
    errors: list[str] = []
    provider = getattr(config, "exchange_provider", "mexc") or "unknown"

    balance_fn = getattr(client, "get_futures_balance", None)
    raw_balance = await _try(errors, "futures_balance", balance_fn(), {}) if balance_fn else {}

    positions_fn = getattr(client, "get_positions", None)
    raw_positions = await _try(errors, "positions", positions_fn(), []) if positions_fn else []

    tp_sl_fn = getattr(client, "get_tp_sl_orders", None)
    raw_orders = await _try(errors, "tp_sl_orders", tp_sl_fn(), []) if tp_sl_fn else []

    enriched = []
    for candidate in candidates[:max_candidates]:
        if candidate.get("symbol"):
            enriched.append(await _enrich_candidate(client, candidate, errors))

    return {
        "snapshot_at": _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat(),
        "provider": provider,
        "balance": _extract_balance(raw_balance),
        "positions": [_normalize_position(p) for p in raw_positions[:10]],
        "tp_sl_orders": [_normalize_order(o) for o in raw_orders[:20]],
        "candidates": enriched,
        "errors": errors[:12],
        "positions_available": bool(positions_fn) and not any(e.startswith("positions:") for e in errors),
        "orders_available": bool(tp_sl_fn) and not any(e.startswith("tp_sl_orders:") for e in errors),
    }


def format_research_snapshot(snapshot: dict | None) -> str:
    if not snapshot:
        return "EXCHANGE API SNAPSHOT:\nprovider=unknown\naccount_data=unavailable"

    balance = snapshot.get("balance") or {}
    lines = [
        "EXCHANGE API SNAPSHOT:",
        f"snapshot_at={snapshot.get('snapshot_at', 'unknown')}",
        f"provider={snapshot.get('provider', 'unknown')}",
        (
            "balance="
            f"free_usdt={_f(balance.get('free_usdt')):.2f}, "
            f"total_usdt={_f(balance.get('total_usdt')):.2f}, "
            f"used_usdt={_f(balance.get('used_usdt')):.2f}"
        ),
    ]
    if not balance:
        lines[-1] = "balance=unavailable"

    positions = snapshot.get("positions") or []
    if positions:
        lines.append("OPEN POSITIONS:")
        for p in positions:
            lines.append(
                f"- {_symbol_coin(p.get('symbol'))} {p.get('side')} "
                f"entry={_f(p.get('entry_price')):.6g}, mark={_f(p.get('mark_price')):.6g}, "
                f"pnl=${_f(p.get('unrealized_pnl')):+.2f}/{_f(p.get('percentage')):+.1f}%, "
                f"lev={int(_f(p.get('leverage'), 1) or 1)}x, liq={_f(p.get('liquidation_price')):.6g}"
            )
    else:
        lines.append("OPEN POSITIONS: " + ("none" if snapshot.get("positions_available", False) else "unavailable"))

    orders = snapshot.get("tp_sl_orders") or []
    if orders:
        lines.append("TP/SL ORDERS:")
        for o in orders[:10]:
            lines.append(
                f"- {_symbol_coin(o.get('symbol'))} trigger={_f(o.get('trigger_price')):.6g} "
                f"type={o.get('trigger_type')} side={o.get('side')}"
            )
    else:
        lines.append("TP/SL ORDERS: " + ("none" if snapshot.get("orders_available", False) else "unavailable"))

    candidates = snapshot.get("candidates") or []
    if candidates:
        lines.append("MARKET CANDIDATES FROM EXCHANGE API:")
        for c in candidates:
            reasons = "; ".join(_safe_text(r, 80) for r in (c.get("reasons") or [])[:3])
            lines.append(
                f"- {c.get('coin')} {str(c.get('direction', 'watch')).upper()} "
                f"price={_f(c.get('api_price'), _f(c.get('local_price'))):.6g}, "
                f"24h={_f(c.get('api_change_pct'), _f(c.get('daily_change_pct'))):+.1f}%, "
                f"rsi={_f(c.get('rsi')):.1f}, "
                f"funding={_f(c.get('funding_rate'))*100:+.4f}%, "
                f"bid={_f(c.get('best_bid')):.6g}, ask={_f(c.get('best_ask')):.6g}, "
                f"spread={_f(c.get('spread_pct')):.4f}%, "
                f"oi={_f(c.get('open_interest')):.0f}, score={c.get('score', 0)}, "
                f"reasons={reasons or 'n/a'}"
            )
    else:
        lines.append("MARKET CANDIDATES FROM EXCHANGE API: none")

    errors = snapshot.get("errors") or []
    if errors:
        lines.append("SNAPSHOT WARNINGS:")
        for e in errors[:8]:
            lines.append(f"- {_safe_text(e, 140)}")

    return "\n".join(lines)


async def research_context(client, config, candidates):
    """Bound total latency; unavailable account data must never look like a zero balance."""
    try:
        return await asyncio.wait_for(build_research_snapshot(client, config, candidates, max_candidates=3), 20)
    except Exception as error:
        return {"provider": "mexc", "errors": [f"snapshot: {type(error).__name__}"]}

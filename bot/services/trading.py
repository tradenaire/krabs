"""Unified trading operations — single source of truth for open/close.

Previously ``execute_open`` lived in handlers/trading.py (imported by jobs and
scan), and position closing was reimplemented three times (handlers/trading
``_do_close``, assistant ``nlp_close_callback``, monitor_callbacks). This module
centralizes both. Handlers keep thin wrappers that call these and render UI.
"""
from __future__ import annotations

import asyncio
import logging

from bot import db as db_mod
from bot.event_logger import log_event, snapshot_exchange_state
from bot.services.tpsl import calc_tp_price, calc_sl_price
from bot.services.sizing import max_leverage_by_vol

logger = logging.getLogger(__name__)

_MEXC_MIN_NOTIONAL = 5.0


def _symbol_lock(app, symbol: str):
    """Per-symbol order lock from shared AppState (parallel across symbols)."""
    from bot.infra.state import get_state
    return get_state(app).locks.symbol(symbol)


class MinOrderUpgradeNeeded(Exception):
    def __init__(self, min_margin: float, leverage: int):
        self.min_margin = min_margin
        self.leverage = leverage
        super().__init__(f"min_margin={min_margin:.4f} lev={leverage}")


async def execute_open(client, app, symbol: str, side: str,
                       margin: float, leverage: int | None = None,
                       tp_pct: float = 500, sl_pct: float = 500,
                       interactive: bool = False) -> dict:
    """Open a futures position with TP/SL and register re-entry.

    interactive=True: raises MinOrderUpgradeNeeded instead of silently upgrading margin.
    """
    config = app.bot_data.get("config")
    log_event(
        "decisions", "execute_open_start", symbol=symbol, side=side,
        requested_margin=margin, requested_leverage=leverage,
        tp_pct=tp_pct, sl_pct=sl_pct, interactive=interactive,
    )
    await snapshot_exchange_state(
        client, "before_open", symbol=symbol, requested_side=side,
        requested_margin=margin, requested_leverage=leverage,
    )

    user_set = leverage is not None and leverage > 0

    if not user_set:
        try:
            leverage = await client.get_max_leverage(symbol)
        except Exception:
            leverage = 25

    if not user_set:
        try:
            ticker = await client.get_ticker(symbol)
            vol_24h = float(ticker.get("quoteVolume") or ticker.get("baseVolume") or 0)
            vol_cap = max_leverage_by_vol(vol_24h)
            if leverage > vol_cap:
                logger.info("Leverage capped %s: %d→%d (vol_24h=$%.0f)", symbol, leverage, vol_cap, vol_24h)
                leverage = vol_cap
        except Exception:
            pass

    # BTC trend warning for manual shorts (non-blocking)
    if side in ("sell", "short"):
        try:
            from bot.jobs.main import _get_btc_rsi_4h
            btc_rsi = await _get_btc_rsi_4h(client)
            btc_threshold = float(getattr(config, "btc_rsi_filter", 65.0)) if config else 65.0
            if btc_rsi is not None and btc_rsi > btc_threshold:
                from bot.jobs.main import _notify_all
                await _notify_all(app,
                    f"⚠️ BTC RSI 4h = `{btc_rsi:.0f}` > `{btc_threshold:.0f}` — бычий рынок\n"
                    f"Шорт открывается, но осторожно")
        except Exception:
            pass

    # Enforce minimum order notional AFTER leverage caps (MEXC error 7008).
    _min_cache: dict = app.bot_data.setdefault("_min_order_cache", {})
    _cached_notional = _min_cache.get(symbol, 0)
    if _cached_notional > 0:
        effective_notional = max(_cached_notional, _MEXC_MIN_NOTIONAL)
        _min_margin = effective_notional / max(leverage, 1) * 1.05
    else:
        try:
            _min_margin_api = await client.get_min_order_usdt(symbol, leverage)
            raw_notional = _min_margin_api * leverage if _min_margin_api > 0 else 0.0
        except Exception:
            raw_notional = 0.0
        effective_notional = max(raw_notional, _MEXC_MIN_NOTIONAL)
        _min_cache[symbol] = effective_notional
        _min_margin = effective_notional / max(leverage, 1) * 1.05

    if _min_margin > 0 and margin < _min_margin - 0.0001:
        if interactive:
            raise MinOrderUpgradeNeeded(_min_margin, leverage)
        logger.info("execute_open %s: margin upgraded $%.4f→$%.4f (×%d)", symbol, margin, _min_margin, leverage)
        margin = _min_margin

    # Serialize order placement per symbol so averaging / re-entry / manual opens
    # never collide on the same symbol (different symbols still run in parallel).
    async with _symbol_lock(app, client.futures_symbol(symbol)):
        order = await client.place_futures_order(symbol, side, margin, leverage)
    actual_lev = order.get("leverage", leverage) or leverage
    log_event("decisions", "execute_open_order_result", symbol=symbol, order=order)

    await asyncio.sleep(2)
    pos = await client.get_position(symbol)
    await snapshot_exchange_state(client, "after_open_order", symbol=symbol, order=order)

    tp_price = sl_price = None
    entry = order.get("price", 0)
    liq = 0

    if pos:
        entry = pos["entry_price"]
        liq = pos.get("liquidation_price", 0)
        pos_side = pos["side"]
        tp_price = calc_tp_price(entry, actual_lev, tp_pct, pos_side)
        sl_price = calc_sl_price(entry, actual_lev, sl_pct, pos_side)
        try:
            await snapshot_exchange_state(
                client, "before_tpsl_set", symbol=symbol,
                tp_price=tp_price, sl_price=sl_price,
            )
            await client.cancel_tp_sl_orders(symbol)
            await client.set_tp_sl(symbol, tp_price=tp_price, sl_price=sl_price)
            await snapshot_exchange_state(
                client, "after_tpsl_set", symbol=symbol,
                tp_price=tp_price, sl_price=sl_price,
            )
        except Exception as e:
            log_event(
                "errors", "execute_open_tpsl_failed", symbol=symbol,
                tp_price=tp_price, sl_price=sl_price, error=str(e),
            )
            logger.warning("TP/SL set failed for %s: %s", symbol, e)

    fsym = client.futures_symbol(symbol)
    max_avg_count = int(getattr(config, "max_averaging_count", 100)) if config else 100
    avg_amount = float(getattr(config, "averaging_amount", 0.5)) if config else 0.5
    budget = max_avg_count * avg_amount
    db_mod.upsert_position(
        symbol=fsym, side=side if side in ("long", "short") else ("short" if side == "sell" else "long"),
        entry_price=entry, leverage=actual_lev, margin=margin,
        tp_pct=tp_pct, sl_pct=sl_pct, budget=budget,
    )

    db_mod.log_trade(fsym, "open", amount=margin, note=f"lev={actual_lev}")

    hist_side = "short" if side == "sell" else "long"
    db_mod.open_position_history(
        fsym, hist_side, actual_lev, entry, margin,
        tp_pct=tp_pct, sl_pct=sl_pct,
        avg_threshold=float(getattr(config, "averaging_threshold", -100)) if config else -100,
        avg_amount=float(getattr(config, "averaging_amount", 0)) if config else 0,
        avg_budget=budget,
        avg_max_count=max_avg_count,
        avg_interval=int(getattr(config, "averaging_interval", 0)) if config else 0,
    )

    max_cycles = int(getattr(config, "max_reentry_cycles", 3)) if config else 3
    if max_cycles == 0:
        db_mod.delete_reentry(fsym)
    else:
        db_mod.upsert_reentry(
            symbol=fsym, side=side, margin=margin, leverage=actual_lev,
            tp_pct=tp_pct, sl_pct=sl_pct, max_cycles=max_cycles, cycle_count=0,
        )

    tp_sl_pcts = app.bot_data.setdefault("tp_sl_pcts", {})
    tp_sl_pcts[fsym] = {"tp_pct": tp_pct, "sl_pct": sl_pct}
    log_event(
        "decisions", "execute_open_persisted", symbol=fsym,
        entry_price=entry, leverage=actual_lev, margin=margin,
        tp_price=tp_price, sl_price=sl_price, tp_pct=tp_pct, sl_pct=sl_pct,
    )

    return {
        "symbol": symbol,
        "side": side,
        "margin": margin,
        "leverage": actual_lev,
        "entry_price": entry,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "liquidation_price": liq,
        "order_id": order.get("id"),
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
    }


async def close_position(client, bot_data, symbol: str, *,
                         keep_reentry: bool = False, note: str | None = None) -> dict:
    """Close a position on the exchange and reconcile DB/re-entry state.

    Unifies the three previous implementations. Returns a dict with pnl, margin,
    exit_price and cycles_left (None when re-entry is not kept).
    """
    pos = await client.get_position(symbol)
    pnl = float(pos.get("unrealized_pnl", 0)) if pos else 0.0
    margin = float(pos.get("margin", 0)) if pos else 0.0
    exit_price = float(pos.get("mark_price", 0)) if pos else 0.0

    if keep_reentry:
        re_rec = db_mod.get_reentry(symbol)
        if not re_rec:
            db_rec = db_mod.get_open_position(symbol)
            config = bot_data.get("config")
            max_cycles = int(getattr(config, "max_reentry_cycles", 3)) if config else 3
            if db_rec and max_cycles > 0:
                db_mod.upsert_reentry(
                    symbol=symbol,
                    side="sell" if db_rec.get("side") == "short" else "buy",
                    margin=margin or float(db_rec.get("margin", 0.2)),
                    leverage=int(db_rec.get("leverage", 1)),
                    tp_pct=float(db_rec.get("tp_pct", 500)),
                    sl_pct=float(db_rec.get("sl_pct", 500)),
                    max_cycles=max_cycles,
                )

    state = bot_data.get("app_state")
    if state is not None:
        async with state.locks.symbol(client.futures_symbol(symbol)):
            await client.cancel_tp_sl_orders(symbol)
            await client.close_futures_position(symbol)
    else:
        await client.cancel_tp_sl_orders(symbol)
        await client.close_futures_position(symbol)
    note = note or ("manual_reentry" if keep_reentry else "manual")
    db_mod.close_position(symbol)
    db_mod.log_trade(symbol, "close", amount=margin, pnl=pnl, note=note)
    db_mod.close_position_history(symbol, exit_price, pnl, note)

    if not keep_reentry:
        db_mod.delete_reentry(symbol)
        return {"pnl": pnl, "margin": margin, "exit_price": exit_price, "cycles_left": None}

    re_rec = db_mod.get_reentry(symbol)
    cycles_left = 0
    if re_rec:
        mc = re_rec.get("max_cycles") or 0
        cc = re_rec.get("cycle_count") or 0
        cycles_left = max(0, int(mc) - int(cc))
    return {"pnl": pnl, "margin": margin, "exit_price": exit_price, "cycles_left": cycles_left}


def format_open_result(result: dict, requested_margin: float,
                       funding_line: str = "") -> str:
    """Standard post-open Telegram message (Markdown). Shared by handlers."""
    coin = result["symbol"].split("/")[0]
    actual_margin = result.get("margin", requested_margin)
    tp_pct = result.get("tp_pct", 500)
    sl_pct = result.get("sl_pct", 500)
    lines = [
        f"*{coin}* 🔻×{result['leverage']} `${actual_margin:.2f}`",
        f"▶ Entry: `{result['entry_price']:.6g}`",
    ]
    if actual_margin > requested_margin + 0.001:
        lines.append(f"⚠️ Маржа поднята до мин MEXC: `${requested_margin:.2f}` → `${actual_margin:.2f}`")
    if result.get("liquidation_price"):
        lines.append(f"💀 Liq: `{result['liquidation_price']:.6g}`")
    if result.get("tp_price"):
        lines.append(f"✅ TP: `{result['tp_price']:.6g}` (+{tp_pct:.0f}%)")
    if result.get("sl_price"):
        lines.append(f"🛑 SL: `-{sl_pct:.0f}%` (`{result['sl_price']:.6g}`)")
    if funding_line:
        lines.append(funding_line)
    return "\n".join(lines)

"""Background jobs: averaging, re-entry, TP/SL enforce, live positions monitor."""
import asyncio
import json
import logging
import time
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.event_logger import log_event, set_correlation_id, snapshot_exchange_state

logger = logging.getLogger(__name__)
SCHEDULER = AsyncIOScheduler()

_EXHAUSTED_PATH = Path(__file__).parent.parent.parent / "data" / "avg_exhausted.json"


def _load_exhausted() -> set:
    try:
        return set(json.loads(_EXHAUSTED_PATH.read_text()))
    except Exception:
        return set()


def _save_exhausted(s: set) -> None:
    try:
        _EXHAUSTED_PATH.parent.mkdir(parents=True, exist_ok=True)
        _EXHAUSTED_PATH.write_text(json.dumps(list(s)))
    except Exception as e:
        logger.warning("Failed to persist avg_exhausted: %s", e)

_position_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    # Legacy global lock kept for backward-compat; engines/services use the
    # per-symbol LockManager in AppState instead (see _symbol_lock).
    global _position_lock
    if _position_lock is None:
        _position_lock = asyncio.Lock()
    return _position_lock


def _symbol_lock(app, symbol: str):
    """Per-symbol order lock: parallel across symbols, serial per symbol."""
    from bot.infra.state import get_state
    return get_state(app).locks.symbol(symbol)


from bot.services.tpsl import calc_tp_price as _calc_tp_price
from bot.services.tpsl import calc_sl_price as _calc_sl_price
from bot.services.tpsl import trigger_price_matches as _trigger_price_matches
from bot.services.tpsl import verify_active_sl as _verify_active_sl
from bot.services.tpsl import set_tp_sl_verified as _set_tp_sl_verified


# NOTE: the old live-monitor (positions_monitor_job + _format_live_text +
# _monitor_keyboard) was dead code — never registered in the scheduler — and
# _format_live_text referenced an undefined ``max_count``. Removed during the
# engine refactor. The pinned balance message is handled by PinEngine instead.


# ── Positions cache job ───────────────────────────────────────────

async def positions_cache_job(app):
    """Refreshes shared positions + free balance cache every few seconds."""
    client = app.bot_data.get("exchange")
    if not client:
        return
    try:
        positions = await client.get_positions()
        app.bot_data["_pos_cache"] = positions
        app.bot_data["_pos_cache_ts"] = time.time()
    except Exception as e:
        logger.debug("positions_cache_job: get_positions failed: %s", e)
    try:
        free = await client.get_free_futures_balance()
        app.bot_data["_bal_cache"] = free
    except Exception as e:
        logger.debug("positions_cache_job: get_balance failed: %s", e)


# ── Averaging job ─────────────────────────────────────────────────

async def averaging_job(app):
    """Докупка при PnL ≤ threshold."""
    from bot import db as db_mod

    config = app.bot_data.get("config")
    if not config:
        return
    if not getattr(config, "averaging_enabled", True):
        return

    client = app.bot_data["exchange"]
    threshold = float(getattr(config, "averaging_threshold", -100))
    amount = float(getattr(config, "averaging_amount", 0.50))
    max_count = int(getattr(config, "max_averaging_count", 100))
    cfg_leverage = int(getattr(config, "default_leverage", 0) or 0)

    # Load dynamic averaging rules (sorted by "after" asc)
    import json as _json
    from bot import db as _db_dyn
    _dyn_rules: list[dict] = []
    try:
        _dyn_raw = _db_dyn.get_config("avg_dynamic_rules", "")
        if _dyn_raw:
            _dyn_rules = sorted(_json.loads(_dyn_raw), key=lambda r: r["after"])
    except Exception:
        pass
    profit_lock_trigger = float(getattr(config, "averaging_profit_lock_trigger", 0))
    # (averaging_profit_lock_sl_pct is applied elsewhere; not needed here)

    # Skip averaging during emergency cooldown
    if time.time() < app.bot_data.get("_avg_disabled_until", 0):
        remain = int(app.bot_data["_avg_disabled_until"] - time.time())
        logger.debug("Averaging paused after emergency close, %ds left", remain)
        return

    positions = app.bot_data.get("_pos_cache")
    if positions is None:
        try:
            positions = await client.get_positions()
        except Exception as e:
            logger.error("Averaging: get_positions failed: %s", e)
            return

    if not positions:
        return

    # Guard: track symbols averaged this cycle to skip duplicates
    _avg_ts: dict = app.bot_data.setdefault("_avg_last_ts", {})
    avg_interval = int(getattr(config, "averaging_interval", 10))
    now_ts = time.time()

    try:
        free_balance = await client.get_free_futures_balance()
        if free_balance >= 0:
            app.bot_data["_bal_cache"] = free_balance
    except Exception:
        free_balance = app.bot_data.get("_bal_cache", 0.0)

    # Margin emergency now runs in its own EmergencyEngine (bot/engines/emergency.py).
    # It sets _avg_disabled_until, which is honored by the early-exit check above.

    db_positions = {p["symbol"]: p for p in db_mod.get_open_positions()}
    synth_store = app.bot_data.setdefault("_avg_synth", {})
    notified_exhausted: set = app.bot_data.setdefault("_avg_notified_exhausted", set())

    # Contracts tracking: compare our expected count vs exchange
    _exp_contracts: dict = app.bot_data.setdefault("_expected_contracts", {})
    _contracts_warned: set = app.bot_data.setdefault("_contracts_warned", set())
    _profit_lock_step: dict = app.bot_data.setdefault("_profit_lock_step", {})

    # Lazy-load profit lock disabled set from file
    if "_profit_lock_disabled" not in app.bot_data:
        _plock_path = Path(__file__).parent.parent.parent / "data" / "profit_lock_disabled.json"
        try:
            app.bot_data["_profit_lock_disabled"] = set(
                json.loads(_plock_path.read_text()))
        except Exception:
            app.bot_data["_profit_lock_disabled"] = set()
    _profit_lock_disabled: set = app.bot_data["_profit_lock_disabled"]
    _age_12h_notified: set = app.bot_data.setdefault("_age_12h_notified", set())

    # Cleanup stale symbols (position closed on exchange)
    current_symbols = {p["symbol"] for p in positions}
    for sym in list(_exp_contracts.keys()):
        if sym not in current_symbols:
            del _exp_contracts[sym]
            _contracts_warned.discard(sym)
    _exhausted_before = set(notified_exhausted)
    for sym in list(notified_exhausted):
        if sym not in current_symbols:
            notified_exhausted.discard(sym)
    if notified_exhausted != _exhausted_before:
        _save_exhausted(notified_exhausted)
    for sym in list(_age_12h_notified):
        if sym not in current_symbols:
            _age_12h_notified.discard(sym)
    for sym in list(_profit_lock_step.keys()):
        if sym not in current_symbols:
            del _profit_lock_step[sym]

    seen_this_run: set[str] = set()
    # Positions managed by the ladder exit are off-limits to averaging.
    ladder_syms = db_mod.get_ladder_symbols()

    for pos in positions:
        symbol = pos["symbol"]
        pnl_pct = float(pos.get("percentage", 0))

        # Skip if already processed this symbol in this run (duplicate position entries)
        if symbol in seen_this_run:
            logger.warning("Averaging: duplicate symbol %s in positions list, skipping", symbol)
            continue
        seen_this_run.add(symbol)

        if symbol in ladder_syms:
            continue  # ladder exit manages this position; no averaging

        # ── Contracts sanity check ──────────────────────────────────
        exchange_contracts = int(round(float(pos.get("contracts", 0))))
        if symbol not in _exp_contracts:
            # First time we see this symbol — anchor to exchange value
            _exp_contracts[symbol] = exchange_contracts
        else:
            expected = _exp_contracts[symbol]
            if expected > 0 and exchange_contracts < expected:
                if symbol not in _contracts_warned:
                    _contracts_warned.add(symbol)
                    coin = symbol.split("/")[0]
                    logger.warning("Contracts mismatch %s: expected=%d exchange=%d — possible partial close",
                                   symbol, expected, exchange_contracts)
                    await _notify_all(app,
                        f"⚠️ *Несоответствие контрактов* `{coin}`\n"
                        f"Ожидалось: `{expected}` | Биржа: `{exchange_contracts}`\n"
                        f"Позиция могла быть частично закрыта. Проверь `/positions`")
            elif exchange_contracts >= expected:
                _contracts_warned.discard(symbol)  # resolved
                if exchange_contracts > expected:
                    # Position grew externally — re-anchor
                    logger.info("Contracts grew externally %s: expected=%d exchange=%d — re-anchoring",
                                symbol, expected, exchange_contracts)
                    _exp_contracts[symbol] = exchange_contracts

        # ── 12h position age notification ────────────────────────────
        if symbol not in _age_12h_notified:
            db_rec_age = db_positions.get(symbol)
            if db_rec_age and db_rec_age.get("created_at"):
                try:
                    import datetime as _dt
                    opened_ts = _dt.datetime.fromisoformat(db_rec_age["created_at"]).timestamp()
                    if time.time() - opened_ts >= 12 * 3600:
                        _age_12h_notified.add(symbol)
                        coin = symbol.split("/")[0]
                        hold_h = (time.time() - opened_ts) / 3600
                        await _notify_all(app,
                            f"⏰ *{coin}* открыта уже `{hold_h:.0f}ч` — проверь позицию\n"
                            f"PnL: `{pnl_pct:+.1f}%`")
                except Exception:
                    pass

        # ── Stepped profit lock: SL ratchets up every 50% of PnL ────
        # +100% PnL → SL at +50%, +150% → SL at +100%, +200% → SL at +150%, ...
        if profit_lock_trigger > 0 and pnl_pct >= 100 and symbol not in _profit_lock_disabled:
            _PL_STEP = 50
            new_pl_step = (int(pnl_pct) // _PL_STEP) * _PL_STEP
            current_pl_step = _profit_lock_step.get(symbol, 0)
            if new_pl_step > current_pl_step:
                lock_sl_pct = new_pl_step - _PL_STEP  # e.g., step=100 → lock at +50%
                _pl_entry = float(pos.get("entry_price", 0) or 0)
                _pl_side = pos.get("side", "short")
                _pl_lev = cfg_leverage or int(pos.get("leverage") or 1)
                if _pl_entry > 0:
                    _tp_sl_pcts_pl = app.bot_data.get("tp_sl_pcts", {})
                    _stored_pl = _tp_sl_pcts_pl.get(symbol, {})
                    _tp_pct_val = _stored_pl.get("tp_pct") or float(getattr(config, "tp_pct", 500))
                    _new_tp = _calc_tp_price(_pl_entry, _pl_lev, _tp_pct_val, _pl_side)
                    _new_sl = _calc_tp_price(_pl_entry, _pl_lev, lock_sl_pct, _pl_side)
                    _profit_lock_step[symbol] = new_pl_step  # guard before await
                    try:
                        await _set_tp_sl_verified(client, symbol, _pl_side, _new_tp, _new_sl, pos)
                        app.bot_data.setdefault("_was_profit_locked", set()).add(symbol)
                        _pl_coin = symbol.split("/")[0]
                        await _notify_all(app,
                            f"🔒 *{_pl_coin}* профит-лок +{new_pl_step}%\n"
                            f"PnL `{pnl_pct:+.1f}%` → SL в `+{lock_sl_pct:.0f}%` PnL\n"
                            f"Триггер: `{_new_sl:.6g}`")
                    except Exception as _pl_e:
                        _profit_lock_step[symbol] = current_pl_step  # rollback
                        app.bot_data.setdefault("_was_profit_locked", set()).discard(symbol)
                        logger.warning("Profit lock SL %s: %s", symbol, _pl_e)

        # Skip if position-limit or count-exhausted (notified_exhausted acts as permanent block)
        if symbol in notified_exhausted:
            continue

        # Auto-register external positions before any threshold check
        db_rec = db_positions.get(symbol)
        if db_rec is None:
            _tp = float(getattr(config, "tp_pct", 500))
            _sl = float(getattr(config, "sl_pct", 500))
            _cur_margin = float(pos.get("margin", amount) or amount)
            _est_count = max(0, round(_cur_margin / amount) - 1) if amount > 0 else 0
            db_mod.upsert_position(
                symbol, pos.get("side", "short"),
                float(pos.get("entry_price", 0) or 0),
                cfg_leverage or int(pos.get("leverage", 1) or 1),
                _cur_margin,
                tp_pct=_tp, sl_pct=_sl,
                budget=amount * max_count,
                total_invested=_cur_margin,
                avg_count=_est_count,
            )
            db_rec = db_mod.get_open_position(symbol)
            if db_rec:
                db_positions[symbol] = db_rec
                app.bot_data.setdefault("tp_sl_pcts", {}).setdefault(
                    symbol, {"tp_pct": _tp, "sl_pct": _sl}
                )
                logger.info("Auto-registered position %s in DB (id=%d)", symbol, db_rec["id"])
            else:
                if symbol not in synth_store:
                    synth_store[symbol] = {
                        "id": None, "symbol": symbol,
                        "total_invested": float(pos.get("margin", 0) or amount),
                        "averaging_count": 0,
                        "tp_pct": _tp, "sl_pct": _sl,
                    }
                db_rec = synth_store[symbol]

        total_invested = float(db_rec.get("total_invested") or 0)
        avg_count = int(db_rec.get("averaging_count") or 0)

        # Skip if averaged too recently (prevents double-order from retry/race)
        last_avg = _avg_ts.get(symbol, 0)
        if now_ts - last_avg < 5:
            continue

        # Determine effective threshold and amount — dynamic rules override globals
        eff_threshold = threshold
        eff_amount = amount
        if _dyn_rules:
            for _rule in _dyn_rules:
                if avg_count >= _rule["after"]:
                    eff_threshold = float(_rule["pnl"])
                    eff_amount = float(_rule["amount"])
        if pnl_pct > eff_threshold:
            continue

        # Count check
        if avg_count >= max_count:
            if symbol not in notified_exhausted:
                notified_exhausted.add(symbol)
                _save_exhausted(notified_exhausted)
                coin = symbol.split("/")[0]
                await _notify_all(app,
                    f"🚫 *Докупки закончились* `{coin}`\n"
                    f"Использовано `{avg_count}/{max_count}` докупок\n"
                    f"Позиция закроется по TP, SL или вручную `/close {coin}`")
            continue

        avg_side = "buy" if pos.get("side") == "long" else "sell"
        avg_lev = cfg_leverage or int(pos.get("leverage") or 1)
        avg_mm = pos.get("margin_mode")
        old_entry = float(pos.get("entry_price", 0) or 0)
        old_contracts = int(round(float(pos.get("contracts", 0) or 0)))
        old_pnl_usd = float(pos.get("unrealized_pnl", 0) or 0)

        # Check minimum contract cost — use cached notional first, then live API
        _min_order_cache: dict = app.bot_data.setdefault("_min_order_cache", {})
        cached_min_notional = _min_order_cache.get(symbol, 0)
        if cached_min_notional > 0 and eff_amount * max(avg_lev, 1) <= cached_min_notional:
            actual_amount = cached_min_notional / max(avg_lev, 1) * 1.05
            logger.info("Avg %s: upgrading amount $%.2f -> $%.2f (cached min notional $%.0f)",
                        symbol, eff_amount, actual_amount, cached_min_notional)
        else:
            try:
                min_cost = await client.get_min_order_usdt(symbol, avg_lev)
            except Exception:
                min_cost = 0.0
            # Add 5% buffer so notional is strictly > MEXC minimum (ceil may land exactly on minimum)
            actual_amount = max(eff_amount, min_cost * 1.05) if min_cost > 0 else eff_amount
            if min_cost > 0 and min_cost * 1.05 > eff_amount:
                logger.info("Avg %s: min contract $%.2f (+5%%) > amount $%.2f, using $%.2f",
                            symbol, min_cost, eff_amount, actual_amount)

        if free_balance < actual_amount - 0.001:
            coin = symbol.split("/")[0]
            _bal_warn_ts: dict = app.bot_data.setdefault("_avg_bal_warn_ts", {})
            if time.time() - _bal_warn_ts.get(symbol, 0) > 300:
                _bal_warn_ts[symbol] = time.time()
                await _notify_all(app,
                    f"⚠️ *Докупка пропущена* `{coin}`\n"
                    f"Баланс `${free_balance:.2f}` < нужно `${actual_amount:.2f}`")
            continue

        # Mark symbol as averaged before placing — prevents retry/race double-orders
        _avg_ts[symbol] = time.time()

        order_result: dict | None = None
        async with _symbol_lock(app, symbol):
            try:
                order_result = await client.place_futures_order(symbol, avg_side, actual_amount, avg_lev,
                                                                margin_mode=avg_mm)
            except Exception as e:
                err_msg = str(e).lower()
                raw_err = str(e)
                # Reset timestamp so next run can retry (we didn't place an order)
                _avg_ts.pop(symbol, None)
                # Position limit hit — mute this symbol until position closes
                _POS_LIMIT_KEYWORDS = ("exceed", "position size", "max position",
                                       "position limit", "risk limit", "too large")
                _MIN_ORDER_KEYWORDS = ("minimum order amount", "min order", "7008", "less than the minimum")
                if any(kw in err_msg for kw in _POS_LIMIT_KEYWORDS):
                    logger.error("Averaging order FAILED for %s: %s", symbol, e)
                    if symbol not in notified_exhausted:
                        notified_exhausted.add(symbol)
                        _save_exhausted(notified_exhausted)
                        coin = symbol.split("/")[0]
                        await _notify_all(app,
                            f"🚫 *Докупки закончились* `{coin}`\n"
                            f"Биржа отклонила: лимит позиции достигнут\n"
                            f"Позиция закроется по TP, SL или вручную `/close {coin}`")
                elif any(kw in err_msg for kw in _MIN_ORDER_KEYWORDS):
                    # Extract minimum from error response if possible
                    import re as _re
                    m = _re.search(r'"value"\s*:\s*(\d+(?:\.\d+)?)', raw_err)
                    min_usdt_notional = float(m.group(1)) if m else 5.0
                    # Update cache so next cycle auto-upgrades amount instead of hitting 7008 again
                    _min_order_cache: dict = app.bot_data.setdefault("_min_order_cache", {})
                    _min_order_cache[symbol] = min_usdt_notional
                    try:
                        from bot import db as _db_7008
                        _db_7008.set_min_order_notional(symbol, min_usdt_notional)
                    except Exception:
                        pass
                    min_margin = min_usdt_notional / max(avg_lev, 1) * 1.05
                    upgraded_amount = min_margin
                    coin = symbol.split("/")[0]
                    logger.info("Avg %s: 7008 — cached min notional $%.0f, next cycle uses $%.2f",
                                symbol, min_usdt_notional, upgraded_amount)
                else:
                    logger.error("Averaging order FAILED for %s: %s", symbol, e)
                continue

        new_total = total_invested + actual_amount
        new_count = avg_count + 1
        free_balance -= actual_amount

        db_rec["total_invested"] = new_total
        db_rec["averaging_count"] = new_count
        if db_rec.get("id"):
            db_mod.update_averaging(db_rec["id"], new_total, new_count)

        # Update expected contracts from actual order result
        if order_result and order_result.get("amount"):
            ordered_contracts = int(round(float(order_result["amount"])))
            _exp_contracts[symbol] = _exp_contracts.get(symbol, exchange_contracts) + ordered_contracts
            logger.info("Expected contracts %s: now %d (+%d from avg)",
                        symbol, _exp_contracts[symbol], ordered_contracts)

        # Log to stats
        db_mod.log_trade(symbol, "avg", amount=actual_amount, note=f"#{new_count}")
        db_mod.update_position_history_avg(symbol, new_total, new_count)

        # Wait for MEXC to update holdAvgPrice
        await asyncio.sleep(1.5)
        pos_after = None
        new_entry = old_entry
        for _ in range(2):
            try:
                pos_after = await client.get_position(symbol)
                if pos_after:
                    fe = float(pos_after.get("entry_price", 0) or 0)
                    if fe and abs(fe - old_entry) > 1e-12:
                        new_entry = fe
                        break
                    new_entry = fe or old_entry
            except Exception:
                pass
            await asyncio.sleep(1.0)

        # Re-apply TP/SL with updated avg entry
        tp_sl_text = ""
        tp_sl_pcts = app.bot_data.get("tp_sl_pcts", {})
        stored = tp_sl_pcts.get(symbol)
        if stored and pos_after and int(round(pos_after.get("contracts", 0))) > 0:
            p_side = pos_after.get("side", "short")
            expected_side = "short" if avg_side == "sell" else "long"
            if p_side != expected_side:
                logger.error("TP/SL skip %s: pos_after.side=%s but expected %s — skipping to avoid wrong SL",
                             symbol, p_side, expected_side)
            else:
                try:
                    p_lev = pos_after.get("leverage", 1)
                    new_tp = _calc_tp_price(new_entry, p_lev, stored["tp_pct"], p_side) \
                        if stored.get("tp_pct") else None
                    new_sl = _calc_sl_price(new_entry, p_lev, stored["sl_pct"], p_side) \
                        if stored.get("sl_pct") else None
                    # Sanity: for short, SL must be above entry, TP below
                    if new_sl and p_side == "short" and new_sl <= new_entry:
                        logger.error("TP/SL skip %s: computed SL %.6g ≤ entry %.6g for short — skipping",
                                     symbol, new_sl, new_entry)
                        new_sl = None
                    if new_tp and p_side == "short" and new_tp >= new_entry:
                        logger.error("TP/SL skip %s: computed TP %.6g ≥ entry %.6g for short — skipping",
                                     symbol, new_tp, new_entry)
                        new_tp = None
                    if new_tp or new_sl:
                        await client.set_tp_sl(symbol, tp_price=new_tp, sl_price=new_sl,
                                               pos_data=pos_after)
                        parts = []
                        if new_tp:
                            parts.append(f"TP: `{new_tp:.6g}`")
                        if new_sl:
                            parts.append(f"SL: `{new_sl:.6g}`")
                        tp_sl_text = "\n🔄 " + ", ".join(parts) + f" (avg: `{new_entry:.6g}`)"
                except Exception as e:
                    logger.warning("TP/SL recalc for %s: %s", symbol, e)

        # Notify
        coin = symbol.split("/")[0]
        mark = pos.get("mark_price", 0)
        liq = pos.get("liquidation_price", 0)
        liq_warn = ""
        if liq > 0 and mark > 0:
            dist = abs(mark - liq) / mark * 100
            if dist < 10:
                liq_warn = f"\n{'💀' if dist < 3 else '⚠️'} Liq `{liq:.6g}` ({dist:.1f}%)"
        shift = ""
        if new_entry and old_entry and abs(new_entry - old_entry) > 1e-12:
            sp = (new_entry - old_entry) / old_entry * 100
            shift = f"\nAvg entry: `{old_entry:.6g}` → `{new_entry:.6g}` ({sp:+.2f}%)"

        new_contracts = int(round(float(pos_after.get("contracts", 0) or 0))) if pos_after else old_contracts
        new_pnl_usd = float(pos_after.get("unrealized_pnl", 0) or 0) if pos_after else 0.0
        new_pnl_pct = float(pos_after.get("percentage", 0) or 0) if pos_after else 0.0

        dyn_line = (f"\n📊 Динамика: порог `{eff_threshold:.0f}%`, сумма `${eff_amount:.2f}` (ступень после {avg_count} докупок)"
                    if _dyn_rules and (eff_threshold != threshold or eff_amount != amount) else "")
        msg = (
            f"*Докупка #{new_count}/{max_count}* `{coin}`\n"
            f"Позиция: `{old_contracts}` → `{new_contracts}` контр. | `${total_invested:.2f}` → `${new_total:.2f}`\n"
            f"PnL: `{pnl_pct:+.1f}%` / `${old_pnl_usd:+.2f}` → `{new_pnl_pct:+.1f}%` / `${new_pnl_usd:+.2f}`\n"
            f"+`${actual_amount:.2f}` (×{avg_lev})"
            f"{dyn_line}{shift}{liq_warn}{tp_sl_text}"
        )
        await _notify_all(app, msg)


# ── Re-entry job ──────────────────────────────────────────────────

async def _resolve_close_reason(client, symbol: str, pos_side_str: str,
                                opened_at_ms: int | None,
                                entry_price: float) -> tuple[bool | None, bool, float | None]:
    """Returns (closed_by_tp, profitable_sl, close_price).
    closed_by_tp: True=TP, False=SL, None=unknown.
    profitable_sl: True if SL triggered but at a price better than entry (profit-lock SL).
    close_price: actual trigger/close price, or None if unknown."""
    is_tp, trigger_price = await client.was_closed_by_tp(symbol, pos_side_str, opened_at_ms)

    profitable_sl = False
    if is_tp is False and trigger_price and entry_price > 0:
        profitable_sl = (
            (trigger_price < entry_price) if pos_side_str == "short"
            else (trigger_price > entry_price)
        )

    if is_tp is None and entry_price > 0:
        try:
            ticker = await client._exchange.fetch_ticker(symbol)
            mark = float(ticker.get("last", 0) or 0)
            if mark > 0:
                is_tp = (mark < entry_price) if pos_side_str == "short" else (mark > entry_price)
                if trigger_price is None:
                    trigger_price = mark
        except Exception as e:
            logger.debug("_resolve_close_reason price fallback %s: %s", symbol, e)

    return is_tp, profitable_sl, trigger_price


def _fmt_close_pnl(entry: float, close_price: float | None,
                   side: str, leverage: int, margin: float) -> str:
    """Returns '+120.5% / +$0.24' string, or '' if data missing."""
    if not close_price or not entry or entry == 0:
        return ""
    if side == "short":
        pct = (entry - close_price) / entry * leverage * 100
    else:
        pct = (close_price - entry) / entry * leverage * 100
    usd = pct / 100 * margin
    return f"`{pct:+.1f}%` / `{usd:+.2f}$`"


async def reentry_job(app):
    """После TP — переоткрыть позицию (max_cycles раз)."""
    from bot import db as db_mod
    client = app.bot_data["exchange"]

    reentry_list = db_mod.get_all_reentry()
    if not reentry_list:
        return

    try:
        open_positions = await client.get_positions()
        open_symbols = {p["symbol"] for p in open_positions}
    except Exception as e:
        logger.error("Re-entry: get_positions failed: %s", e)
        return

    # Fetch available futures balance once per run
    futures_avail = 0.0
    try:
        bal = await client.get_futures_balance()
        raw = bal.get("_raw", {})
        futures_avail = float(raw.get("availableOpen", raw.get("availableBalance", 0)) or 0)
        free = float(bal.get("free", {}).get("USDT", 0) or 0)
        futures_avail = max(futures_avail, free)
    except Exception:
        pass

    for re_cfg in reentry_list:
        symbol = re_cfg["symbol"]
        if symbol in open_symbols:
            app.bot_data.pop(f"_reentry_sl_ts_{symbol}", None)
            continue

        cycle_count = int(re_cfg.get("cycle_count") or 0)
        _mc = re_cfg.get("max_cycles")
        max_cycles = int(_mc) if _mc is not None else 3

        side = re_cfg["side"]
        coin = symbol.split("/")[0]
        pos_side_str = "short" if side == "sell" else "long"

        # Get open timestamp and entry price for close reason resolution
        ph = db_mod.get_last_position_history(symbol)
        opened_at_ms = None
        entry_price = float(ph["entry_price"]) if ph and ph.get("entry_price") else 0.0
        ph_total_invested = float(ph.get("total_invested") or 0) if ph else 0.0
        ph_leverage = int(ph.get("leverage") or 0) if ph else 0
        if ph and ph.get("opened_at"):
            try:
                import datetime as _dt
                opened_at_ms = int(
                    _dt.datetime.fromisoformat(ph["opened_at"]).timestamp() * 1000
                )
            except Exception:
                pass

        if max_cycles == 0:
            closed_by_tp, profitable_sl, close_price = await _resolve_close_reason(client, symbol, pos_side_str, opened_at_ms, entry_price)
            if closed_by_tp is None:
                continue
            close_note = "tp" if (closed_by_tp or profitable_sl) else "sl"
            pnl_text, pnl_usdt = _close_pnl(close_price)
            db_mod.close_position(symbol)
            db_mod.log_trade(symbol, "close", note=close_note, pnl=pnl_usdt)
            db_mod.close_position_history(symbol, exit_price=close_price or 0, pnl=pnl_usdt, close_reason=close_note)
            if not closed_by_tp and not profitable_sl:
                app.bot_data.setdefault("_sl_cooldown", {})[symbol] = time.time()
            icon = "✅" if (closed_by_tp or profitable_sl) else "🛑"
            label = "по тейку" if closed_by_tp else ("по профит-локк SL" if profitable_sl else "по стопу")
            await _notify_all(app,
                f"{icon} *{coin}* {label} (перезаход отключён){pnl_text}")
            db_mod.delete_reentry(symbol)
            continue

        if cycle_count >= max_cycles:
            logger.info("Re-entry: %s exhausted (%d/%d cycles)", symbol, cycle_count, max_cycles)
            closed_by_tp, profitable_sl, close_price = await _resolve_close_reason(client, symbol, pos_side_str, opened_at_ms, entry_price)
            if closed_by_tp is None:
                continue
            close_note = "tp" if (closed_by_tp or profitable_sl) else "sl"
            pnl_text, pnl_usdt = _close_pnl(close_price)
            db_mod.close_position(symbol)
            db_mod.log_trade(symbol, "close", note=close_note, pnl=pnl_usdt)
            db_mod.close_position_history(symbol, exit_price=close_price or 0, pnl=pnl_usdt, close_reason=close_note)
            icon = "✅" if (closed_by_tp or profitable_sl) else "🛑"
            label = "по тейку" if closed_by_tp else ("по профит-локк SL" if profitable_sl else "по стопу")
            await _notify_all(app,
                f"{icon} *{coin}* {label} — циклы исчерпаны ({cycle_count}/{max_cycles}){pnl_text}")
            db_mod.delete_reentry(symbol)
            continue

        # 30s cooldown between cycles
        last_check_key = f"_reentry_ts_{symbol}"
        last_ts = app.bot_data.get(last_check_key, 0)
        if time.time() - last_ts < 30:
            continue
        app.bot_data[last_check_key] = time.time()

        config_obj = app.bot_data.get("config")
        margin = (float(getattr(config_obj, "default_trade_usdt", 0) or 0)
                  or float(re_cfg.get("margin") or 1.0))
        leverage = (int(getattr(config_obj, "default_leverage", 0) or 0)
                    or int(re_cfg.get("leverage") or 0) or None)
        tp_pct = (float(getattr(config_obj, "tp_pct", 0) or 0)
                  or float(re_cfg.get("tp_pct") or 500))
        sl_pct = (float(getattr(config_obj, "sl_pct", 0) or 0)
                  or float(re_cfg.get("sl_pct") or 500))

        logger.info("Re-entry #%d %s %s $%.2f", cycle_count + 1, symbol, side, margin)

        pnl_margin = ph_total_invested or float(re_cfg.get("margin") or 1.0)
        pnl_lev = ph_leverage or int(re_cfg.get("leverage") or 0) or 1

        def _close_pnl(exit_price: float | None) -> tuple[str, float]:
            if not exit_price or entry_price <= 0:
                return "", 0.0
            pnl_s = _fmt_close_pnl(entry_price, exit_price, pos_side_str, pnl_lev, pnl_margin)
            if side == "short":
                pct = (entry_price - exit_price) / entry_price * pnl_lev * 100
            else:
                pct = (exit_price - entry_price) / entry_price * pnl_lev * 100
            pnl_usdt = pct / 100 * pnl_margin
            return (f" · PnL {pnl_s}" if pnl_s else "", pnl_usdt)

        # Determine close reason: TP or profitable-SL → re-enter, loss-SL → skip
        closed_by_tp, profitable_sl, close_price = await _resolve_close_reason(client, symbol, pos_side_str, opened_at_ms, entry_price)

        if closed_by_tp is None:
            logger.info("Re-entry: %s close reason unknown, retrying next cycle", symbol)
            continue

        close_note = "tp" if (closed_by_tp or profitable_sl) else "sl"
        still_open = db_mod.get_open_position(symbol) is not None
        if still_open:
            db_mod.close_position(symbol)
            db_mod.log_trade(symbol, "close", note=close_note)
            db_mod.close_position_history(symbol, exit_price=0, pnl=0, close_reason=close_note)

        # Fallback: if price bounced above entry before reentry_job ran,
        # _resolve_close_reason misses the profit-lock. Check persistent flag.
        if not closed_by_tp and not profitable_sl:
            _was_pl: set = app.bot_data.setdefault("_was_profit_locked", set())
            if symbol in _was_pl:
                profitable_sl = True
                _was_pl.discard(symbol)

        if profitable_sl and not closed_by_tp:
            # Profit-lock SL fired because price bounced. Wait 1 min, then re-enter.
            pl_cd_key = f"_reentry_pl_ts_{symbol}"
            pl_ts = app.bot_data.get(pl_cd_key, 0)

            if pl_ts == 0:
                app.bot_data[pl_cd_key] = time.time()
                await _notify_all(app,
                    f"🔒 *{coin}* закрыта в прибыль (профит-локк SL)\n"
                    f"⏳ Пауза 1м, отмена ордеров, перезаход "
                    f"#{cycle_count + 1}/{max_cycles}")
                continue

            if time.time() - pl_ts < 60:
                continue  # still waiting

            # Cooldown elapsed — cancel stale plan orders then proceed to re-entry
            app.bot_data.pop(pl_cd_key, None)
            await client.cancel_plan_orders(symbol)

        if not closed_by_tp and not profitable_sl:
            config_obj = app.bot_data.get("config")
            reentry_on_sl = bool(getattr(config_obj, "reentry_on_sl", False))

            if not reentry_on_sl:
                # Classic behaviour: block re-entry after SL
                app.bot_data.setdefault("_sl_cooldown", {})[symbol] = time.time()
                await _notify_all(app,
                    f"🛑 *{coin}* закрыта по стопу — перезаход пропущен\n"
                    f"⏳ Кулдаун авто-скана на 2ч")
                db_mod.delete_reentry(symbol)
                continue

            # Tight-stop strategy: wait cooldown, then re-enter at (hopefully) better price
            cooldown_min = int(getattr(config_obj, "reentry_sl_cooldown_min", 10))
            sl_cd_key = f"_reentry_sl_ts_{symbol}"
            sl_ts = app.bot_data.get(sl_cd_key, 0)

            if sl_ts == 0:
                app.bot_data[sl_cd_key] = time.time()
                app.bot_data.setdefault("_sl_cooldown", {})[symbol] = time.time()
                await _notify_all(app,
                    f"🛑 *{coin}* закрыта по стопу\n"
                    f"⏳ Пауза {cooldown_min}м, затем перезаход "
                    f"#{cycle_count + 1}/{max_cycles}")
                continue

            if time.time() - sl_ts < cooldown_min * 60:
                continue  # still waiting

            # Cooldown elapsed — proceed to re-entry below
            app.bot_data.pop(sl_cd_key, None)

        # Cancel re-entry if no free futures balance — don't retry
        if futures_avail < margin * 0.1:
            logger.info("Re-entry %s cancelled: no free balance (avail=%.4f)", symbol, futures_avail)
            await _notify_all(app, f"⚠️ *{coin}* перезаход отменён — недостаточно средств")
            db_mod.delete_reentry(symbol)
            continue

        try:
            from bot.handlers.trading import execute_open
            result = await execute_open(client, app, symbol, side, margin, leverage,
                                        tp_pct=tp_pct, sl_pct=sl_pct)
            new_cycle = db_mod.increment_reentry_cycle(symbol)
            pnl_text, pnl_usdt = _close_pnl(close_price)
            db_mod.log_trade(symbol, "reentry", amount=margin, note=f"cycle {new_cycle}", pnl=pnl_usdt)
            # Clear exhausted flag so new cycle gets fresh averaging tracking
            notified_exhausted: set = app.bot_data.setdefault("_avg_notified_exhausted", set())
            notified_exhausted.discard(symbol)
            _save_exhausted(notified_exhausted)
            close_label = "по TP" if closed_by_tp else "в прибыль (профит-локк SL)"
            msg = (
                f"✅ *{coin}* закрыта {close_label} → перезаход #{new_cycle}/{max_cycles}"
                f"{pnl_text}\n"
                f"Entry: `{result['entry_price']:.6g}` | ×{result['leverage']}\n"
                f"TP: `{result.get('tp_price', 0):.6g}` | SL: `{result.get('sl_price', 0):.6g}`"
            )
            await _notify_all(app, msg)
        except Exception as e:
            logger.error("Re-entry failed for %s: %s", symbol, e)


# ── Balance alert job ─────────────────────────────────────────────

async def balance_alert_job(app):
    """Орёт когда свободный баланс падает ниже 20% от общего."""
    client = app.bot_data.get("exchange")
    if not client:
        return
    try:
        bal = await client.get_futures_balance()
        raw = bal.get("_raw", {})
        total = float(bal.get("total", {}).get("USDT", 0) or 0)
        avail = float(raw.get("availableOpen", raw.get("availableBalance", 0)) or 0)
        free = float(bal.get("free", {}).get("USDT", 0) or 0)
        free = max(avail, free)

        if total <= 0:
            return

        pct = free / total * 100
        was_alerted = app.bot_data.get("_bal_alert_sent", False)

        if pct <= 20.0 and not was_alerted:
            await _notify_all(app,
                f"🚨 *Мало свободных средств!*\n"
                f"Свободно: `${free:.2f}` — это `{pct:.1f}%` от депо `${total:.2f}`\n"
                f"Осталось менее 20% — пора пополнить или закрыть позиции."
            )
            app.bot_data["_bal_alert_sent"] = True
        elif pct > 20.0 and was_alerted:
            app.bot_data["_bal_alert_sent"] = False  # сбросить при восстановлении
    except Exception as e:
        logger.debug("balance_alert_job: %s", e)


# ── TP/SL enforce job ─────────────────────────────────────────────

async def tpsl_enforce_job(app):
    """Sync closed positions and clean orphaned plan orders without restoring TP/SL."""
    from bot import db as db_mod
    client = app.bot_data["exchange"]
    tp_sl_pcts = app.bot_data.get("tp_sl_pcts", {})
    set_correlation_id("job-tpsl-enforce")

    try:
        positions = await client.get_positions()
    except Exception as e:
        log_event("errors", "tpsl_enforce_get_positions_failed", error=str(e))
        logger.error("TP/SL enforce: get_positions: %s", e)
        return
    await snapshot_exchange_state(client, "tpsl_enforce_run")

    sem = asyncio.Semaphore(5)  # max 5 concurrent MEXC requests

    async def _check_pos(pos):
        symbol = pos["symbol"]
        stored = tp_sl_pcts.get(symbol)
        if not stored:
            db_rec = db_mod.get_open_position(symbol)
            if not db_rec:
                # Auto-register externally opened position
                config = app.bot_data.get("config")
                _tp = float(getattr(config, "tp_pct", 500)) if config else 500.0
                _sl = float(getattr(config, "sl_pct", 500)) if config else 500.0
                _amount = float(getattr(config, "averaging_amount", 0.25)) if config else 0.25
                _max_count = int(getattr(config, "max_averaging_count", 200)) if config else 200
                _cur_margin = float(pos.get("margin", _amount) or _amount)
                _est_count = max(0, round(_cur_margin / _amount) - 1) if _amount > 0 else 0
                _cfg_lev = int(getattr(config, "default_leverage", 0) or 0) if config else 0
                db_mod.upsert_position(
                    symbol, pos.get("side", "short"),
                    float(pos.get("entry_price", 0) or 0),
                    _cfg_lev or int(pos.get("leverage", 1) or 1),
                    _cur_margin,
                    tp_pct=_tp, sl_pct=_sl,
                    budget=_amount * _max_count,
                    total_invested=_cur_margin,
                    avg_count=_est_count,
                )
                db_rec = db_mod.get_open_position(symbol)
                if db_rec:
                    tp_sl_pcts[symbol] = {"tp_pct": _tp, "sl_pct": _sl}
                    app.bot_data.setdefault("tp_sl_pcts", {})[symbol] = {"tp_pct": _tp, "sl_pct": _sl}
                    log_event(
                        "decisions", "tpsl_enforce_auto_registered_position",
                        symbol=symbol, tp_pct=_tp, sl_pct=_sl,
                        margin=_cur_margin, avg_count=_est_count,
                    )
                    logger.info("tpsl_enforce: auto-registered position %s in DB (id=%d)", symbol, db_rec["id"])
                else:
                    return
            stored = {"tp_pct": db_rec.get("tp_pct", 500), "sl_pct": db_rec.get("sl_pct", 500)}
            tp_sl_pcts[symbol] = stored

    if len(positions) >= 5:
        await asyncio.gather(*[_check_pos(pos) for pos in positions])
    else:
        for pos in positions:
            await _check_pos(pos)

    # Clean up orphaned plan orders: cancel any active plan order whose symbol
    # has no open position on the exchange (covers DB-missing cases too)
    exchange_symbols = {pos["symbol"] for pos in positions}
    db_open_symbols = {p["symbol"] for p in db_mod.get_open_positions()}
    # DB-based cleanup (fast, no extra API call)
    async def _cancel_orphan(symbol):
        async with sem:
            try:
                n = await client.cancel_tp_sl_orders(symbol)
                if n > 0:
                    logger.info("Cancelled %d orphaned plan orders for closed position %s", n, symbol)
            except Exception as e:
                logger.warning("Orphan order cleanup %s: %s", symbol, e)

    orphan_syms = db_open_symbols - exchange_symbols
    log_event(
        "decisions", "tpsl_enforce_summary",
        exchange_symbols=sorted(exchange_symbols), db_open_symbols=sorted(db_open_symbols),
        orphan_symbols=sorted(orphan_syms),
    )
    if orphan_syms:
        await asyncio.gather(*[_cancel_orphan(s) for s in orphan_syms])

    # Sync DB: close any position that's open in DB but gone from exchange
    reentry_symbols = {r["symbol"] for r in db_mod.get_all_reentry()}
    for symbol in orphan_syms:
        if symbol in reentry_symbols:
            continue  # reentry_job will handle it (close + re-entry logic)
        db_mod.close_position(symbol)
        db_mod.close_position_history(symbol, exit_price=0, pnl=0, close_reason="liquidated")
        coin = symbol.split("/")[0]
        logger.info("DB sync: closed stale open position %s (not on exchange)", symbol)
        await _notify_all(app, f"💀 *{coin}* закрыта принудительно (ликвидация или внешнее закрытие)")

    # Full plan-order sweep every 5 min (every 5th run)
    run_count = app.bot_data.get("_tpsl_run_count", 0) + 1
    app.bot_data["_tpsl_run_count"] = run_count
    if run_count % 5 == 0:
        try:
            all_plan_orders = await client.get_tp_sl_orders()
            order_syms = {o["symbol"] for o in all_plan_orders}
            sweep_syms = order_syms - exchange_symbols
            if sweep_syms:
                await asyncio.gather(*[_cancel_orphan(s) for s in sweep_syms])
        except Exception as e:
            logger.debug("Plan order sweep failed: %s", e)


# ── BTC trend helper ──────────────────────────────────────────────

async def _get_btc_rsi_4h(client) -> float | None:
    """Return BTC RSI on 4h candles, or None on error."""
    try:
        import pandas as pd
        import pandas_ta as ta
        ohlcv = await client._exchange.fetch_ohlcv("BTC/USDT:USDT", "4h", limit=20)
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
        df["close"] = df["close"].astype(float)
        rsi = ta.rsi(df["close"], length=14)
        if rsi is None or rsi.empty:
            return None
        return float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None
    except Exception as e:
        logger.debug("_get_btc_rsi_4h: %s", e)
        return None


# ── Auto scan job ─────────────────────────────────────────────────

async def auto_scan_job(app):
    """Periodically scan market with AI and auto-open qualifying shorts."""
    import re as _re
    import datetime as _dt
    config = app.bot_data.get("config")
    if not config or not getattr(config, "auto_scan_enabled", False):
        return

    client = app.bot_data["exchange"]
    api_key = getattr(config, "openrouter_api_key", "")
    if not api_key:
        return

    interval_min = int(getattr(config, "auto_scan_interval_min", 30))
    max_pos = int(getattr(config, "auto_scan_max_positions", 3))
    max_risk = int(getattr(config, "auto_scan_max_risk", 7))

    now_str = _dt.datetime.now().strftime("%H:%M")
    next_str = (_dt.datetime.now() + _dt.timedelta(minutes=interval_min)).strftime("%H:%M")

    try:
        positions = await client.get_positions()
    except Exception as e:
        logger.warning("AutoScan: get_positions failed: %s", e)
        await _notify_all(app, f"🤖 *AutoScan* {now_str} — ошибка биржи: {e}\nСледующий: {next_str}")
        return

    cur_pos = len(positions)
    slots = max_pos - cur_pos
    if slots <= 0:
        await _notify_all(app,
            f"🤖 *AutoScan* {now_str} — позиций {cur_pos}/{max_pos}, слотов нет\n"
            f"Следующий: {next_str}")
        return

    # BTC trend filter — skip shorts when BTC is in uptrend (RSI 4h > 65)
    btc_rsi = await _get_btc_rsi_4h(client)
    btc_rsi_threshold = float(getattr(config, "btc_rsi_filter", 65.0))
    if btc_rsi is not None and btc_rsi > btc_rsi_threshold:
        await _notify_all(app,
            f"🚫 *AutoScan* {now_str} — пропущен\n"
            f"BTC RSI 4h = `{btc_rsi:.0f}` > `{btc_rsi_threshold:.0f}` — бычий рынок, шортить опасно\n"
            f"Следующий: {next_str}")
        return

    ask_n = min(slots * 2 + 2, 20)
    logger.info("AutoScan: %d free slots, asking AI for %d picks (BTC RSI 4h=%.0f)",
                slots, ask_n, btc_rsi or 0)

    from bot.ai.scanner import scan_overbought, analyze_single_coin, mexc_find_futures_symbol
    from bot.ai.analyst import (deep_short_analysis, parse_analyst_blocks,
                                DEFAULT_MODEL, FALLBACK_MODEL)

    # Heavy technical scan: prefer the separate worker process; fall back to
    # in-process if the worker is unavailable.
    worker = app.bot_data.get("scanner_worker")
    local_results = []
    if worker is not None and worker.alive:
        try:
            local_results, _ = await worker.scan(65.0, 10.0)
        except Exception as e:
            logger.warning("AutoScan: worker scan failed (%s); falling back in-process", e)
            worker = None
    if not local_results and (worker is None or not getattr(worker, "alive", False)):
        try:
            local_results, _ = await scan_overbought(client, 65.0, 10.0)
        except Exception as e:
            logger.warning("AutoScan: local scan failed: %s", e)
            local_results = []

    model = getattr(config, "openrouter_model", DEFAULT_MODEL) or DEFAULT_MODEL
    ai_result = await deep_short_analysis(local_results, api_key, model=model, n=ask_n)
    if ai_result.error and not ai_result.text:
        ai_result = await deep_short_analysis(local_results, api_key, model=FALLBACK_MODEL, n=ask_n)
    if ai_result.error and not ai_result.text:
        logger.warning("AutoScan: AI unavailable — %s", ai_result.error)
        await _notify_all(app,
            f"🤖 *AutoScan* {now_str} — AI недоступен: {ai_result.error}\n"
            f"Следующий: {next_str}")
        return

    picks = parse_analyst_blocks(ai_result.text, n=ask_n)

    def _risk_int(pick):
        m = _re.match(r"(\d+)", pick.get("risk", "10"))
        return int(m.group(1)) if m else 10

    # Least-risky first (ascending RISK)
    good_picks = sorted([p for p in picks if _risk_int(p) <= max_risk], key=_risk_int)
    filtered_out = len(picks) - len(good_picks)

    if not good_picks:
        msg = f"🤖 *AutoScan* {now_str} — AI выдал {len(picks)} пиков"
        if filtered_out:
            msg += f", все риск > {max_risk}/10"
        msg += f"\nСледующий: {next_str}"
        await _notify_all(app, msg)
        return

    open_coins = {p["symbol"].split("/")[0] for p in positions}
    margin = float(getattr(config, "default_trade_usdt", 0.20))
    tp_pct = float(getattr(config, "tp_pct", 500))
    sl_pct = float(getattr(config, "sl_pct", 500))
    user_lev = int(getattr(config, "default_leverage", 0) or 0)
    max_avg_count = int(getattr(config, "max_averaging_count", 100))
    avg_amount = float(getattr(config, "averaging_amount", 0.50))
    scan_risk_pct = float(getattr(config, "auto_scan_capital_pct", 0.0))
    profit_lock_trigger = float(getattr(config, "averaging_profit_lock_trigger", 0))
    base_budget = max_avg_count * avg_amount + margin
    # Multiply by SL factor unless profit-lock SL is set (position won't reach full loss)
    if profit_lock_trigger > 0:
        full_budget = base_budget
    else:
        full_budget = base_budget * (sl_pct / 100.0)
    min_balance = full_budget * (1.0 - scan_risk_pct / 100.0)

    try:
        free_balance = await client.get_free_futures_balance()
    except Exception:
        free_balance = 0.0

    from bot.handlers.trading import execute_open
    opened = 0
    opened_names: list[str] = []
    skipped: list[str] = []
    initial_open_count = len(positions)

    for pick in good_picks:
        if opened >= slots:
            break
        ticker = pick["ticker"].upper()
        if ticker in open_coins:
            skipped.append(f"{ticker}(позиция)")
            continue
        # Check SL cooldown — skip recently stopped symbols for 2h
        _sl_cd: dict = app.bot_data.get("_sl_cooldown", {})
        fut_sym_pre = await mexc_find_futures_symbol(client, ticker)
        if fut_sym_pre:
            cd_ts = _sl_cd.get(fut_sym_pre, 0)
            if time.time() - cd_ts < 2 * 3600:
                remain_min = int((2 * 3600 - (time.time() - cd_ts)) / 60)
                skipped.append(f"{ticker}(кулдаун {remain_min}м)")
                continue
        fut_sym = fut_sym_pre
        if not fut_sym:
            skipped.append(f"{ticker}(нет MEXC)")
            continue
        tech = await analyze_single_coin(client, fut_sym)
        if not tech:
            skipped.append(f"{ticker}(нет OHLCV)")
            continue

        # Direction: prefer AI's SIDE, fall back to local technical direction.
        side = pick.get("side") or tech.get("direction") or "short"
        order_side = "buy" if side == "long" else "sell"

        try:
            sym_max = await client.get_max_leverage(fut_sym)
        except Exception:
            sym_max = 100
        leverage = min(user_lev, sym_max) if user_lev > 0 else sym_max

        # Capital check: total balance must cover full_budget for ALL positions (existing + new)
        current_open = initial_open_count + opened
        total_available = free_balance + current_open * margin
        min_total = full_budget * (current_open + 1) * (1.0 - scan_risk_pct / 100.0)
        if min_total > 0 and total_available < min_total:
            skipped.append(f"{ticker}(мало депа)")
            logger.info("AutoScan: skip %s — total $%.2f < required $%.2f (%d poз × $%.2f, risk=%d%%)",
                        ticker, total_available, min_total, current_open + 1, full_budget, int(scan_risk_pct))
            continue

        try:
            result = await execute_open(client, app, fut_sym, order_side, margin, leverage,
                                        tp_pct=tp_pct, sl_pct=sl_pct, pick=pick)
            free_balance -= margin  # update local estimate after open
        except Exception as e:
            logger.error("AutoScan: open %s failed: %s", fut_sym, e)
            skipped.append(f"{ticker}(ошибка)")
            continue

        coin = fut_sym.split("/")[0]
        risk_val = _risk_int(pick)
        side_label = "LONG" if side == "long" else "SHORT"
        side_emoji = "🔺" if side == "long" else "🔻"
        lines = [
            f"🤖 *AutoScan* → {side_emoji} {side_label} `{coin}` риск {risk_val}/10",
            f"Entry: `{result['entry_price']:.6g}` | ×{result['leverage']} | `${margin:.2f}`",
        ]
        if result.get("tp_price"):
            lines.append(f"TP: `{result['tp_price']:.6g}` | SL: `{result.get('sl_price', 0):.6g}`")
        if pick.get("fund"):
            lines.append(f"_{pick['fund']}_")
        await _notify_all(app, "\n".join(lines))
        open_coins.add(ticker)
        opened_names.append(coin)
        opened += 1

    # Summary
    summary_lines = [f"🤖 *AutoScan* {now_str}"]
    if opened:
        summary_lines.append(f"✅ Открыто: {', '.join(f'`{c}`' for c in opened_names)}")
    else:
        summary_lines.append("— ничего не открыто")
    if skipped:
        summary_lines.append(f"Пропущено: {', '.join(skipped[:5])}")
    if filtered_out:
        summary_lines.append(f"Отфильтровано (риск > {max_risk}/10): {filtered_out}")
    summary_lines.append(f"Следующий: {next_str}")
    await _notify_all(app, "\n".join(summary_lines))
    logger.info("AutoScan %s: opened=%d skipped=%s", now_str, opened, skipped)


# ── Daily report job ─────────────────────────────────────────────

async def daily_report_job(app):
    """Ежедневный отчёт в 23:00 — статистика закрытых позиций за день."""
    import datetime as _dt
    from bot import db as db_mod
    today = _dt.datetime.now().strftime("%Y-%m-%d")
    try:
        with db_mod._connect() as conn:
            rows = conn.execute(
                "SELECT symbol, side, pnl, close_reason, hold_seconds "
                "FROM position_history WHERE closed_at LIKE ?",
                (f"{today}%",),
            ).fetchall()
    except Exception as e:
        logger.error("daily_report_job: %s", e)
        return

    if not rows:
        await _notify_all(app, f"📊 *Отчёт {today}*\nЗакрытых позиций сегодня не было")
        return

    closed = [dict(r) for r in rows]
    total_pnl = sum(r.get("pnl") or 0 for r in closed)
    winners = [r for r in closed if (r.get("pnl") or 0) > 0]
    losers  = [r for r in closed if (r.get("pnl") or 0) < 0]
    best  = max(closed, key=lambda r: r.get("pnl") or 0)
    worst = min(closed, key=lambda r: r.get("pnl") or 0)

    icon = "✅" if total_pnl >= 0 else "🛑"
    lines = [
        f"📊 *Отчёт за {today}*",
        f"Закрыто: {len(closed)} | ✅ {len(winners)} | 🛑 {len(losers)}",
        f"Итог: {icon} `{total_pnl:+.2f}$`",
    ]
    if best and (best.get("pnl") or 0) > 0:
        coin = best["symbol"].split("/")[0]
        lines.append(f"Лучшая: `{coin}` `{best['pnl']:+.2f}$`")
    if worst and (worst.get("pnl") or 0) < 0:
        coin = worst["symbol"].split("/")[0]
        lines.append(f"Худшая: `{coin}` `{worst['pnl']:+.2f}$`")
    win_rate = len(winners) / len(closed) * 100 if closed else 0
    lines.append(f"Win rate: `{win_rate:.0f}%`")
    await _notify_all(app, "\n".join(lines))


# ── Scheduler setup ───────────────────────────────────────────────

# Engine manager singleton (set in setup_scheduler). Engines replace the old
# APScheduler interval jobs with independent async loops.
_MANAGER = None


def _get_manager(app=None):
    global _MANAGER
    if app is not None:
        mgr = app.bot_data.get("engine_manager")
        if mgr is not None:
            return mgr
    return _MANAGER


def reschedule_averaging(app, interval: int):
    """Hot-update averaging engine interval without restarting the bot."""
    mgr = _get_manager(app)
    if mgr and mgr.set_interval("averaging", interval):
        logger.info("averaging engine rescheduled to every %ds", interval)


def reschedule_auto_scan(interval_min: int):
    """Hot-update auto_scan (scout) engine interval without restarting the bot."""
    mgr = _get_manager()
    if mgr and mgr.set_interval("auto_scan", interval_min * 60):
        logger.info("auto_scan engine rescheduled to every %dm", interval_min)


async def setup_scheduler(app):
    """Build and start the EngineManager (replaces APScheduler)."""
    global _MANAGER
    from bot.engines.base import EngineManager
    from bot.engines.monitor import MonitorEngine, BalanceAlertEngine
    from bot.engines.averaging import AveragingEngine
    from bot.engines.emergency import EmergencyEngine
    from bot.engines.reentry import ReentryEngine
    from bot.engines.tpsl import TpSlEngine
    from bot.engines.ladder import LadderExitEngine
    from bot.engines.scout import ScoutEngine
    from bot.engines.reporting import ReportingEngine
    from bot.engines.paper import PaperScanEngine, PaperSignalEngine, PaperUpdateEngine

    config = app.bot_data.get("config")
    avg_interval = int(getattr(config, "averaging_interval", 3)) if config else 3
    auto_scan_interval = int(getattr(config, "auto_scan_interval_min", 30)) if config else 30

    mgr = EngineManager(app)
    mgr.add(MonitorEngine(app))
    mgr.add(EmergencyEngine(app))
    mgr.add(AveragingEngine(app, interval=avg_interval))
    mgr.add(ReentryEngine(app))
    mgr.add(TpSlEngine(app))
    mgr.add(LadderExitEngine(app))
    mgr.add(BalanceAlertEngine(app))
    mgr.add(ScoutEngine(app, interval=auto_scan_interval * 60))
    mgr.add(ReportingEngine(app))
    mgr.add(PaperScanEngine(app))
    mgr.add(PaperSignalEngine(app))
    mgr.add(PaperUpdateEngine(app))

    # Pin auto-update: schedule the previously-unscheduled pin_update_job.
    from bot.engines.pin import PinEngine
    mgr.add(PinEngine(app))

    # Hybrid parallelism: spawn the scanner worker process (heavy pandas scan).
    if getattr(config, "scanner_worker_enabled", True):
        try:
            from bot.workers.scanner_worker import ScannerWorkerClient
            from bot.exchange.factory import provider_credentials
            api_key, secret, testnet = provider_credentials(config)
            provider = getattr(config, "exchange_provider", "mexc")
            worker = ScannerWorkerClient()
            if worker.start(provider, api_key, secret, testnet):
                app.bot_data["scanner_worker"] = worker
        except Exception:
            logger.exception("scanner worker unavailable; scanning runs in-process")

    app.bot_data["engine_manager"] = mgr
    _MANAGER = mgr
    await mgr.start_all()
    logger.info("Engines started (cache=3s, avg=%ds, reentry=30s, tpsl=60s, auto_scan=%dm)",
                avg_interval, auto_scan_interval)


async def _notify_all(app, text: str, reply_markup=None):
    config = app.bot_data.get("config")
    if not config:
        return
    for uid in (config.allowed_user_ids or []):
        try:
            await app.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown",
                                       reply_markup=reply_markup)
        except Exception as e:
            logger.warning("notify uid=%s: %s", uid, e)

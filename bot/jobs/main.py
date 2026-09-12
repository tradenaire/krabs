"""Background jobs: averaging, re-entry, TP/SL enforce, live positions monitor."""
import asyncio
import json
import logging
import time
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)
SCHEDULER = AsyncIOScheduler()

from bot.db import DATA_DIR
_EXHAUSTED_PATH = DATA_DIR / "avg_exhausted.json"


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
    global _position_lock
    if _position_lock is None:
        _position_lock = asyncio.Lock()
    return _position_lock


def _calc_tp_price(entry: float, leverage: int, tp_pct: float, side: str) -> float:
    move = entry * tp_pct / 100 / leverage
    return entry - move if side in ("short", "sell") else entry + move


def _calc_sl_price(entry: float, leverage: int, sl_pct: float, side: str) -> float:
    move = entry * sl_pct / 100 / leverage
    return entry + move if side in ("short", "sell") else entry - move


# ── Live positions monitor (pinned message, 3s) ───────────────────

def _format_live_text(bal: dict, positions: list[dict],
                      tp_sl_pcts: dict | None = None,
                      lev_cache: dict | None = None) -> str:
    from datetime import datetime
    from bot.fmt import fmt_pct, fmt_usd
    free = float(bal.get("free", {}).get("USDT", 0) or 0)
    total = float(bal.get("total", {}).get("USDT", 0) or 0)
    ts = datetime.now().strftime("%H:%M:%S")

    lines = [f"📊 *Монитор* `{ts}`",
             f"Баланс: `${total:.4f}` | Свободно: `${free:.4f}`"]

    if not positions:
        lines.append("_Нет открытых позиций_")
        return "\n".join(lines)

    total_pnl = sum(float(p.get("unrealized_pnl", 0)) for p in positions)
    lines.append(f"PnL итого: `{fmt_usd(total_pnl)}`")
    lines.append("")

    from bot import db as db_mod
    for i, pos in enumerate(positions, 1):
        symbol = pos["symbol"]
        coin = symbol.split("/")[0]
        side = pos.get("side", "")
        lev = int(pos.get("leverage", 1))
        pct = float(pos.get("percentage", 0))
        pnl = float(pos.get("unrealized_pnl", 0))
        entry = float(pos.get("entry_price", 0))
        mark = float(pos.get("mark_price", 0))
        liq = float(pos.get("liquidation_price", 0))
        margin = float(pos.get("margin", 0))

        side_e = "🔻" if side == "short" else "🟩"
        pct_s = fmt_pct(pct)
        pnl_s = fmt_usd(pnl)

        health = ""
        if liq > 0 and mark > 0:
            dist = abs(mark - liq) / mark * 100
            if dist < 3:
                health = " 💀"
            elif dist < 10:
                health = " ⚠️"
        if pct >= 200:
            health = " 🔥"

        pos_line = (
            f"{i}. {side_e}`{coin}`×{lev}{health}\n"
            f"   PnL: `{pct_s}` ({pnl_s}) | Entry: `{entry:.6g}` → `{mark:.6g}`\n"
            f"   Маржа: `${margin:.3f}`"
        )
        if liq > 0:
            pos_line += f" | Liq: `{liq:.6g}`"

        # TP/SL %
        stored = (tp_sl_pcts or {}).get(symbol, {})
        db_rec = db_mod.get_open_position(symbol)
        tp_pct = stored.get("tp_pct") or (db_rec.get("tp_pct") if db_rec else None)
        sl_pct = stored.get("sl_pct") or (db_rec.get("sl_pct") if db_rec else None)
        if tp_pct or sl_pct:
            tp_s = f"+{tp_pct:.0f}%" if tp_pct else "—"
            sl_s = f"-{sl_pct:.0f}%" if sl_pct else "—"
            pos_line += f"\n   TP `{tp_s}` SL `{sl_s}`"

        # Avg and re-entry progress
        if db_rec:
            avg_count = db_rec.get("averaging_count", 0)
            total_invested = db_rec.get("total_invested", 0)
            pos_line += f"\n   Докупок: `{avg_count}/{max_count}` | вложено `${total_invested:.2f}`"
        re_rec = db_mod.get_reentry(symbol)
        if re_rec:
            pos_line += f"\n   Перезаходов: `{re_rec.get('cycle_count', 0)}/{re_rec.get('max_cycles', 3)}`"

        # Max leverage and position limit (from cache)
        cached_lev = (lev_cache or {}).get(symbol, {})
        if cached_lev:
            max_lev = cached_lev.get("max_lev")
            max_usdt = cached_lev.get("max_usdt")
            lev_line = f"Макс ×{max_lev}" if max_lev else ""
            if max_usdt:
                lev_line += f" | Лимит ~${max_usdt:,.0f}"
            if lev_line:
                pos_line += f"\n   {lev_line}"

        lines.append(pos_line)

    return "\n".join(lines)


def _monitor_keyboard(positions: list[dict]) -> InlineKeyboardMarkup:
    from bot.fmt import fmt_pct, fmt_usd
    rows = []
    for pos in positions:
        coin = pos["symbol"].split("/")[0]
        pnl = float(pos.get("unrealized_pnl", 0))
        pct = float(pos.get("percentage", 0))
        icon = "✅" if pnl >= 0 else "🔻"
        label = f"❌ {icon} {coin}  {fmt_pct(pct)}  {fmt_usd(pnl)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"mon_close_{pos['symbol']}")])
    rows.append([InlineKeyboardButton("💰 Баланс", callback_data="balance_futures")])
    rows.append([InlineKeyboardButton("📈 Статистика", callback_data="mon_stats")])
    return InlineKeyboardMarkup(rows)


async def positions_monitor_job(app):
    """Обновляет запиненное сообщение с позициями каждые 3 секунды."""
    client = app.bot_data.get("exchange")
    config = app.bot_data.get("config")
    if not client or not config:
        return

    live_msgs: dict = app.bot_data.setdefault("_live_msgs", {})
    live_texts: dict = app.bot_data.setdefault("_live_texts", {})

    try:
        bal = await client.get_futures_balance()
        positions = await client.get_positions()
    except Exception as e:
        logger.debug("monitor: fetch failed: %s", e)
        return

    # Cache max_lev and position limit per symbol (refresh every 5 min)
    lev_cache: dict = app.bot_data.setdefault("_lev_cache", {})
    for pos in positions:
        sym = pos["symbol"]
        cached = lev_cache.get(sym)
        if not cached or time.time() - cached.get("ts", 0) > 300:
            try:
                lev = int(pos.get("leverage", 1))
                max_lev = await client.get_max_leverage(sym)
                max_usdt = await client.get_position_limit_usdt(sym, lev)
                lev_cache[sym] = {"max_lev": max_lev, "max_usdt": max_usdt, "ts": time.time()}
            except Exception:
                pass

    tp_sl_pcts: dict = app.bot_data.get("tp_sl_pcts", {})
    text = _format_live_text(bal, positions, tp_sl_pcts, lev_cache)
    has_positions = bool(positions)
    kb = _monitor_keyboard(positions) if has_positions else None

    for uid in (config.allowed_user_ids or []):
        last_text = live_texts.get(uid, "")
        if text == last_text:
            continue

        live_texts[uid] = text
        msg_id = live_msgs.get(uid)

        if msg_id:
            try:
                await app.bot.edit_message_text(
                    chat_id=uid, message_id=msg_id, text=text,
                    parse_mode="Markdown", reply_markup=kb
                )
            except Exception as e:
                err = str(e)
                if "message to edit not found" in err or "MESSAGE_ID_INVALID" in err:
                    live_msgs.pop(uid, None)
                    msg_id = None
                elif "Message is not modified" in err:
                    pass
                else:
                    logger.debug("monitor edit %s: %s", uid, err)

        if not msg_id:
            if not has_positions:
                continue
            try:
                sent = await app.bot.send_message(
                    chat_id=uid, text=text, parse_mode="Markdown", reply_markup=kb
                )
                live_msgs[uid] = sent.message_id
                try:
                    await app.bot.pin_chat_message(
                        chat_id=uid, message_id=sent.message_id,
                        disable_notification=True
                    )
                except Exception:
                    pass
            except Exception as e:
                logger.debug("monitor send %s: %s", uid, e)

        # Unpin + delete when no positions
        if not has_positions and msg_id:
            try:
                await app.bot.unpin_chat_message(chat_id=uid, message_id=msg_id)
                await app.bot.delete_message(chat_id=uid, message_id=msg_id)
            except Exception:
                pass
            live_msgs.pop(uid, None)
            live_texts.pop(uid, None)


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

    client = app.bot_data["exchange"]
    threshold = float(getattr(config, "averaging_threshold", -100))
    amount = float(getattr(config, "averaging_amount", 0.50))
    max_count = int(getattr(config, "max_averaging_count", 100))

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
    profit_lock_sl_pct = float(getattr(config, "averaging_profit_lock_sl_pct", 0))

    try:
        positions = [p for p in await client.get_positions() if db_mod.get_managed_position(p)]
    except Exception as error:
        logger.error("Averaging: positions unavailable: %s", error)
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

    db_positions = {p["symbol"]: db_mod.get_managed_position(p) for p in positions}
    notified_exhausted: set = app.bot_data.setdefault("_avg_notified_exhausted", set())

    # Contracts tracking: compare our expected count vs exchange
    _exp_contracts: dict = app.bot_data.setdefault("_expected_contracts", {})
    _contracts_warned: set = app.bot_data.setdefault("_contracts_warned", set())
    _profit_lock_step: dict = app.bot_data.setdefault("_profit_lock_step", {})

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

    for pos in positions:
        symbol = pos["symbol"]
        pnl_pct = float(pos.get("percentage", 0))

        # Skip if already processed this symbol in this run (duplicate position entries)
        if symbol in seen_this_run:
            logger.warning("Averaging: duplicate symbol %s in positions list, skipping", symbol)
            continue
        seen_this_run.add(symbol)

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
        if profit_lock_trigger > 0 and pnl_pct >= 100 and db_positions[symbol]["profit_lock_enabled"]:
            _PL_STEP = 50
            new_pl_step = (int(pnl_pct) // _PL_STEP) * _PL_STEP
            current_pl_step = db_positions[symbol].get("profit_lock_step") or 0
            if new_pl_step > current_pl_step:
                lock_sl_pct = new_pl_step - _PL_STEP  # e.g., step=100 → lock at +50%
                _pl_entry = float(pos.get("entry_price", 0) or 0)
                _pl_side = pos.get("side", "short")
                _pl_lev = int(pos.get("leverage") or 1)
                if _pl_entry > 0:
                    _tp_sl_pcts_pl = app.bot_data.get("tp_sl_pcts", {})
                    _stored_pl = _tp_sl_pcts_pl.get(symbol, {})
                    _tp_pct_val = _stored_pl.get("tp_pct") or float(getattr(config, "tp_pct", 500))
                    _new_tp = _calc_tp_price(_pl_entry, _pl_lev, _tp_pct_val, _pl_side)
                    _new_sl = _calc_tp_price(_pl_entry, _pl_lev, lock_sl_pct, _pl_side)
                    try:
                        await client.set_tp_sl(symbol, tp_price=_new_tp, sl_price=_new_sl,
                                               pos_data=pos, profit_lock_step=new_pl_step)
                        _pl_coin = symbol.split("/")[0]
                        await _notify_all(app,
                            f"🔒 *{_pl_coin}* профит-лок +{new_pl_step}%\n"
                            f"PnL `{pnl_pct:+.1f}%` → SL в `+{lock_sl_pct:.0f}%` PnL\n"
                            f"Триггер: `{_new_sl:.6g}`")
                    except Exception as _pl_e:
                        logger.warning("Profit lock SL %s: %s", symbol, _pl_e)
                        await _notify_all(app, f"⚠️ {symbol}: profit-lock не подтверждён: {_pl_e}")

        # Skip if position-limit or count-exhausted (notified_exhausted acts as permanent block)
        if symbol in notified_exhausted:
            continue

        if not config.averaging_enabled or time.time() < app.bot_data.get("_avg_disabled_until", 0):
            continue
        db_rec = db_positions[symbol]

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
                coin = symbol.split("/")[0]
                await _notify_all(app,
                    f"🚫 *Докупки закончились* `{coin}`\n"
                    f"Использовано `{avg_count}/{max_count}` докупок\n"
                    f"Позиция закроется по TP, SL или вручную `/close {coin}`")
            continue

        avg_side = "buy" if pos.get("side") == "long" else "sell"
        avg_lev = int(pos.get("leverage") or 1)
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
        async with _lock():
            try:
                order_result = await client.place_futures_order(symbol, avg_side, actual_amount, avg_lev,
                                                                margin_mode=avg_mm, expected_position_id=pos["position_id"])
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
                        confirmed = await client.set_tp_sl(symbol, tp_price=new_tp, sl_price=new_sl,
                                               pos_data=pos_after)
                        prices = {r["type"]: r["price"] for r in confirmed}
                        new_tp, new_sl = prices.get("TP"), prices.get("SL")
                        parts = []
                        if new_tp:
                            parts.append(f"TP: `{new_tp:.6g}`")
                        if new_sl:
                            parts.append(f"SL: `{new_sl:.6g}`")
                        tp_sl_text = "\n🔄 " + ", ".join(parts) + f" (avg: `{new_entry:.6g}`)"
                except Exception as e:
                    logger.warning("TP/SL recalc for %s: %s", symbol, e)
                    tp_sl_text = f"\n⚠️ Защита не подтверждена: {e}"

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

async def reentry_job(app):
    from bot import db as db_mod
    from bot.lifecycle import reconcile_closures, reset_runtime, pnl_text
    import datetime as dt
    client = app.bot_data["exchange"]
    config = app.bot_data["config"]
    try:
        live = await reconcile_closures(app)
    except Exception as error:
        logger.warning("Reconciliation unavailable: %s", error)
        return
    for re_cfg in db_mod.get_all_reentry():
        symbol = re_cfg["symbol"]
        record = db_mod.get_position_by_id(re_cfg.get("position_key"))
        if not record or not record.get("exchange_position_id"):
            db_mod.delete_reentry(symbol)
            continue
        same_symbol = [p for p in live if p["symbol"] == symbol]
        if same_symbol:
            if any(str(p["position_id"]) != record["exchange_position_id"] for p in same_symbol):
                db_mod.delete_reentry(symbol)
                reset_runtime(app, symbol)
            continue
        closure = db_mod.get_closure(record["id"])
        if not closure:
            continue
        maximum = min(int(re_cfg["max_cycles"]), int(config.max_reentry_cycles))
        reason = closure["reason"]
        if maximum <= 0 or int(re_cfg["cycle_count"]) >= maximum or reason not in ("tp", "sl", "profit_lock", "manual_reentry"):
            db_mod.delete_reentry(symbol)
            continue
        if reason == "sl" and not config.reentry_on_sl:
            app.bot_data.setdefault("_sl_cooldown", {})[symbol] = time.time()
            db_mod.delete_reentry(symbol)
            continue
        delay = 60 if reason == "profit_lock" else (config.reentry_sl_cooldown_min * 60 if reason == "sl" else 30)
        closed_ts = dt.datetime.fromisoformat(closure["closed_at"]).timestamp()
        if time.time() - closed_ts < delay:
            continue
        try:
            # Recheck after cooldown; execute_open also refuses any existing position.
            if await client.get_position(symbol):
                db_mod.delete_reentry(symbol)
                continue
            await client.cancel_tp_sl_orders(symbol, record["id"])
            from bot.handlers.trading import execute_open
            reset_runtime(app, symbol)
            cycle = int(re_cfg["cycle_count"]) + 1
            result = await execute_open(client, app, symbol, re_cfg["side"], config.default_trade_usdt,
                config.default_leverage or re_cfg["leverage"], config.tp_pct, config.sl_pct,
                cycle_count=cycle)
            db_mod.log_trade(symbol, "reentry", amount=result["margin"], note=f"cycle {cycle}")
            await _notify_all(app, f"Перезаход {symbol} #{cycle}/{maximum}; предыдущая сделка: {reason}\n"
                f"{pnl_text(closure)}\nEntry: {result['entry_price']}; защита: {result['protection_status']}")
        except Exception as error:
            logger.warning("Re-entry %s: %s", symbol, error)


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
        from bot.exchange.client import available_margin
        free = available_margin(bal)

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
    from bot import db as db_mod
    client = app.bot_data["exchange"]
    try:
        positions = await client.get_positions()
        for pos in positions:
            record = db_mod.get_managed_position(pos, allow_closing=True)
            if not record:
                continue
            entry, lev, side = pos["entry_price"], pos["leverage"], pos["side"]
            try:
                await client.set_tp_sl(pos["symbol"],
                    tp_price=_calc_tp_price(entry, lev, record["tp_pct"], side),
                    sl_price=_calc_sl_price(entry, lev, record["sl_pct"], side), pos_data=pos)
            except Exception as error:
                logger.error("Protection %s: %s", pos["symbol"], error)
                await _notify_all(app, f"⚠️ {pos['symbol']}: защита не подтверждена: {error}")
        # Cancel only our order IDs after a fresh position-ID check, never CancelAll.
        live_ids = {str(p["position_id"]) for p in positions}
        for key in {o["position_key"] for o in db_mod.get_bot_orders()}:
            record = db_mod.get_position_by_id(key)
            if record and record["exchange_position_id"] not in live_ids:
                fresh = await client.get_positions()
                if record["exchange_position_id"] not in {str(p["position_id"]) for p in fresh}:
                    await client.cancel_tp_sl_orders(record["symbol"], key)
    except Exception as error:
        logger.warning("Protection scan aborted: %s", error)


async def margin_emergency_job(app):
    from bot import db as db_mod
    from bot.exchange.client import available_margin
    import math
    config = app.bot_data["config"]
    threshold, trim = config.margin_emergency_threshold_pct, config.margin_emergency_trim_pct
    if not 0 < threshold <= 100 or not 0 < trim <= 100:
        return
    client = app.bot_data["exchange"]
    try:
        positions = [p for p in await client.get_positions() if db_mod.get_managed_position(p)]
        balance = await client.get_futures_balance()
        for currency in {p.get("settle_currency", "USDT") for p in positions}:
            avail = available_margin(balance, currency)
            free = float(balance.get("free", {}).get(currency, 0))
            low = avail <= 0 or (free > 0 and avail < free * threshold / 100)
            key = f"emergency_latched_{currency}"
            if not low:
                db_mod.set_config(key, "")
                continue
            app.bot_data["_avg_disabled_until"] = time.time() + 300
            if db_mod.get_config(key):
                continue
            # Persist before sending: no repeated trim after a timeout or restart.
            db_mod.set_config(key, "pending")
            for pos in positions:
                if pos.get("settle_currency", "USDT") != currency:
                    continue
                count = int(pos["contracts"])
                contracts = min(count, max(1, math.floor(count * trim / 100)))
                if contracts <= 0:
                    continue
                try:
                    await client.partial_close_futures_position(pos["symbol"], contracts,
                        expected_position_id=pos["position_id"])
                    await _notify_all(app, f"Аварийное сокращение {pos['symbol']}: отправлен ордер на "
                        f"{contracts}/{count} контрактов ({contracts/count*100:.1f}%). "
                        f"Доступно {avail:g} {currency}; порог {threshold:g}% от {free:g}. PnL ожидает исполнения.")
                except Exception as error:
                    await _notify_all(app, f"⚠️ Сокращение {pos['symbol']} не подтверждено: {error}")
            db_mod.set_config(key, "latched")
    except Exception as error:
        logger.warning("Emergency check aborted: %s", error)


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
    from bot.ai.analyst import (deep_short_analysis, parse_short_candidates,
                                DEFAULT_MODEL, FALLBACK_MODEL)

    try:
        local_results, _ = await scan_overbought(client, 65.0, 10.0)
    except Exception as e:
        logger.warning("AutoScan: local scan failed: %s", e)
        local_results = []

    model = getattr(config, "openrouter_model", DEFAULT_MODEL) or DEFAULT_MODEL
    from bot.ai.research_snapshot import research_context
    snapshot = await research_context(client, config, local_results)
    ai_result = await deep_short_analysis(local_results, api_key, model=model, n=ask_n, research_snapshot=snapshot)
    if ai_result.error and not ai_result.text:
        ai_result = await deep_short_analysis(local_results, api_key, model=FALLBACK_MODEL, n=ask_n, research_snapshot=snapshot)
    if ai_result.error and not ai_result.text:
        logger.warning("AutoScan: AI unavailable — %s", ai_result.error)
        await _notify_all(app,
            f"🤖 *AutoScan* {now_str} — AI недоступен: {ai_result.error}\n"
            f"Следующий: {next_str}")
        return

    picks = parse_short_candidates(ai_result.text, n=ask_n)

    def _risk_int(pick):
        m = _re.match(r"(\d+)", pick.get("risk", "10"))
        return int(m.group(1)) if m else 10

    good_picks = [p for p in picks if _risk_int(p) <= max_risk]
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
            result = await execute_open(client, app, fut_sym, "sell", margin, leverage,
                                        tp_pct=tp_pct, sl_pct=sl_pct)
            free_balance -= margin  # update local estimate after open
        except Exception as e:
            logger.error("AutoScan: open %s failed: %s", fut_sym, e)
            skipped.append(f"{ticker}(ошибка)")
            continue

        coin = fut_sym.split("/")[0]
        risk_val = _risk_int(pick)
        lines = [
            f"🤖 *AutoScan* → SHORT `{coin}` риск {risk_val}/10",
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
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
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
        f"Известный итог: {icon} `{total_pnl:+.2f}$`; PnL неизвестен: {sum(r.get('pnl') is None for r in closed)}",
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

def reschedule_averaging(app, interval: int):
    """Hot-update averaging job interval without restarting the bot."""
    from apscheduler.triggers.interval import IntervalTrigger
    SCHEDULER.reschedule_job(
        "averaging",
        trigger=IntervalTrigger(seconds=interval),
    )
    logger.info("averaging_job rescheduled to every %ds", interval)


def reschedule_auto_scan(interval_min: int):
    """Hot-update auto_scan_job interval without restarting the bot."""
    from apscheduler.triggers.interval import IntervalTrigger
    SCHEDULER.reschedule_job(
        "auto_scan",
        trigger=IntervalTrigger(minutes=interval_min),
    )
    logger.info("auto_scan_job rescheduled to every %dm", interval_min)


def setup_scheduler(app):
    from apscheduler.triggers.interval import IntervalTrigger

    config = app.bot_data.get("config")
    from bot.handlers.pin import pin_update_job
    SCHEDULER.add_job(pin_update_job, trigger=IntervalTrigger(seconds=30), args=[app],
                      id="pin_update", max_instances=1, replace_existing=True)
    avg_interval = int(getattr(config, "averaging_interval", 3)) if config else 3

    SCHEDULER.add_job(
        positions_cache_job,
        trigger=IntervalTrigger(seconds=3),
        args=[app],
        id="positions_cache",
        max_instances=1,
        replace_existing=True,
    )
    SCHEDULER.add_job(
        averaging_job,
        trigger=IntervalTrigger(seconds=avg_interval),
        args=[app],
        id="averaging",
        max_instances=1,
        replace_existing=True,
    )
    SCHEDULER.add_job(margin_emergency_job, trigger=IntervalTrigger(seconds=10), args=[app],
                      id="margin_emergency", max_instances=1, replace_existing=True)
    SCHEDULER.add_job(
        reentry_job,
        trigger=IntervalTrigger(seconds=30),
        args=[app],
        id="reentry",
        max_instances=1,
        replace_existing=True,
    )
    SCHEDULER.add_job(
        tpsl_enforce_job,
        trigger=IntervalTrigger(seconds=60),
        args=[app],
        id="tpsl_enforce",
        max_instances=1,
        replace_existing=True,
    )

    SCHEDULER.add_job(
        balance_alert_job,
        trigger=IntervalTrigger(minutes=3),
        args=[app],
        id="balance_alert",
        max_instances=1,
        replace_existing=True,
    )

    auto_scan_interval = int(getattr(config, "auto_scan_interval_min", 30)) if config else 30
    SCHEDULER.add_job(
        auto_scan_job,
        trigger=IntervalTrigger(minutes=auto_scan_interval),
        args=[app],
        id="auto_scan",
        max_instances=1,
        replace_existing=True,
    )

    from apscheduler.triggers.cron import CronTrigger
    SCHEDULER.add_job(
        daily_report_job,
        trigger=CronTrigger(hour=23, minute=0),
        args=[app],
        id="daily_report",
        max_instances=1,
        replace_existing=True,
    )

    from bot.paper_trading import paper_scan_job, paper_update_job, paper_signal_job
    SCHEDULER.add_job(
        paper_scan_job,
        trigger=IntervalTrigger(minutes=30),
        args=[app],
        id="paper_scan",
        max_instances=1,
        replace_existing=True,
    )
    SCHEDULER.add_job(
        paper_signal_job,
        trigger=IntervalTrigger(minutes=5),
        args=[app],
        id="paper_signal",
        max_instances=1,
        replace_existing=True,
    )
    SCHEDULER.add_job(
        paper_update_job,
        trigger=IntervalTrigger(seconds=20),
        args=[app],
        id="paper_update",
        max_instances=1,
        replace_existing=True,
    )


    SCHEDULER.start()
    logger.info("Scheduler started (cache=3s, avg=%ds, reentry=30s, tpsl=60s, auto_scan=%dm, paper_scan=30m, paper_signal=5m)",
                avg_interval, auto_scan_interval)


async def _notify_all(app, text: str, reply_markup=None):
    from bot.event_logger import log_event
    config = app.bot_data.get("config")
    if not config:
        return
    for uid in (config.allowed_user_ids or []):
        try:
            await app.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown",
                                       reply_markup=reply_markup)
            log_event("notification_sent", user_id=uid, text_length=len(text))
        except Exception as e:
            log_event("notification_failed", user_id=uid, error_type=type(e).__name__)
            logger.warning("notify uid=%s: %s", uid, e)

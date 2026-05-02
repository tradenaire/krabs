"""Background jobs: averaging, re-entry, TP/SL enforce, live positions monitor."""
import asyncio
import logging
import time
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)
SCHEDULER = AsyncIOScheduler()

_position_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _position_lock
    if _position_lock is None:
        _position_lock = asyncio.Lock()
    return _position_lock


def _calc_tp_price(entry: float, leverage: int, tp_pct: float, side: str) -> float:
    move = entry * tp_pct / 100 / leverage
    return entry - move if side == "short" else entry + move


def _calc_sl_price(entry: float, leverage: int, sl_pct: float, side: str) -> float:
    move = entry * sl_pct / 100 / leverage
    return entry + move if side == "short" else entry - move


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
    profit_lock_trigger = float(getattr(config, "averaging_profit_lock_trigger", 0))
    profit_lock_sl_pct = float(getattr(config, "averaging_profit_lock_sl_pct", 0))

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

    free_balance = app.bot_data.get("_bal_cache", 0.0)

    db_positions = {p["symbol"]: p for p in db_mod.get_open_positions()}
    synth_store = app.bot_data.setdefault("_avg_synth", {})
    notified_exhausted: set = app.bot_data.setdefault("_avg_notified_exhausted", set())

    # Contracts tracking: compare our expected count vs exchange
    _exp_contracts: dict = app.bot_data.setdefault("_expected_contracts", {})
    _contracts_warned: set = app.bot_data.setdefault("_contracts_warned", set())
    _profit_locked: set = app.bot_data.setdefault("_profit_locked", set())

    # Cleanup stale symbols (position closed on exchange)
    current_symbols = {p["symbol"] for p in positions}
    for sym in list(_exp_contracts.keys()):
        if sym not in current_symbols:
            del _exp_contracts[sym]
            _contracts_warned.discard(sym)
    for sym in list(notified_exhausted):
        if sym not in current_symbols:
            notified_exhausted.discard(sym)
    for sym in list(_profit_locked):
        if sym not in current_symbols:
            _profit_locked.discard(sym)

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

        # ── Profit lock: move SL into profit zone ────────────────────
        if (profit_lock_trigger > 0 and profit_lock_sl_pct > 0
                and pnl_pct >= profit_lock_trigger
                and symbol not in _profit_locked):
            _pl_entry = float(pos.get("entry_price", 0) or 0)
            _pl_side = pos.get("side", "short")
            _pl_lev = int(pos.get("leverage") or 1)
            if _pl_entry > 0:
                _tp_sl_pcts = app.bot_data.get("tp_sl_pcts", {})
                _stored = _tp_sl_pcts.get(symbol, {})
                _tp_pct_val = _stored.get("tp_pct") or float(getattr(config, "tp_pct", 500))
                _new_tp = _calc_tp_price(_pl_entry, _pl_lev, _tp_pct_val, _pl_side)
                _new_sl = _calc_tp_price(_pl_entry, _pl_lev, profit_lock_sl_pct, _pl_side)
                try:
                    await client.set_tp_sl(symbol, tp_price=_new_tp, sl_price=_new_sl, pos_data=pos)
                    _profit_locked.add(symbol)
                    _pl_coin = symbol.split("/")[0]
                    await _notify_all(app,
                        f"🔒 *{_pl_coin}* SL перемещён в профит\n"
                        f"PnL `{pnl_pct:+.1f}%` ≥ `+{profit_lock_trigger:.0f}%` → SL в `+{profit_lock_sl_pct:.0f}%` PnL\n"
                        f"TP: `{_new_tp:.6g}` | SL: `{_new_sl:.6g}`")
                except Exception as _pl_e:
                    logger.warning("Profit lock SL %s: %s", symbol, _pl_e)

        # Skip if averaged too recently (prevents double-order from retry/race)
        last_avg = _avg_ts.get(symbol, 0)
        if now_ts - last_avg < 5:
            continue

        if pnl_pct > threshold:
            continue

        db_rec = db_positions.get(symbol)
        if db_rec is None:
            if symbol not in synth_store:
                synth_store[symbol] = {
                    "id": None, "symbol": symbol,
                    "total_invested": float(pos.get("margin", 0) or amount),
                    "averaging_count": 0,
                    "tp_pct": 500, "sl_pct": 500,
                }
            db_rec = synth_store[symbol]

        total_invested = float(db_rec.get("total_invested") or 0)
        avg_count = int(db_rec.get("averaging_count") or 0)

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

        # Check minimum contract cost — use it if larger than configured amount
        try:
            min_cost = await client.get_min_order_usdt(symbol, avg_lev)
        except Exception:
            min_cost = 0.0
        actual_amount = max(amount, min_cost) if min_cost > 0 else amount
        if min_cost > amount:
            logger.info("Avg %s: min contract $%.2f > amount $%.2f, using min",
                        symbol, min_cost, amount)

        if free_balance < actual_amount:
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
                                                                margin_mode=avg_mm)
            except Exception as e:
                err_msg = str(e).lower()
                logger.error("Averaging order FAILED for %s: %s", symbol, e)
                # Position limit hit — mute this symbol until position closes
                _POS_LIMIT_KEYWORDS = ("exceed", "position size", "max position",
                                       "position limit", "risk limit", "too large")
                if any(kw in err_msg for kw in _POS_LIMIT_KEYWORDS):
                    notified_exhausted.add(symbol)
                    coin = symbol.split("/")[0]
                    await _notify_all(app,
                        f"🚫 *Докупки закончились* `{coin}`\n"
                        f"Биржа отклонила: лимит позиции достигнут\n"
                        f"Позиция закроется по TP, SL или вручную `/close {coin}`")
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

        msg = (
            f"*Докупка #{new_count}/{max_count}* `{coin}`\n"
            f"Позиция: `{old_contracts}` → `{new_contracts}` контр. | `${total_invested:.2f}` → `${new_total:.2f}`\n"
            f"PnL: `{pnl_pct:+.1f}%` / `${old_pnl_usd:+.2f}` → `{new_pnl_pct:+.1f}%` / `${new_pnl_usd:+.2f}`\n"
            f"+`${actual_amount:.2f}` (×{avg_lev})"
            f"{shift}{liq_warn}{tp_sl_text}"
        )
        await _notify_all(app, msg)


# ── Re-entry job ──────────────────────────────────────────────────

async def _resolve_close_reason(client, symbol: str, pos_side_str: str,
                                opened_at_ms: int | None,
                                entry_price: float) -> tuple[bool | None, bool]:
    """Returns (closed_by_tp, profitable_sl).
    closed_by_tp: True=TP, False=SL, None=unknown.
    profitable_sl: True if SL triggered but at a price better than entry (profit-lock SL)."""
    is_tp, trigger_price = await client.was_closed_by_tp(symbol, pos_side_str, opened_at_ms)

    profitable_sl = False
    if is_tp is False and trigger_price and entry_price > 0:
        # SL in profit zone: for short trigger_price < entry; for long trigger_price > entry
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
        except Exception as e:
            logger.debug("_resolve_close_reason price fallback %s: %s", symbol, e)

    return is_tp, profitable_sl


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
        if ph and ph.get("opened_at"):
            try:
                import datetime as _dt
                opened_at_ms = int(
                    _dt.datetime.fromisoformat(ph["opened_at"]).timestamp() * 1000
                )
            except Exception:
                pass

        if max_cycles == 0:
            closed_by_tp, profitable_sl = await _resolve_close_reason(client, symbol, pos_side_str, opened_at_ms, entry_price)
            if closed_by_tp is None:
                continue
            close_note = "tp" if (closed_by_tp or profitable_sl) else "sl"
            db_mod.log_trade(symbol, "close", note=close_note)
            db_mod.close_position_history(symbol, exit_price=0, pnl=0, close_reason=close_note)
            icon = "✅" if (closed_by_tp or profitable_sl) else "🛑"
            label = "по тейку" if closed_by_tp else ("по профит-локк SL" if profitable_sl else "по стопу")
            await _notify_all(app, f"{icon} *{coin}* {label} (перезаход отключён)")
            db_mod.delete_reentry(symbol)
            continue

        if cycle_count >= max_cycles:
            logger.info("Re-entry: %s exhausted (%d/%d cycles)", symbol, cycle_count, max_cycles)
            closed_by_tp, profitable_sl = await _resolve_close_reason(client, symbol, pos_side_str, opened_at_ms, entry_price)
            if closed_by_tp is None:
                continue
            close_note = "tp" if (closed_by_tp or profitable_sl) else "sl"
            db_mod.log_trade(symbol, "close", note=close_note)
            db_mod.close_position_history(symbol, exit_price=0, pnl=0, close_reason=close_note)
            icon = "✅" if (closed_by_tp or profitable_sl) else "🛑"
            label = "по тейку" if closed_by_tp else ("по профит-локк SL" if profitable_sl else "по стопу")
            await _notify_all(app, f"{icon} *{coin}* {label} — циклы исчерпаны ({cycle_count}/{max_cycles})")
            db_mod.delete_reentry(symbol)
            continue

        # 30s cooldown between cycles
        last_check_key = f"_reentry_ts_{symbol}"
        last_ts = app.bot_data.get(last_check_key, 0)
        if time.time() - last_ts < 30:
            continue
        app.bot_data[last_check_key] = time.time()

        margin = float(re_cfg.get("margin") or 1.0)
        leverage = int(re_cfg.get("leverage") or 0) or None
        tp_pct = float(re_cfg.get("tp_pct") or 500)
        sl_pct = float(re_cfg.get("sl_pct") or 500)

        logger.info("Re-entry #%d %s %s $%.2f", cycle_count + 1, symbol, side, margin)

        # Determine close reason: TP or profitable-SL → re-enter, loss-SL → skip
        closed_by_tp, profitable_sl = await _resolve_close_reason(client, symbol, pos_side_str, opened_at_ms, entry_price)

        if closed_by_tp is None:
            logger.info("Re-entry: %s close reason unknown, retrying next cycle", symbol)
            continue

        close_note = "tp" if (closed_by_tp or profitable_sl) else "sl"
        still_open = db_mod.get_open_position(symbol) is not None
        if still_open:
            db_mod.close_position(symbol)
            db_mod.log_trade(symbol, "close", note=close_note)
            db_mod.close_position_history(symbol, exit_price=0, pnl=0, close_reason=close_note)

        if not closed_by_tp and not profitable_sl:
            await _notify_all(app,
                f"🛑 *{coin}* закрыта по стопу — перезаход пропущен")
            db_mod.delete_reentry(symbol)
            continue

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
            db_mod.log_trade(symbol, "reentry", amount=margin, note=f"cycle {new_cycle}")
            # Clear exhausted flag so new cycle gets fresh averaging tracking
            notified_exhausted: set = app.bot_data.setdefault("_avg_notified_exhausted", set())
            notified_exhausted.discard(symbol)
            msg = (
                f"✅ *{coin}* в профит → перезаход #{new_cycle}/{max_cycles}\n"
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
    """Каждые 60с проверяет — есть ли TP/SL у каждой позиции."""
    from bot import db as db_mod
    client = app.bot_data["exchange"]
    tp_sl_pcts = app.bot_data.get("tp_sl_pcts", {})

    try:
        positions = await client.get_positions()
    except Exception as e:
        logger.error("TP/SL enforce: get_positions: %s", e)
        return

    sem = asyncio.Semaphore(5)  # max 5 concurrent MEXC requests

    async def _check_pos(pos):
        symbol = pos["symbol"]
        stored = tp_sl_pcts.get(symbol)
        if not stored:
            db_rec = db_mod.get_open_position(symbol)
            if db_rec:
                stored = {"tp_pct": db_rec.get("tp_pct", 500), "sl_pct": db_rec.get("sl_pct", 500)}
                tp_sl_pcts[symbol] = stored
            else:
                return
        async with sem:
            try:
                existing = await client.get_tp_sl_orders(symbol)
            except Exception:
                return
        if not existing:
            entry = float(pos.get("entry_price", 0) or 0)
            lev = int(pos.get("leverage", 1) or 1)
            side = pos.get("side", "short")
            async with sem:
                try:
                    tp = _calc_tp_price(entry, lev, stored["tp_pct"], side)
                    sl = _calc_sl_price(entry, lev, stored["sl_pct"], side)
                    await client.set_tp_sl(symbol, tp_price=tp, sl_price=sl, pos_data=pos)
                    logger.info("TP/SL enforce: restored for %s (tp=%.6g sl=%.6g)", symbol, tp, sl)
                except Exception as e:
                    logger.error("TP/SL enforce failed for %s: %s", symbol, e)

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

    ask_n = min(slots * 2 + 2, 20)
    logger.info("AutoScan: %d free slots, asking AI for %d picks", slots, ask_n)

    from bot.ai.scanner import scan_overbought, analyze_single_coin, mexc_find_futures_symbol
    from bot.ai.analyst import (deep_short_analysis, parse_analyst_blocks,
                                DEFAULT_MODEL, FALLBACK_MODEL)

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
        fut_sym = await mexc_find_futures_symbol(client, ticker)
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

    from bot.paper_trading import paper_scan_job, paper_update_job
    SCHEDULER.add_job(
        paper_scan_job,
        trigger=IntervalTrigger(minutes=30),
        args=[app],
        id="paper_scan",
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
    logger.info("Scheduler started (cache=3s, avg=%ds, reentry=30s, tpsl=60s, auto_scan=%dm, paper_scan=30m)",
                avg_interval, auto_scan_interval)


async def _notify_all(app, text: str):
    config = app.bot_data.get("config")
    if not config:
        return
    for uid in (config.allowed_user_ids or []):
        try:
            await app.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.warning("notify uid=%s: %s", uid, e)

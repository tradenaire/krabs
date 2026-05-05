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
                    # Persistent flag в БД: гарантирует re-entry даже если бот рестартанёт
                    # или was_closed_by_tp потеряет trigger_price из-за rate-limit.
                    # См. reentry_job:_apply_profit_lock_override.
                    db_mod.set_reentry_profit_locked(symbol, True)
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

        _min_order_cache: dict = app.bot_data.setdefault("_min_order_cache", {})
        cached_min_notional = _min_order_cache.get(symbol, 0)
        if cached_min_notional > 0 and amount * max(avg_lev, 1) <= cached_min_notional:
            actual_amount = await client.get_min_order_usdt(
                symbol, avg_lev, min_notional=cached_min_notional
            ) or (cached_min_notional / max(avg_lev, 1) * 1.05)
            logger.info("Avg %s: upgrading amount $%.2f -> $%.2f (min notional $%.0f)",
                        symbol, amount, actual_amount, cached_min_notional)
        else:
            actual_amount = amount

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
                raw_err = str(e)
                # Reset timestamp so next run can retry (we did not place an order)
                _avg_ts.pop(symbol, None)
                # Position limit hit — mute this symbol until position closes
                _POS_LIMIT_KEYWORDS = ("exceed", "position size", "max position",
                                       "position limit", "risk limit", "too large")
                _MIN_ORDER_KEYWORDS = ("minimum order amount", "min order", "7008", "less than the minimum")
                if any(kw in err_msg for kw in _POS_LIMIT_KEYWORDS):
                    notified_exhausted.add(symbol)
                    coin = symbol.split("/")[0]
                    logger.error("Averaging order FAILED for %s: %s", symbol, e)
                    await _notify_all(app,
                        f"🚫 *Докупки закончились* `{coin}`\n"
                        f"Биржа отклонила: лимит позиции достигнут\n"
                        f"Позиция закроется по TP, SL или вручную `/close {coin}`")
                    continue
                elif any(kw in err_msg for kw in _MIN_ORDER_KEYWORDS):
                    import re as _re
                    m = _re.search(r'(?i)"value"\s*:\s*(\d+(?:\.\d+)?)', raw_err)
                    min_usdt_notional = float(m.group(1)) if m else 5.0
                    # Cache so next cycle uses correct margin automatically
                    _min_order_cache[symbol] = min_usdt_notional
                    try:
                        from bot import db as _db_c
                        _db_c.set_min_order_notional(symbol, min_usdt_notional)
                    except Exception:
                        pass
                    min_margin = await client.get_min_order_usdt(
                        symbol, avg_lev, min_notional=min_usdt_notional
                    ) or (min_usdt_notional / max(avg_lev, 1) * 1.05)
                    coin = symbol.split("/")[0]
                    logger.info("Avg %s: cached min notional $%.1f -> next avg $%.3f (auto-upgrade)",
                                symbol, min_usdt_notional, min_margin)
                    if min_margin > actual_amount and free_balance >= min_margin:
                        try:
                            order_result = await client.place_futures_order(
                                symbol, avg_side, min_margin, avg_lev, margin_mode=avg_mm
                            )
                            actual_amount = min_margin
                            logger.info("Avg %s: retry succeeded at MEXC min margin $%.3f",
                                        symbol, actual_amount)
                        except Exception as retry_e:
                            logger.error("Averaging retry FAILED for %s at $%.3f: %s",
                                         symbol, min_margin, retry_e)
                            continue
                    else:
                        continue
                else:
                    logger.error("Averaging order FAILED for %s: %s", symbol, e)
                    continue

        new_total = total_invested + actual_amount
        if order_result and order_result.get("margin"):
            actual_amount = max(actual_amount, float(order_result["margin"]))
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
                        # После докупки новый SL пересчитывается по обычной формуле
                        # _calc_sl_price (loss-зона относительно нового entry).
                        # Если профит-лок ранее поднял флаг — сбрасываем, иначе при
                        # срабатывании этого SL мы ложно re-enter на убыточном close.
                        # averaging_job снова дождётся pnl_pct ≥ trigger и снова
                        # поднимет флаг + переставит SL в новую профит-зону.
                        if new_sl is not None:
                            sl_in_profit = (
                                (new_sl < new_entry) if p_side == "short"
                                else (new_sl > new_entry)
                            )
                            if not sl_in_profit:
                                db_mod.set_reentry_profit_locked(symbol, False)
                                _profit_locked.discard(symbol)
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
                                entry_price: float) -> tuple[bool | None, bool, float | None]:
    """Returns (closed_by_tp, profitable_sl, exit_price).
    closed_by_tp: True=TP, False=SL, None=unknown.
    profitable_sl: True if SL triggered but at a price better than entry (profit-lock SL).
    exit_price:  цена, по которой исполнился plan order (или mark при fallback) —
                 используется для расчёта realized PnL в сообщении пользователю.
                 None если данных нет."""
    is_tp, trigger_price = await client.was_closed_by_tp(symbol, pos_side_str, opened_at_ms)

    profitable_sl = False
    if is_tp is False and trigger_price and entry_price > 0:
        # SL in profit zone: for short trigger_price < entry; for long trigger_price > entry
        profitable_sl = (
            (trigger_price < entry_price) if pos_side_str == "short"
            else (trigger_price > entry_price)
        )

    # exit_price предпочитаем trigger_price (точная цена сделки plan-order),
    # иначе — mark из fallback-ветки ниже, иначе — None.
    exit_price: float | None = trigger_price if trigger_price else None

    if is_tp is None and entry_price > 0:
        try:
            ticker = await client._exchange.fetch_ticker(symbol)
            mark = float(ticker.get("last", 0) or 0)
            if mark > 0:
                is_tp = (mark < entry_price) if pos_side_str == "short" else (mark > entry_price)
                if exit_price is None:
                    exit_price = mark
        except Exception as e:
            logger.debug("_resolve_close_reason price fallback %s: %s", symbol, e)

    return is_tp, profitable_sl, exit_price


def _apply_profit_lock_override(closed_by_tp: bool | None, profitable_sl: bool,
                                re_cfg: dict) -> tuple[bool | None, bool]:
    """Forces profitable_sl=True если для symbol установлен флаг profit_locked в БД.

    Зачем: averaging_job устанавливает profit_locked=1 в БД когда переставляет SL в
    профит-зону. После закрытия позиции по такому SL `_resolve_close_reason` может
    вернуть `profitable_sl=False` если `was_closed_by_tp` потеряла trigger_price из-за
    rate-limit MEXC, лага plan-orders или рестарта бота. Без этого override
    позиция выпала бы в loss-SL ветку и re-entry бы не случился.

    Условия применения:
      - closed_by_tp is False — резолв определил что был SL, но не уверен profit/loss.
        НЕ переопределяем None (неопределённость = ждём следующий тик).
      - profitable_sl is False — иначе и так re-enter, override не нужен.
      - re_cfg['profit_locked'] truthy — флаг был поднят averaging_job.

    Возвращает (closed_by_tp, profitable_sl) с возможной коррекцией profitable_sl.
    """
    if closed_by_tp is False and not profitable_sl and bool(re_cfg.get("profit_locked")):
        return closed_by_tp, True
    return closed_by_tp, profitable_sl


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

        # Размер позиции для расчёта реализованного PnL — используем total_invested
        # из position_history (учитывает все докупки), fallback на initial margin.
        # Leverage берём из ph (актуальный для закрытой позиции), fallback на re_cfg.
        ph_total_invested = float(ph.get("total_invested") or 0) if ph else 0
        ph_leverage = int(ph.get("leverage") or 0) if ph else 0
        re_margin = float(re_cfg.get("margin") or 1.0)
        re_leverage = int(re_cfg.get("leverage") or 0)
        pnl_margin = ph_total_invested or re_margin
        pnl_lev = ph_leverage or re_leverage or 1

        from bot.fmt import format_close_pnl, calc_close_pnl

        def _pnl_suffix(exit_price: float | None) -> tuple[str, float]:
            """Возвращает (' (+$X / +Y%)' для текста, числовой pnl_usdt для DB)."""
            if not exit_price or entry_price <= 0:
                return "", 0.0
            pnl_usdt, _ = calc_close_pnl(entry_price, exit_price, pos_side_str, pnl_lev, pnl_margin)
            human = format_close_pnl(entry_price, exit_price, pos_side_str, pnl_lev, pnl_margin)
            return f" {human}" if human else "", pnl_usdt

        # Сохраняем факт profit-lock ДО override чтобы потом различить "natural TP"
        # vs "forced via flag" в сообщении re-entry. После override это значение
        # становится частью profitable_sl и его уже не отличить.
        was_profit_locked = bool(re_cfg.get("profit_locked"))

        if max_cycles == 0:
            closed_by_tp, profitable_sl, exit_price = await _resolve_close_reason(client, symbol, pos_side_str, opened_at_ms, entry_price)
            closed_by_tp, profitable_sl = _apply_profit_lock_override(closed_by_tp, profitable_sl, re_cfg)
            if closed_by_tp is None:
                continue
            close_note = "tp" if (closed_by_tp or profitable_sl) else "sl"
            pnl_text, pnl_usdt = _pnl_suffix(exit_price)
            db_mod.log_trade(symbol, "close", pnl=pnl_usdt, note=close_note)
            db_mod.close_position_history(symbol, exit_price=exit_price or 0, pnl=pnl_usdt, close_reason=close_note)
            icon = "✅" if (closed_by_tp or profitable_sl) else "🛑"
            label = "по тейку" if closed_by_tp else ("по профит-локк SL" if profitable_sl else "по стопу")
            await _notify_all(app, f"{icon} *{coin}* {label}{pnl_text} (перезаход отключён)")
            db_mod.delete_reentry(symbol)
            continue

        if cycle_count >= max_cycles:
            logger.info("Re-entry: %s exhausted (%d/%d cycles)", symbol, cycle_count, max_cycles)
            closed_by_tp, profitable_sl, exit_price = await _resolve_close_reason(client, symbol, pos_side_str, opened_at_ms, entry_price)
            closed_by_tp, profitable_sl = _apply_profit_lock_override(closed_by_tp, profitable_sl, re_cfg)
            if closed_by_tp is None:
                continue
            close_note = "tp" if (closed_by_tp or profitable_sl) else "sl"
            pnl_text, pnl_usdt = _pnl_suffix(exit_price)
            db_mod.log_trade(symbol, "close", pnl=pnl_usdt, note=close_note)
            db_mod.close_position_history(symbol, exit_price=exit_price or 0, pnl=pnl_usdt, close_reason=close_note)
            icon = "✅" if (closed_by_tp or profitable_sl) else "🛑"
            label = "по тейку" if closed_by_tp else ("по профит-локк SL" if profitable_sl else "по стопу")
            await _notify_all(app, f"{icon} *{coin}* {label}{pnl_text} — циклы исчерпаны ({cycle_count}/{max_cycles})")
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

        # Determine close reason: TP or profitable-SL → re-enter, loss-SL → skip.
        # Override: если re_cfg.profit_locked=1 (averaging_job переставлял SL в профит)
        # и резолв вернул closed_by_tp=False, profitable_sl=False — форсим
        # profitable_sl=True, потому что мы УВЕРЕНЫ что SL был в плюсе.
        # Это страхует от потери trigger_price из-за rate-limit MEXC.
        closed_by_tp, profitable_sl, exit_price = await _resolve_close_reason(client, symbol, pos_side_str, opened_at_ms, entry_price)
        _orig_profitable_sl = profitable_sl
        closed_by_tp, profitable_sl = _apply_profit_lock_override(closed_by_tp, profitable_sl, re_cfg)
        if was_profit_locked and profitable_sl and not _orig_profitable_sl:
            logger.info("Re-entry %s: profit-lock SL override applied (resolved as loss-SL but flag was set)", symbol)

        if closed_by_tp is None:
            logger.info("Re-entry: %s close reason unknown, retrying next cycle", symbol)
            continue

        close_note = "tp" if (closed_by_tp or profitable_sl) else "sl"
        pnl_text, pnl_usdt = _pnl_suffix(exit_price)
        still_open = db_mod.get_open_position(symbol) is not None
        if still_open:
            db_mod.close_position(symbol)
            db_mod.log_trade(symbol, "close", pnl=pnl_usdt, note=close_note)
            db_mod.close_position_history(symbol, exit_price=exit_price or 0, pnl=pnl_usdt, close_reason=close_note)

        if not closed_by_tp and not profitable_sl:
            # Loss-SL ветка: по умолчанию обрываем re-entry (защита от двойной
            # просадки). Если пользователь явно включил config.reenter_on_loss_sl
            # (через /avg визард или `/avg reenter_on_loss_sl 1`) — продолжаем
            # цикл, тратя следующий cycle. Уведомление различается чтобы было
            # видно какая ветка отработала.
            config = app.bot_data.get("config")
            reenter_on_loss = bool(getattr(config, "reenter_on_loss_sl", False)) if config else False
            if reenter_on_loss:
                await _notify_all(app,
                    f"🛑 *{coin}* закрыта по стопу{pnl_text} — перезаход (loss-SL включён)")
                # Не делаем delete_reentry, не делаем continue — падаем в общий
                # execute_open ниже. Но баланс-чек ниже всё равно может отменить.
            else:
                await _notify_all(app,
                    f"🛑 *{coin}* закрыта по стопу{pnl_text} — перезаход пропущен")
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
            # Сброс profit_locked флага: новая позиция начинает с чистым флагом.
            # averaging_job снова дождётся pnl_pct ≥ trigger и снова поднимет флаг
            # вместе с переустановкой SL в новую профит-зону.
            # Также синхронизируем runtime _profit_locked set (используется самим
            # averaging_job для пред-проверки `symbol not in _profit_locked`).
            db_mod.set_reentry_profit_locked(symbol, False)
            _profit_locked: set = app.bot_data.setdefault("_profit_locked", set())
            _profit_locked.discard(symbol)
            db_mod.log_trade(symbol, "reentry", amount=margin, note=f"cycle {new_cycle}")
            # Clear exhausted flag so new cycle gets fresh averaging tracking
            notified_exhausted: set = app.bot_data.setdefault("_avg_notified_exhausted", set())
            notified_exhausted.discard(symbol)
            # Различаем head в зависимости от того, был ли это profit-lock close.
            # Если флаг был поднят (was_profit_locked=True), показываем 🔒 и явно
            # говорим "по profit-lock SL" — это интуитивнее чем "в профит" когда
            # позиция закрылась по ползущему SL. Если flag не был поднят — обычный
            # формат TP/profitable-SL.
            if was_profit_locked:
                head = f"🔒 *{coin}* закрыта по profit-lock SL{pnl_text} → перезаход #{new_cycle}/{max_cycles}"
            else:
                head = f"✅ *{coin}* в профит{pnl_text} → перезаход #{new_cycle}/{max_cycles}"
            msg = (
                f"{head}\n"
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

        # Alert when free balance can't cover averaging budgets for all open positions
        try:
            from bot import db as db_mod
            n_pos = len(db_mod.get_open_positions())
            if n_pos > 0:
                config = app.bot_data.get("config")
                _avg_amount = float(getattr(config, "averaging_amount", 0.10)) if config else 0.10
                _max_cnt = int(getattr(config, "max_averaging_count", 100)) if config else 100
                _sl_pct = float(getattr(config, "sl_pct", 500)) if config else 500.0
                _profit_lock = float(getattr(config, "averaging_profit_lock_trigger", 0)) if config else 0
                avg_budget_eff = _max_cnt * _avg_amount
                _sl_mult = 1.0 if _profit_lock > 0 else (_sl_pct / 100.0)
                risk_per_pos = avg_budget_eff * _sl_mult
                total_needed = n_pos * risk_per_pos
                _margin_alert_ts = app.bot_data.get("_margin_alert_ts", 0)
                if free < total_needed and time.time() - _margin_alert_ts >= 120:
                    can_for = int(free / risk_per_pos) if risk_per_pos > 0 else 0
                    _sl_label = "profit-lock" if _profit_lock > 0 else f"SL {_sl_pct:.0f}%"
                    await _notify_all(app,
                        f"🚨 *Не хватает маржи для докупок!*\n"
                        f"Позиций: `{n_pos}` · нужно `${total_needed:.2f}` "
                        f"(по `${risk_per_pos:.2f}` на каждую, {_max_cnt}×${_avg_amount:.2f} × {_sl_label})\n"
                        f"Свободно `${free:.2f}` — хватит на `{can_for}` из `{n_pos}` поз\n"
                        f"Пополни баланс или закрой часть позиций")
                    app.bot_data["_margin_alert_ts"] = time.time()
        except Exception as _me:
            logger.debug("balance_alert_job margin check: %s", _me)

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
    # has no open position on the exchange (covers DB-missing cases too).
    exchange_symbols = {pos["symbol"] for pos in positions}
    db_open_symbols = {p["symbol"] for p in db_mod.get_open_positions()}

    # _delisted_orphans накапливается параллельно и используется ниже в DB sync.
    # Контракт-делистинг (1001) — терминальное состояние: ZEC/LAB могут крутиться
    # в orphan loop вечно если не помечать их закрытыми сразу.
    _delisted_orphans: set[str] = set()

    async def _cancel_orphan(symbol):
        async with sem:
            try:
                n = await client.cancel_tp_sl_orders(symbol)
                if n == client.CANCEL_DELISTED:
                    _delisted_orphans.add(symbol)
                elif n > 0:
                    logger.info("Cancelled %d orphaned plan orders for closed position %s", n, symbol)
            except Exception as e:
                logger.warning("Orphan order cleanup %s: %s", symbol, e)

    orphan_syms = db_open_symbols - exchange_symbols
    if orphan_syms:
        await asyncio.gather(*[_cancel_orphan(s) for s in orphan_syms])

    # Sync DB: close any position that's open in DB but gone from exchange.
    # Делистнутые контракты — закрываем БЕЗ оглядки на reentry: re-entry для
    # делистнутого фьючерса всё равно невозможен, и оставление reentry-записи
    # удерживает символ в orphan loop навсегда.
    reentry_symbols = {r["symbol"] for r in db_mod.get_all_reentry()}
    for symbol in orphan_syms:
        is_delisted = symbol in _delisted_orphans
        if symbol in reentry_symbols and not is_delisted:
            continue  # reentry_job will handle close + re-entry decision
        db_mod.close_position(symbol)
        db_mod.close_position_history(
            symbol, exit_price=0, pnl=0,
            close_reason="delisted" if is_delisted else "liquidated",
        )
        coin = symbol.split("/")[0]
        if is_delisted:
            db_mod.delete_reentry(symbol)
            logger.info("DB sync: %s delisted on MEXC, closed and reentry deleted", symbol)
            await _notify_all(app,
                f"⚠️ *{coin}* делистнут с MEXC — позиция и перезаход закрыты")
        else:
            logger.info("DB sync: closed stale open position %s (not on exchange)", symbol)
            await _notify_all(app,
                f"💀 *{coin}* закрыта принудительно (ликвидация или внешнее закрытие)")

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
    full_budget = base_budget * (sl_pct / 100.0)
    min_balance = full_budget * (1.0 - scan_risk_pct / 100.0)

    try:
        free_balance = await client.get_free_futures_balance()
    except Exception:
        free_balance = 0.0

    # Compute actual remaining risk for existing open positions (mirrors balance.py logic)
    _existing_risk = 0.0
    try:
        from bot import db as _db_scan
        _min_cache_scan = app.bot_data.get("_min_order_cache", {})
        for _ep in positions:
            _esym = _ep.get("symbol", "")
            _elev = int(_ep.get("leverage") or 1)
            _erec = _db_scan.get_open_position(_esym)
            _einv = float((_erec or {}).get("total_invested") or margin)
            _ecnt = int((_erec or {}).get("averaging_count") or 0)
            _erem = max(0, max_avg_count - _ecnt)
            _enotional = _min_cache_scan.get(_esym, 0)
            _eeff = (max(avg_amount, _enotional / max(_elev, 1) * 1.05)
                     if _enotional > 0 else avg_amount)
            _existing_risk += (_einv + _erem * _eeff) * (sl_pct / 100.0)
    except Exception:
        _existing_risk = 0.0
    _opened_risk = 0.0  # accumulates risk of positions opened in this scan run

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

        # Pre-fetch live min notional from MEXC (populate cache before skip/budget checks)
        try:
            _live_min = await client.get_min_order_usdt(fut_sym, leverage)
            if _live_min > 0:
                _live_notional = _live_min * max(leverage, 1)
                _ao_cache = app.bot_data.setdefault("_min_order_cache", {})
                if _live_notional > _ao_cache.get(fut_sym, 0):
                    _ao_cache[fut_sym] = _live_notional
                    try:
                        from bot import db as _db_ao
                        _db_ao.set_min_order_notional(fut_sym, _live_notional)
                    except Exception:
                        pass
        except Exception:
            pass

        # Per-symbol effective avg amount and budget
        _ao_notional = app.bot_data.get("_min_order_cache", {}).get(fut_sym, 0)
        _ao_eff_avg = (max(avg_amount, _ao_notional / max(leverage, 1) * 1.05)
                       if _ao_notional > 0 else avg_amount)
        _sym_base = max_avg_count * _ao_eff_avg + margin
        _sym_full = _sym_base * (sl_pct / 100.0)

        # Capital check: free balance must cover existing risk + new position full risk
        _total_risk = _existing_risk + _opened_risk + _sym_full
        _needed = _total_risk * (1.0 - scan_risk_pct / 100.0)
        if _needed > 0 and free_balance < _needed:
            skipped.append(f"{ticker}(мало депа)")
            _surplus = free_balance - _existing_risk - _opened_risk
            logger.info("AutoScan: skip %s — surplus $%.2f < needed $%.2f (existing $%.2f, new $%.2f, risk=%d%%)",
                        ticker, _surplus, _sym_full, _existing_risk + _opened_risk, _sym_full, int(scan_risk_pct))
            continue

        # Skip only if averaging is fundamentally impossible:
        # avg_amount * leverage must cover the MEXC minimum notional.
        # The 5% buffer is applied at order-placement time, not here.
        _cached_min_notional = app.bot_data.get("_min_order_cache", {}).get(fut_sym, 0)
        if _cached_min_notional > 0 and avg_amount * max(leverage, 1) < _cached_min_notional:
            _min_avg_margin = _cached_min_notional / max(leverage, 1) * 1.05
            skipped.append(f"{ticker}(мин докупка ${_min_avg_margin:.2f})")
            logger.info("AutoScan: skip %s — avg $%.2f x %d = $%.2f < min notional $%.1f",
                        ticker, avg_amount, leverage, avg_amount * leverage, _cached_min_notional)
            await _notify_all(app,
                f"🚫 *AutoScan* `{ticker}` — открытие пропущено\n"
                f"Мин. ордер MEXC `${_cached_min_notional:.0f}` при ×{leverage}: нужна маржа `${_min_avg_margin:.2f}`\n"
                f"Настройка `averaging_amount=${avg_amount:.2f}` недостаточна")
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
        _opened_risk += _sym_full

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

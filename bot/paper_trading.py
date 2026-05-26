"""Paper trading — virtual $500 portfolio, realistic MEXC futures simulation.

v2 improvements over v1:
- Mark price (not last) for all PnL/TP/SL/liq decisions
- Orderbook VWAP fill simulation on entry, averaging, and exit
- Liquidation price tracking (MEXC isolated margin formula)
- Funding fee accrual every 8h (real rates from MEXC API)
- Stepped profit-lock SL: ratchets every +50% PnL (mirrors real bot logic)
"""
import datetime
import logging

from bot import db as db_mod

logger = logging.getLogger(__name__)

# ── Fixed paper-only constants ────────────────────────────────────
PAPER_INITIAL_BALANCE = 500.0
PAPER_MAX_POSITIONS = 10
PAPER_MIN_SCORE = 30
MAINTENANCE_MARGIN_RATE = 0.005   # 0.5% — MEXC default for most perp pairs
FUNDING_INTERVAL_HOURS = 8        # MEXC settles funding every 8h
PROFIT_LOCK_STEP = 50             # ratchet SL every 50% PnL gain


def _paper_params(config) -> dict:
    """Extract live trading params from real bot config.

    Paper trading mirrors the real strategy so results are comparable.
    averaging_budget is not a Config dataclass field — derive it from
    max_averaging_count * averaging_amount as the real bot does.
    """
    margin = float(getattr(config, "default_trade_usdt", 1.0))
    avg_amount = float(getattr(config, "averaging_amount", 0.50))
    max_avg_count = int(getattr(config, "max_averaging_count", 20))
    # averaging_budget may exist as a dynamic DB key; fall back to derived value
    avg_budget = float(getattr(config, "averaging_budget",
                               max_avg_count * avg_amount))
    return {
        "margin":        margin,
        "avg_amount":    avg_amount,
        "avg_threshold": float(getattr(config, "averaging_threshold", -100.0)),
        "max_avg_count": max_avg_count,
        "avg_budget":    avg_budget,
        "tp_pct":        float(getattr(config, "tp_pct", 500.0)),
        "sl_pct":        float(getattr(config, "sl_pct", 500.0)),
        "leverage":      int(getattr(config, "default_leverage", 0) or 0),
        "required":      margin + avg_budget,
    }


# ── Math helpers ──────────────────────────────────────────────────

def calc_pnl_pct(entry_price: float, mark_price: float, leverage: int, side: str) -> float:
    if entry_price <= 0 or mark_price <= 0:
        return 0.0
    if side == "short":
        return (entry_price - mark_price) / entry_price * leverage * 100
    return (mark_price - entry_price) / entry_price * leverage * 100


def calc_new_entry(entry_price: float, total_invested: float,
                   add_price: float, add_amount: float) -> float:
    """VWAP-style weighted average entry after adding to position."""
    if entry_price <= 0 or add_price <= 0:
        return add_price
    old_qty = total_invested / entry_price
    new_qty = add_amount / add_price
    total_qty = old_qty + new_qty
    if total_qty == 0:
        return entry_price
    return (total_invested + add_amount) / total_qty


def _remaining_budget(pos: dict) -> float:
    return float(pos["averaging_budget"]) - (float(pos["total_invested"]) - float(pos["margin"]))


def _calc_liq_price(entry: float, leverage: int, side: str) -> float:
    """MEXC isolated margin liquidation price (simplified formula)."""
    if entry <= 0 or leverage <= 0:
        return 0.0
    mm = MAINTENANCE_MARGIN_RATE
    if side == "short":
        return entry * (1.0 + 1.0 / leverage - mm)
    return entry * (1.0 - 1.0 / leverage + mm)


# ── Market data helpers ───────────────────────────────────────────

async def _get_mark_price(client, symbol: str) -> float:
    """Fetch futures mark price; falls back to last trade price."""
    try:
        sym = client.futures_symbol(symbol)
        ticker = await client._exchange.fetch_ticker(sym)
        info = ticker.get("info") or {}
        mark = (float(info.get("markPrice") or 0) or
                float(ticker.get("mark") or 0) or
                float(ticker.get("last") or 0))
        return mark
    except Exception as e:
        logger.debug("_get_mark_price %s: %s", symbol, e)
        return 0.0


async def _simulate_fill(client, symbol: str, direction: str, notional_usdt: float) -> float:
    """Walk the MEXC orderbook for a realistic VWAP fill price.

    direction='buy'  -> walk asks (open long / close short).
    direction='sell' -> walk bids (open short / close long).
    Falls back to mark price on error or insufficient depth.
    """
    if notional_usdt <= 0:
        return await _get_mark_price(client, symbol)
    try:
        sym = client.futures_symbol(symbol)
        ob = await client._exchange.fetch_order_book(sym, limit=20)
        levels = ob["asks"] if direction == "buy" else ob["bids"]
        if not levels:
            return await _get_mark_price(client, symbol)

        remaining = notional_usdt
        cost = 0.0
        qty_total = 0.0

        for price, qty in levels:
            if price <= 0 or qty <= 0:
                continue
            level_notional = price * qty
            take = min(level_notional, remaining)
            take_qty = take / price
            cost += price * take_qty
            qty_total += take_qty
            remaining -= take
            if remaining <= 1e-8:
                break

        if qty_total <= 0:
            return await _get_mark_price(client, symbol)
        return cost / qty_total

    except Exception as e:
        logger.debug("_simulate_fill %s %s: %s", symbol, direction, e)
        return await _get_mark_price(client, symbol)


async def _calc_funding_fee(client, pos: dict) -> tuple[float, bool]:
    """Returns (funding_delta_usd, should_apply).

    funding_delta_usd > 0 = profit (e.g. short while rate > 0, longs pay us).
    should_apply = True only if FUNDING_INTERVAL_HOURS have elapsed.
    """
    last_ts = pos.get("last_funding_ts")
    baseline_str = last_ts or pos.get("created_at") or ""
    try:
        baseline_dt = datetime.datetime.fromisoformat(baseline_str)
    except Exception:
        return 0.0, False

    now = datetime.datetime.utcnow()
    if (now - baseline_dt.replace(tzinfo=None)).total_seconds() / 3600 < FUNDING_INTERVAL_HOURS:
        return 0.0, False

    symbol = pos["symbol"]
    side = pos["side"]
    total_invested = float(pos["total_invested"])
    leverage = int(pos["leverage"])
    notional = total_invested * leverage

    try:
        info = await client.get_funding_rate(symbol)
        rate = float(info.get("rate") or 0)
    except Exception:
        return 0.0, False

    # Short: rate > 0 -> longs pay us (+delta); rate < 0 -> we pay (-delta)
    # Long:  rate > 0 -> we pay (-delta); rate < 0 -> shorts pay us (+delta)
    delta = notional * rate * (1.0 if side == "short" else -1.0)
    return delta, True


# ── Jobs ──────────────────────────────────────────────────────────

async def paper_scan_job(app):
    """Every 30 min: scan for candidates and open paper positions."""
    from bot.ai.scanner import scan_overbought

    client = app.bot_data.get("exchange")
    config = app.bot_data.get("config")
    if not client or not config:
        return
    if not getattr(config, "paper_enabled", True):
        return

    db_mod.init_paper_account(PAPER_INITIAL_BALANCE)
    p = _paper_params(config)

    account = db_mod.get_paper_account()
    free = account["balance"]
    open_positions = db_mod.get_open_paper_positions()
    open_symbols = {p_["symbol"] for p_ in open_positions}

    slots_left = PAPER_MAX_POSITIONS - len(open_positions)
    if slots_left <= 0:
        logger.info("Paper scan: max %d positions reached", PAPER_MAX_POSITIONS)
        return
    if free < p["required"]:
        logger.info("Paper scan: insufficient balance %.2f", free)
        return

    try:
        candidates, total_scanned = await scan_overbought(client, 65.0, 10.0)
    except Exception as e:
        logger.error("Paper scan: scan_overbought failed: %s", e)
        return

    logger.info("Paper scan: %d candidates from %d symbols", len(candidates), total_scanned)

    entered = []
    for coin in candidates:
        if len(entered) >= slots_left or free < p["required"]:
            break

        symbol = coin["symbol"]
        if symbol in open_symbols or coin["score"] < PAPER_MIN_SCORE:
            continue

        side = coin["direction"]

        try:
            leverage = p["leverage"] or await client.get_max_leverage(symbol)
        except Exception:
            leverage = 10

        # Simulate orderbook fill for realistic entry price
        fill_dir = "sell" if side == "short" else "buy"
        entry_price = await _simulate_fill(client, symbol, fill_dir, p["margin"] * leverage)
        if entry_price <= 0:
            entry_price = float(coin["price"])
        if entry_price <= 0:
            continue

        liq_price = _calc_liq_price(entry_price, leverage, side)

        db_mod.open_paper_position(
            symbol=symbol, side=side, entry_price=entry_price,
            leverage=leverage, margin=p["margin"],
            avg_budget=p["avg_budget"],
            tp_pct=p["tp_pct"], sl_pct=p["sl_pct"],
            liq_price=liq_price,
        )
        db_mod.update_paper_balance(-p["required"])
        free -= p["required"]
        db_mod.log_paper_trade(
            symbol=symbol, action="open", side=side,
            entry_price=entry_price, margin=p["margin"],
            note=f"score={coin['score']} lev={leverage} liq={liq_price:.6g}",
        )
        open_symbols.add(symbol)
        entered.append({
            "symbol": symbol, "side": side, "score": coin["score"],
            "entry_price": entry_price, "leverage": leverage, "liq_price": liq_price,
        })
        logger.info("Paper: opened %s %s @ %.6g x%d liq=%.6g",
                    side, symbol, entry_price, leverage, liq_price)

    if not entered:
        return

    account = db_mod.get_paper_account()
    lines = [f"📄 *Бумага — {len(entered)} новых поз* (свободно `${account['balance']:.2f}`)"]
    for e in entered:
        c = e["symbol"].split("/")[0]
        icon = "🔻" if e["side"] == "short" else "🟢⬆️"
        lines.append(
            f"  {icon} `{c}` @ `{e['entry_price']:.6g}` x{e['leverage']}"
            f"  лик `{e['liq_price']:.6g}`  score {e['score']}"
        )

    text = "\n".join(lines)
    for uid in (config.allowed_user_ids or []):
        try:
            await app.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
        except Exception as ex:
            logger.debug("Paper notify %s: %s", uid, ex)


async def paper_update_job(app):
    """Every 15s: update all open paper positions.

    Order of checks per position:
    1. Mark price fetch
    2. Liquidation check (hard close, lose margin)
    3. Profit-lock SL ratchet + trigger
    4. TP
    5. Averaging (check before SL so budget depletes first)
    6. SL
    7. Funding accrual (every 8h)
    """
    client = app.bot_data.get("exchange")
    config = app.bot_data.get("config")
    if not client or not config:
        return
    if not getattr(config, "paper_enabled", True):
        return

    open_positions = db_mod.get_open_paper_positions()
    if not open_positions:
        return

    p = _paper_params(config)
    notifications: list[str] = []

    for pos in open_positions:
        symbol = pos["symbol"]
        side = pos["side"]
        entry_price = float(pos["entry_price"])
        leverage = int(pos["leverage"])
        total_invested = float(pos["total_invested"])
        avg_count = int(pos["averaging_count"])
        tp_pct = float(pos["tp_pct"])
        sl_pct = float(pos["sl_pct"])
        profit_lock_step = float(pos.get("profit_lock_step") or 0)
        funding_accrued = float(pos.get("funding_accrued") or 0)
        liq_price = float(pos.get("liquidation_price") or 0)
        pos_id = pos["id"]
        remaining = _remaining_budget(pos)
        coin = symbol.split("/")[0]
        icon = "🔻" if side == "short" else "🟢⬆️"

        # ── 1. Mark price ─────────────────────────────────────────
        mark_price = await _get_mark_price(client, symbol)
        if mark_price <= 0:
            continue

        # ── 2. PnL (price-based, same as MEXC uses for TP/SL triggers) ──
        pnl_pct = calc_pnl_pct(entry_price, mark_price, leverage, side)

        # ── 3. Liquidation check ──────────────────────────────────
        if liq_price > 0:
            liq_hit = ((side == "short" and mark_price >= liq_price) or
                       (side == "long" and mark_price <= liq_price))
            if liq_hit:
                realized_pnl = -total_invested  # total margin loss
                db_mod.close_paper_position(pos_id, liq_price, realized_pnl)
                db_mod.update_paper_balance(remaining)  # return unused avg budget only
                db_mod.log_paper_trade(
                    symbol=symbol, action="liq", side=side,
                    entry_price=entry_price, close_price=liq_price,
                    margin=total_invested, pnl=realized_pnl,
                    note=f"avg={avg_count}",
                )
                notifications.append(
                    f"💀 *Бумага ЛИК* {icon}`{coin}` @ `{liq_price:.6g}` "
                    f"= `-${total_invested:.2f}`"
                )
                continue

        # ── 4. Profit-lock SL ─────────────────────────────────────
        if pnl_pct >= 100:
            new_step = (int(pnl_pct) // PROFIT_LOCK_STEP) * PROFIT_LOCK_STEP
            if new_step > profit_lock_step:
                db_mod.update_paper_profit_lock(pos_id, float(new_step))
                old_step = profit_lock_step
                profit_lock_step = float(new_step)
                if old_step == 0:
                    notifications.append(
                        f"🔒 *Бумага лок* {icon}`{coin}` "
                        f"PnL `{pnl_pct:+.1f}%` -> SL в `+{new_step - PROFIT_LOCK_STEP:.0f}%`"
                    )

        if profit_lock_step > 0:
            lock_floor = profit_lock_step - PROFIT_LOCK_STEP
            if pnl_pct < lock_floor:
                exit_dir = "buy" if side == "short" else "sell"
                exit_price = await _simulate_fill(
                    client, symbol, exit_dir, total_invested * leverage)
                close_pnl_pct = calc_pnl_pct(entry_price, exit_price, leverage, side)
                realized_pnl = total_invested * close_pnl_pct / 100 + funding_accrued
                db_mod.close_paper_position(pos_id, exit_price, realized_pnl)
                db_mod.update_paper_balance(total_invested + remaining + realized_pnl)
                db_mod.log_paper_trade(
                    symbol=symbol, action="lock_sl", side=side,
                    entry_price=entry_price, close_price=exit_price,
                    margin=total_invested, pnl=realized_pnl,
                    note=f"lock={lock_floor:.0f}% pnl={pnl_pct:.1f}% avg={avg_count}",
                )
                notifications.append(
                    f"🔒✅ *Бумага лок-SL* {icon}`{coin}` "
                    f"`{pnl_pct:+.1f}%` = `+${realized_pnl:.2f}`"
                )
                continue

        # ── 5. TP ─────────────────────────────────────────────────
        if pnl_pct >= tp_pct:
            exit_dir = "buy" if side == "short" else "sell"
            exit_price = await _simulate_fill(
                client, symbol, exit_dir, total_invested * leverage)
            close_pnl_pct = calc_pnl_pct(entry_price, exit_price, leverage, side)
            realized_pnl = total_invested * close_pnl_pct / 100 + funding_accrued
            db_mod.close_paper_position(pos_id, exit_price, realized_pnl)
            db_mod.update_paper_balance(total_invested + remaining + realized_pnl)
            db_mod.log_paper_trade(
                symbol=symbol, action="tp", side=side,
                entry_price=entry_price, close_price=exit_price,
                margin=total_invested, pnl=realized_pnl,
                note=f"avg={avg_count} funding={funding_accrued:.3f}",
            )
            notifications.append(
                f"✅ *Бумага TP* {icon}`{coin}` +{pnl_pct:.0f}% = `+${realized_pnl:.2f}`"
            )
            continue

        # ── 6. Averaging ──────────────────────────────────────────
        can_avg = (pnl_pct <= p["avg_threshold"]
                   and avg_count < p["max_avg_count"]
                   and remaining >= p["avg_amount"])
        if can_avg:
            avg_dir = "sell" if side == "short" else "buy"
            fill_price = await _simulate_fill(
                client, symbol, avg_dir, p["avg_amount"] * leverage)
            if fill_price <= 0:
                fill_price = mark_price
            new_entry = calc_new_entry(entry_price, total_invested, fill_price, p["avg_amount"])
            new_total = total_invested + p["avg_amount"]
            new_liq = _calc_liq_price(new_entry, leverage, side)
            db_mod.update_paper_averaging(pos_id, new_total, avg_count + 1, new_entry)
            db_mod.update_paper_liq_price(pos_id, new_liq)
            db_mod.log_paper_trade(
                symbol=symbol, action="avg", side=side,
                entry_price=new_entry, close_price=fill_price,
                margin=p["avg_amount"],
                note=f"#{avg_count + 1} fill={fill_price:.6g} liq={new_liq:.6g}",
            )
            logger.debug("Paper avg %s #%d @ %.6g (pnl=%.1f%%)",
                         coin, avg_count + 1, fill_price, pnl_pct)
            continue

        # ── 7. SL ─────────────────────────────────────────────────
        if pnl_pct <= -sl_pct:
            exit_dir = "buy" if side == "short" else "sell"
            exit_price = await _simulate_fill(
                client, symbol, exit_dir, total_invested * leverage)
            close_pnl_pct = calc_pnl_pct(entry_price, exit_price, leverage, side)
            realized_pnl = total_invested * close_pnl_pct / 100 + funding_accrued
            db_mod.close_paper_position(pos_id, exit_price, realized_pnl)
            db_mod.update_paper_balance(total_invested + remaining + realized_pnl)
            db_mod.log_paper_trade(
                symbol=symbol, action="sl", side=side,
                entry_price=entry_price, close_price=exit_price,
                margin=total_invested, pnl=realized_pnl,
                note=f"avg={avg_count} funding={funding_accrued:.3f}",
            )
            notifications.append(
                f"❌ *Бумага SL* {icon}`{coin}` {pnl_pct:.0f}% = `${realized_pnl:.2f}`"
            )
            continue

        # ── 8. Funding accrual (deferred, every 8h) ───────────────
        funding_delta, should_apply = await _calc_funding_fee(client, pos)
        if should_apply:
            ts_now = datetime.datetime.utcnow().isoformat()
            db_mod.update_paper_funding(pos_id, funding_delta, ts_now)
            if abs(funding_delta) >= 0.001:
                sign = "+" if funding_delta >= 0 else ""
                logger.info("Paper funding %s: %s%.4f USD", coin, sign, funding_delta)

    if notifications:
        account = db_mod.get_paper_account()
        text = "\n".join(notifications) + f"\n\nБаланс: `${account['balance']:.2f}`"
        for uid in (config.allowed_user_ids or []):
            try:
                await app.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
            except Exception as ex:
                logger.debug("Paper notify %s: %s", uid, ex)


async def paper_signal_job(app):
    """Every 5 min: fast signal scan — RSI + orderbook + funding.

    Fires a paper open when all three align, without waiting for the 30-min cron.
    Stricter thresholds than paper_scan_job to avoid duplicate opens.
    source='signal' in DB to distinguish from cron-based opens.
    """
    from bot.ai.scanner import scan_overbought

    client = app.bot_data.get("exchange")
    config = app.bot_data.get("config")
    if not client or not config:
        return
    if not getattr(config, "paper_enabled", True):
        return

    db_mod.init_paper_account(PAPER_INITIAL_BALANCE)
    p = _paper_params(config)
    account = db_mod.get_paper_account()
    free = account["balance"]
    open_positions = db_mod.get_open_paper_positions()
    open_symbols = {p_["symbol"] for p_ in open_positions}

    slots_left = PAPER_MAX_POSITIONS - len(open_positions)
    if slots_left <= 0 or free < p["required"]:
        return

    # Stricter RSI/change thresholds than 30-min cron to avoid redundant opens
    try:
        candidates, _ = await scan_overbought(client, rsi_threshold=70.0,
                                              daily_change_threshold=15.0,
                                              max_symbols=30)
    except Exception as e:
        logger.debug("Paper signal scan: %s", e)
        return

    entered = []
    ratio = 1.0
    for coin in candidates:
        if len(entered) >= slots_left or free < p["required"]:
            break

        symbol = coin["symbol"]
        if symbol in open_symbols:
            continue
        if coin["score"] < PAPER_MIN_SCORE + 10:  # tighter than cron scan
            continue

        side = coin["direction"]

        # ── Orderbook gate: asks must clearly outweigh bids ──────
        try:
            sym = client.futures_symbol(symbol)
            ob = await client._exchange.fetch_order_book(sym, limit=10)
            bid_usdt = sum(px * q for px, q in (ob.get("bids") or [])[:5])
            ask_usdt = sum(px * q for px, q in (ob.get("asks") or [])[:5])
            ratio = bid_usdt / ask_usdt if ask_usdt > 0 else 1.0
            if side == "short" and ratio > 0.5:
                logger.debug("Paper signal %s: ob ratio %.2f not bearish, skip", symbol, ratio)
                continue
            if side == "long" and ratio < 2.0:
                logger.debug("Paper signal %s: ob ratio %.2f not bullish, skip", symbol, ratio)
                continue
        except Exception:
            pass  # no orderbook = proceed on RSI alone

        # ── Funding gate: skip if paying too much ─────────────────
        try:
            info = await client.get_funding_rate(symbol)
            rate = float(info.get("rate") or 0)
            if side == "short" and rate < -0.0003:
                logger.debug("Paper signal %s: funding %.4f%% negative, skip", symbol, rate * 100)
                continue
        except Exception:
            pass

        try:
            leverage = p["leverage"] or await client.get_max_leverage(symbol)
        except Exception:
            leverage = 10

        fill_dir = "sell" if side == "short" else "buy"
        entry_price = await _simulate_fill(client, symbol, fill_dir, p["margin"] * leverage)
        if entry_price <= 0:
            entry_price = float(coin["price"])
        if entry_price <= 0:
            continue

        liq_price = _calc_liq_price(entry_price, leverage, side)

        db_mod.open_paper_position(
            symbol=symbol, side=side, entry_price=entry_price,
            leverage=leverage, margin=p["margin"],
            avg_budget=p["avg_budget"],
            tp_pct=p["tp_pct"], sl_pct=p["sl_pct"],
            liq_price=liq_price, source="signal",
        )
        db_mod.update_paper_balance(-p["required"])
        free -= p["required"]
        db_mod.log_paper_trade(
            symbol=symbol, action="open", side=side,
            entry_price=entry_price, margin=p["margin"],
            note=f"signal score={coin['score']} lev={leverage} ob_ratio={ratio:.2f}",
        )
        open_symbols.add(symbol)
        coin["entry_price"] = entry_price
        coin["leverage"] = leverage
        coin["liq_price"] = liq_price
        coin["ob_ratio"] = ratio if isinstance(ratio, float) else 0.0
        entered.append(coin)
        logger.info("Paper signal: opened %s %s @ %.6g ×%d score=%d",
                    side, symbol, entry_price, leverage, coin["score"])

    if not entered:
        return

    account = db_mod.get_paper_account()
    lines = [f"📡 *Бумага сигнал — {len(entered)} поз* (свободно `${account['balance']:.2f}`)"]
    for e in entered:
        c = e["symbol"].split("/")[0]
        icon = "🔻" if e["side"] == "short" else "🟢⬆️"
        ob_str = f" ob={e['ob_ratio']:.2f}" if e.get("ob_ratio") else ""
        lines.append(
            f"  {icon} `{c}` @ `{e['entry_price']:.6g}` ×{e['leverage']}"
            f"  лик `{e['liq_price']:.6g}`  score {e['score']}{ob_str}"
        )

    text = "\n".join(lines)
    for uid in (config.allowed_user_ids or []):
        try:
            await app.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
        except Exception as ex:
            logger.debug("Paper signal notify %s: %s", uid, ex)

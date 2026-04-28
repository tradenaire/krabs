"""Paper trading — virtual $500 portfolio, automated scanning + management."""
import logging
from bot import db as db_mod

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────
PAPER_INITIAL_BALANCE = 500.0
PAPER_MARGIN = 1.0           # per position
PAPER_AVG_AMOUNT = 0.5       # per averaging step
PAPER_MAX_AVG_COUNT = 20     # max averaging steps per position
PAPER_AVG_BUDGET = PAPER_MAX_AVG_COUNT * PAPER_AVG_AMOUNT   # derived: 20 × $0.5 = $10
PAPER_AVG_THRESHOLD_PCT = -100.0
PAPER_TP_PCT = 500.0
PAPER_SL_PCT = 500.0
PAPER_MAX_POSITIONS = 10
PAPER_MIN_SCORE = 30         # minimum scanner score to enter
PAPER_REQUIRED_PER_POS = PAPER_MARGIN + PAPER_AVG_BUDGET    # $1 + $10 = $11


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
    margin = float(pos["margin"])
    total = float(pos["total_invested"])
    budget = float(pos["averaging_budget"])
    return budget - (total - margin)


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

    account = db_mod.get_paper_account()
    free = account["balance"]
    open_positions = db_mod.get_open_paper_positions()
    open_symbols = {p["symbol"] for p in open_positions}

    slots_left = PAPER_MAX_POSITIONS - len(open_positions)
    if slots_left <= 0:
        logger.info("Paper scan: max %d positions reached", PAPER_MAX_POSITIONS)
        return
    if free < PAPER_REQUIRED_PER_POS:
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
        if len(entered) >= slots_left:
            break
        if free < PAPER_REQUIRED_PER_POS:
            break

        symbol = coin["symbol"]
        if symbol in open_symbols:
            continue
        if coin["score"] < PAPER_MIN_SCORE:
            continue

        side = coin["direction"]
        entry_price = float(coin["price"])
        if entry_price <= 0:
            continue

        try:
            leverage = await client.get_max_leverage(symbol)
        except Exception:
            leverage = 10

        pos_id = db_mod.open_paper_position(
            symbol=symbol, side=side, entry_price=entry_price,
            leverage=leverage, margin=PAPER_MARGIN,
            avg_budget=PAPER_AVG_BUDGET,
            tp_pct=PAPER_TP_PCT, sl_pct=PAPER_SL_PCT,
        )
        db_mod.update_paper_balance(-PAPER_REQUIRED_PER_POS)
        free -= PAPER_REQUIRED_PER_POS
        db_mod.log_paper_trade(
            symbol=symbol, action="open", side=side,
            entry_price=entry_price, margin=PAPER_MARGIN,
            note=f"score={coin['score']} lev={leverage}",
        )
        open_symbols.add(symbol)
        entered.append({
            "symbol": symbol, "side": side,
            "entry_price": entry_price, "leverage": leverage,
            "score": coin["score"],
        })
        logger.info("Paper: opened %s %s @ %.6g x%d", side, symbol, entry_price, leverage)

    if not entered:
        return

    account = db_mod.get_paper_account()
    lines = [f"📄 *Бумага — {len(entered)} новых поз* (свободно `${account['balance']:.2f}`)"]
    for e in entered:
        c = e["symbol"].split("/")[0]
        icon = "🔻" if e["side"] == "short" else "🟢⬆️"
        lines.append(f"  {icon} `{c}` @ `{e['entry_price']:.6g}` ×{e['leverage']}  score {e['score']}")

    text = "\n".join(lines)
    for uid in (config.allowed_user_ids or []):
        try:
            await app.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
        except Exception as ex:
            logger.debug("Paper notify %s: %s", uid, ex)


async def paper_update_job(app):
    """Every 5 min: check TP/SL/averaging for all open paper positions."""
    client = app.bot_data.get("exchange")
    config = app.bot_data.get("config")
    if not client or not config:
        return
    if not getattr(config, "paper_enabled", True):
        return

    open_positions = db_mod.get_open_paper_positions()
    if not open_positions:
        return

    notifications = []

    for pos in open_positions:
        symbol = pos["symbol"]
        side = pos["side"]
        entry_price = float(pos["entry_price"])
        leverage = int(pos["leverage"])
        total_invested = float(pos["total_invested"])
        avg_count = int(pos["averaging_count"])
        tp_pct = float(pos["tp_pct"])
        sl_pct = float(pos["sl_pct"])
        pos_id = pos["id"]
        remaining = _remaining_budget(pos)
        coin = symbol.split("/")[0]

        try:
            ticker = await client._exchange.fetch_ticker(symbol)
            mark_price = float(ticker.get("last", 0) or 0)
        except Exception as e:
            logger.debug("Paper update price %s: %s", symbol, e)
            continue

        if mark_price <= 0:
            continue

        pnl_pct = calc_pnl_pct(entry_price, mark_price, leverage, side)
        pnl_usd = total_invested * pnl_pct / 100
        icon = "🔻" if side == "short" else "🟢⬆️"

        # TP
        if pnl_pct >= tp_pct:
            db_mod.close_paper_position(pos_id, mark_price, pnl_usd)
            db_mod.update_paper_balance(total_invested + remaining + pnl_usd)
            db_mod.log_paper_trade(
                symbol=symbol, action="tp", side=side,
                entry_price=entry_price, close_price=mark_price,
                margin=total_invested, pnl=pnl_usd,
                note=f"avg={avg_count}",
            )
            notifications.append(
                f"✅ *Бумага TP* {icon}`{coin}` +{pnl_pct:.0f}% = `+${pnl_usd:.2f}`"
            )
            continue

        # Averaging (check before SL so budget is used before firing stop)
        max_avg = PAPER_MAX_AVG_COUNT
        can_avg = (pnl_pct <= PAPER_AVG_THRESHOLD_PCT
                   and avg_count < max_avg
                   and remaining >= PAPER_AVG_AMOUNT)
        if can_avg:
            new_entry = calc_new_entry(entry_price, total_invested, mark_price, PAPER_AVG_AMOUNT)
            new_total = total_invested + PAPER_AVG_AMOUNT
            db_mod.update_paper_averaging(pos_id, new_total, avg_count + 1, new_entry)
            db_mod.log_paper_trade(
                symbol=symbol, action="avg", side=side,
                entry_price=new_entry, close_price=mark_price,
                margin=PAPER_AVG_AMOUNT,
                note=f"#{avg_count + 1}",
            )
            logger.debug("Paper avg %s #%d @ %.6g (pnl=%.1f%%)",
                         coin, avg_count + 1, mark_price, pnl_pct)
            continue

        # SL (only when averaging is exhausted or not triggered)
        if pnl_pct <= -sl_pct:
            db_mod.close_paper_position(pos_id, mark_price, pnl_usd)
            db_mod.update_paper_balance(total_invested + remaining + pnl_usd)
            db_mod.log_paper_trade(
                symbol=symbol, action="sl", side=side,
                entry_price=entry_price, close_price=mark_price,
                margin=total_invested, pnl=pnl_usd,
                note=f"avg={avg_count}",
            )
            notifications.append(
                f"❌ *Бумага SL* {icon}`{coin}` {pnl_pct:.0f}% = `${pnl_usd:.2f}`"
            )
            continue

    if notifications:
        account = db_mod.get_paper_account()
        text = "\n".join(notifications) + f"\n\nБаланс: `${account['balance']:.2f}`"
        for uid in (config.allowed_user_ids or []):
            try:
                await app.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
            except Exception as ex:
                logger.debug("Paper notify %s: %s", uid, ex)

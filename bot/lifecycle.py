"""Position identity, close accounting and explicit adoption."""
import datetime as dt
import logging
import os
from bot import db
from bot.event_logger import log_event

logger = logging.getLogger(__name__)


async def authorize_update(update, context):
    from telegram.ext import ApplicationHandlerStop
    config = context.bot_data.get("config")
    user = update.effective_user
    if not config or not user:
        raise ApplicationHandlerStop
    diagnostic_bot_id = os.getenv("KRABS_DIAGNOSTIC_BOT_ID")
    if diagnostic_bot_id and str(user.id) == diagnostic_bot_id:
        message = getattr(update, "message", None)
        chat = getattr(message, "chat", None)
        text = getattr(message, "text", None)
        entities = getattr(message, "entities", ()) or ()
        if (getattr(user, "is_bot", False)
                and getattr(chat, "type", None) == "private"
                and text in {"/positions", "/balance"}
                and any(getattr(entity, "type", None) == "bot_command"
                        and getattr(entity, "offset", None) == 0
                        and getattr(entity, "length", None) == len(text)
                        for entity in entities)):
            return
        raise ApplicationHandlerStop
    if user.id not in config.allowed_user_ids:
        raise ApplicationHandlerStop


def register_position(pos, config, *, tp_pct=None, sl_pct=None):
    if not pos.get("position_id") or not pos.get("opened_at_ms"):
        raise ValueError("Exchange position ID and creation time are required")
    if pos.get("settle_currency", "USDT") != "USDT":
        raise ValueError("Management of non-USDT settled contracts is not supported; balances remain visible")
    existing = db.get_managed_position(pos)
    if existing:
        return existing
    tp = config.tp_pct if tp_pct is None else tp_pct
    sl = config.sl_pct if sl_pct is None else sl_pct
    key = db.upsert_position(pos["symbol"], pos["side"], pos["entry_price"], pos["leverage"],
        pos["margin"], tp, sl, config.averaging_amount * config.max_averaging_count,
        exchange_position_id=str(pos["position_id"]), opened_at_ms=int(pos["opened_at_ms"]))
    db.open_position_history(pos["symbol"], pos["side"], pos["leverage"], pos["entry_price"],
        pos["margin"], tp, sl, avg_threshold=config.averaging_threshold, avg_amount=config.averaging_amount,
        avg_budget=config.averaging_amount * config.max_averaging_count,
        avg_max_count=config.max_averaging_count, avg_interval=config.averaging_interval, position_key=key)
    return db.get_position_by_id(key)


def reset_runtime(app, symbol):
    for name in ("_avg_last_ts", "_expected_contracts", "_profit_lock_step", "_age_12h_notified",
                 "_contracts_warned", "_avg_notified_exhausted", "_was_profit_locked", "_avg_synth", "tp_sl_pcts"):
        value = app.bot_data.get(name)
        if isinstance(value, dict):
            value.pop(symbol, None)
        elif isinstance(value, set):
            value.discard(symbol)


def pnl_text(closure):
    if not closure or closure.get("pnl") is None:
        return "PnL: неизвестен (ожидается история биржи)"
    return f"PnL: `{closure['pnl']:+.4f} USDT` (realised MEXC)"


CLOSE_LABELS = {"tp": "по TP", "sl": "по SL", "profit_lock": "по profit-lock SL",
                "manual": "по команде закрытия", "manual_reentry": "по команде закрытия с перезаходом",
                "emergency": "аварийным сокращением", "liquidation": "ликвидация (системный ордер)",
                "adl": "ADL", "unknown": "причина неизвестна"}


async def reconcile_closures(app):
    client = app.bot_data["exchange"]
    live = await client.get_positions()
    ids = {str(p["position_id"]) for p in live}
    with db._connect() as conn:
        records = [dict(r) for r in conn.execute(
            "SELECT p.* FROM positions p LEFT JOIN closures c ON c.position_key=p.id "
            "WHERE p.exchange_position_id IS NOT NULL AND "
            "(p.status IN ('open','closing') OR c.pnl IS NULL OR c.reason='unknown')")]
    for record in records:
        if record["exchange_position_id"] in ids:
            continue
        try:
            result = await client.get_closed_position_result(record)
            if result is not None:
                db.record_closure(record, result)
                log_event("closure_reconciled", position_key=record["id"],
                          exchange_position_id=record["exchange_position_id"], result=result)
        except Exception as error:
            logger.warning("Close history position %s: %s", record["exchange_position_id"], error)
    # Claim before sending: an uncertain Telegram response must not cause duplicate notifications.
    with db._connect() as conn:
        pending = [dict(r) for r in conn.execute(
            "SELECT c.*,p.symbol FROM closures c JOIN positions p ON p.id=c.position_key WHERE c.notified=0")]
    from bot.jobs.main import _notify_all
    for closure in pending:
        with db._connect() as conn:
            claimed = conn.execute("UPDATE closures SET notified=1 WHERE position_key=? AND notified=0",
                                   (closure["position_key"],)).rowcount
        if claimed:
            await _notify_all(app, f"*{closure['symbol'].split('/')[0]}* закрыта: "
                f"{CLOSE_LABELS.get(closure['reason'], 'причина неизвестна')}\n{pnl_text(closure)}\n"
                f"Сделка #{closure['position_key']}")
    return live


async def adopt_handler(update, context):
    args = context.args or []
    if not args:
        await update.message.reply_text("/adopt SYMBOL — показать позицию для принятия под управление")
        return
    client = context.bot_data["exchange"]
    pos = await client.get_position(args[0])
    if not pos:
        await update.message.reply_text("Позиция не найдена")
        return
    if len(args) != 3 or args[1] != "confirm" or args[2] != str(pos["position_id"]):
        await update.message.reply_text(
            f"{pos['symbol']} / {pos['side']}: {pos['contracts']} контрактов. "
            "Принятие разрешит TP/SL, profit-lock, докупки и аварийное сокращение.\n"
            f"Подтверждение: /adopt {args[0]} confirm {pos['position_id']}")
        return
    register_position(pos, context.bot_data["config"])
    reset_runtime(context.application, pos["symbol"])
    db.delete_reentry(pos["symbol"])
    await update.message.reply_text(f"Принята позиция {pos['position_id']}. Перезаход выключен.")

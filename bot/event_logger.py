"""Structured JSONL event logging for audit/debug traces."""
from __future__ import annotations

import contextvars
import inspect
import json
import logging
import traceback
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOG_DIR = Path(__file__).parent.parent / "data" / "logs"
_CORRELATION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "krabs_correlation_id", default=None
)

_SECRET_KEYS = (
    "token", "secret", "api_key", "apikey", "authorization", "cookie",
    "password", "mexc_secret", "telegram_token", "openrouter_api_key",
)


def current_correlation_id() -> str:
    cid = _CORRELATION_ID.get()
    if not cid:
        cid = uuid.uuid4().hex
        _CORRELATION_ID.set(cid)
    return cid


def set_correlation_id(value: str | None = None) -> str:
    cid = value or uuid.uuid4().hex
    _CORRELATION_ID.set(cid)
    return cid


def _mask_string(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def sanitize(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "<max-depth>"
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if any(s in key.lower() for s in _SECRET_KEYS):
                clean[key] = _mask_string(str(v)) if v is not None else None
            else:
                clean[key] = sanitize(v, depth + 1)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [sanitize(v, depth + 1) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return sanitize(value.to_dict(), depth + 1)
    except Exception:
        return repr(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return repr(value)


def log_event(stream: str, event: str, **fields: Any) -> None:
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "correlation_id": current_correlation_id(),
            "event": event,
            **sanitize(fields),
        }
        line = json.dumps(record, ensure_ascii=False, default=_json_default)
        (_LOG_DIR / f"{stream}-{day}.jsonl").open("a", encoding="utf-8").write(line + "\n")
    except Exception as e:
        logger.debug("structured log skipped: %s", e)


def log_exception(stream: str, event: str, exc: BaseException, **fields: Any) -> None:
    log_event(
        stream,
        event,
        exception_type=type(exc).__name__,
        exception=str(exc),
        traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        **fields,
    )


def update_payload(update: Any) -> dict[str, Any]:
    try:
        raw = update.to_dict()
    except Exception:
        raw = repr(update)
    effective_user = getattr(update, "effective_user", None)
    effective_chat = getattr(update, "effective_chat", None)
    message = getattr(update, "effective_message", None)
    callback_query = getattr(update, "callback_query", None)
    return {
        "update_id": getattr(update, "update_id", None),
        "chat_id": getattr(effective_chat, "id", None),
        "user_id": getattr(effective_user, "id", None),
        "username": getattr(effective_user, "username", None),
        "message_id": getattr(message, "message_id", None),
        "text": getattr(message, "text", None),
        "callback_data": getattr(callback_query, "data", None),
        "raw": raw,
    }


async def telegram_update_logger(update: Any, context: Any) -> None:
    cid = set_correlation_id(f"tg-{getattr(update, 'update_id', uuid.uuid4().hex)}")
    log_event("telegram", "telegram_update", correlation_id=cid, **update_payload(update))


async def telegram_error_logger(update: object, context: Any) -> None:
    exc = getattr(context, "error", None)
    if exc:
        log_exception("errors", "telegram_error", exc, update=update_payload(update) if update else None)


def patch_bot_logging(bot: Any) -> None:
    for name in ("send_message", "edit_message_text", "delete_message", "answer_callback_query"):
        original = getattr(bot, name, None)
        if original is None or getattr(original, "_krabs_logged", False):
            continue

        @wraps(original)
        async def wrapper(*args: Any, __name: str = name, __original: Any = original, **kwargs: Any) -> Any:
            log_event("telegram", "telegram_outgoing_call", method=__name, args=args, kwargs=kwargs)
            try:
                result = __original(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                log_event("telegram", "telegram_outgoing_result", method=__name, result=result)
                return result
            except Exception as e:
                log_exception("telegram", "telegram_outgoing_error", e, method=__name, args=args, kwargs=kwargs)
                raise

        setattr(wrapper, "_krabs_logged", True)
        try:
            setattr(bot, name, wrapper)
            if getattr(getattr(bot, name, None), "_krabs_logged", False):
                continue
        except Exception as e:
            logger.debug("patch bot.%s skipped: %s", name, e)

        cls = bot.__class__
        class_original = getattr(cls, name, None)
        if class_original is None or getattr(class_original, "_krabs_logged", False):
            continue

        @wraps(class_original)
        async def class_wrapper(self: Any, *args: Any, __name: str = name,
                                __original: Any = class_original, **kwargs: Any) -> Any:
            log_event("telegram", "telegram_outgoing_call", method=__name, args=args, kwargs=kwargs)
            try:
                result = __original(self, *args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                log_event("telegram", "telegram_outgoing_result", method=__name, result=result)
                return result
            except Exception as e:
                log_exception("telegram", "telegram_outgoing_error", e, method=__name, args=args, kwargs=kwargs)
                raise

        setattr(class_wrapper, "_krabs_logged", True)
        try:
            setattr(cls, name, class_wrapper)
        except Exception as e:
            logger.debug("patch %s.%s skipped: %s", cls.__name__, name, e)


async def snapshot_exchange_state(client: Any, label: str, symbol: str | None = None,
                                  stream: str = "exchange", **fields: Any) -> None:
    snapshot: dict[str, Any] = {"label": label, "symbol": symbol, **fields}
    try:
        snapshot["futures_balance"] = await client.get_futures_balance()
    except Exception as e:
        snapshot["futures_balance_error"] = str(e)
    try:
        positions = await client.get_positions()
        snapshot["positions"] = positions
    except Exception as e:
        snapshot["positions_error"] = str(e)
    try:
        snapshot["tp_sl_orders"] = await client.get_tp_sl_orders(symbol) if symbol else await client.get_tp_sl_orders()
    except Exception as e:
        snapshot["tp_sl_orders_error"] = str(e)
    log_event(stream, "exchange_snapshot", **snapshot)

"""Bounded JSONL audit, adapted from tradenaire/krabs e1f7a76."""
import contextvars
import datetime as dt
import json
import logging
from logging.handlers import RotatingFileHandler
import re
import uuid

from bot import db

logger = logging.getLogger("krabs.audit")
logger.addHandler(logging.NullHandler())
logger.propagate = False
correlation_id = contextvars.ContextVar("krabs_event_id", default=None)
_secrets = ()
_secret_key = re.compile(r"token|secret|api.?key|authorization|cookie|password", re.I)


def sanitize(value, depth=0):
    if depth > 8:
        return "[truncated]"
    if isinstance(value, dict):
        return {str(k): "[redacted]" if _secret_key.search(str(k)) else sanitize(v, depth + 1)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, depth + 1) for v in value[:100]]
    if isinstance(value, str):
        for secret in _secrets:
            value = value.replace(secret, "[redacted]")
        value = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b|\bsk-[A-Za-z0-9_-]+", "[redacted]", value)
        return value[:2000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return type(value).__name__


def configure_audit(config):
    global _secrets
    _secrets = tuple(sorted((str(v) for k, v in vars(config).items()
                            if _secret_key.search(k) and v), key=len, reverse=True))
    path = db.DB_PATH.parent / "logs"
    path.mkdir(parents=True, exist_ok=True)
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    handler = RotatingFileHandler(path / "audit.jsonl", maxBytes=2_000_000,
                                  backupCount=4, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def log_event(event, **fields):
    logger.info(json.dumps({"ts": dt.datetime.now(dt.UTC).isoformat(),
        "event": event, "correlation_id": correlation_id.get() or uuid.uuid4().hex,
        **sanitize(fields)}, ensure_ascii=False, default=str))


async def telegram_update_logger(update, context):
    correlation_id.set(f"tg-{update.update_id}")
    # Command bodies can contain /setkey credentials; only metadata is audited.
    log_event("telegram_update", update_id=update.update_id,
              user_id=update.effective_user.id if update.effective_user else None)


async def telegram_error_logger(update, context):
    log_event("telegram_error", error_type=type(context.error).__name__)

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProtectionAudit:
    symbol: str
    status: str
    tp_count: int
    sl_count: int
    needs_repair: bool


def _norm_side(side: str) -> str:
    return "short" if side in ("short", "sell") else "long"


def _trigger_types(side: str) -> tuple[int, int]:
    return (1, 2) if _norm_side(side) == "long" else (2, 1)


def classify_protection(pos: dict, orders: list[dict] | None,
                        db_records: list[dict] | None = None) -> ProtectionAudit:
    symbol = str(pos.get("symbol") or "")
    db_records = list(db_records or [])
    tp_type, sl_type = _trigger_types(str(pos.get("side") or ""))
    related = [
        order for order in (orders or [])
        if str(order.get("symbol") or symbol) == symbol
    ]
    tp_count = len({
        float(order.get("trigger_price") or 0)
        for order in related
        if int(order.get("trigger_type") or 0) == tp_type
        and float(order.get("trigger_price") or 0) > 0
    })
    sl_count = len({
        float(order.get("trigger_price") or 0)
        for order in related
        if int(order.get("trigger_type") or 0) == sl_type
        and float(order.get("trigger_price") or 0) > 0
    })
    if not db_records:
        return ProtectionAudit(symbol, "DB_MISSING", tp_count, sl_count, True)
    if len(db_records) > 1:
        return ProtectionAudit(symbol, "DB_DUPLICATE", tp_count, sl_count, True)
    if tp_count >= 3 and sl_count >= 1:
        return ProtectionAudit(symbol, "OK_3TP_1SL", tp_count, sl_count, False)
    if tp_count == 1 and sl_count >= 1:
        return ProtectionAudit(symbol, "OK_1TP_1SL", tp_count, sl_count, False)
    if tp_count == 0 and sl_count == 0:
        return ProtectionAudit(symbol, "MISSING_ALL", tp_count, sl_count, True)
    return ProtectionAudit(symbol, "PARTIAL", tp_count, sl_count, True)


def protection_summary_line(audit: ProtectionAudit) -> str:
    if audit.status == "OK_3TP_1SL":
        return f"✅ TP/SL на бирже: {audit.tp_count} TP / {audit.sl_count} SL"
    if audit.status == "OK_1TP_1SL":
        return f"⚠️ TP/SL на бирже: single mode: {audit.tp_count} TP / {audit.sl_count} SL"
    if audit.status == "DB_MISSING":
        return "⚠️ TP/SL: позиция есть на бирже, но нет open DB record · требуется repair"
    if audit.status == "DB_DUPLICATE":
        return "⚠️ TP/SL: дубли open DB record · требуется repair"
    if audit.status == "MISSING_ALL":
        return f"⚠️ TP/SL на бирже: {audit.tp_count} TP / {audit.sl_count} SL · требуется repair"
    return f"⚠️ TP/SL на бирже: {audit.tp_count} TP / {audit.sl_count} SL · частичная защита, требуется repair"

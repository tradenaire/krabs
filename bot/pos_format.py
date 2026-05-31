"""Shared position block formatter used by balance and positions handlers."""
from bot.fmt import fmt_pct, fmt_usd


def _calc_tp_price(entry: float, lev: int, tp_pct: float, side: str) -> float:
    move = entry * tp_pct / 100 / lev
    return entry - move if side == "short" else entry + move


def _calc_sl_price(entry: float, lev: int, sl_pct: float, side: str) -> float:
    move = entry * sl_pct / 100 / lev
    return entry + move if side == "short" else entry - move


def _pnl_pct_at_price(entry: float, lev: int, price: float, side: str) -> float:
    if entry <= 0:
        return 0.0
    if side == "short":
        return (entry - price) / entry * lev * 100
    return (price - entry) / entry * lev * 100


def _fmt_funding(rate: float, lev: int, margin: float, next_ts: str | None = None) -> str:
    if rate == 0:
        return ""
    from datetime import datetime, timezone
    pct = rate * 100
    daily_usdt = abs(rate) * 3 * lev * margin  # 3 periods × leverage × margin
    sign = "+" if rate > 0 else ""
    icon = "💰" if rate > 0 else ("⚠️" if rate > -0.001 else "🚨")
    line = f"{icon} Фандинг `{sign}{pct:.4f}%`/8h · ~`${daily_usdt:.4f}`/день"
    if next_ts:
        try:
            if isinstance(next_ts, (int, float)):
                dt = datetime.fromtimestamp(next_ts / 1000, tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(str(next_ts).replace("Z", "+00:00"))
            diff = dt - datetime.now(tz=timezone.utc)
            mins = int(diff.total_seconds() / 60)
            if mins >= 0:
                if mins >= 60:
                    line += f" · через `{mins // 60}ч {mins % 60}м`"
                else:
                    line += f" · через `{mins}м`"
        except Exception:
            pass
    return line


def format_position_block(pos: dict, db_rec: dict | None, re_rec: dict | None,
                          config, tp_sl_pcts: dict,
                          max_lev: int = 0, max_pos_usdt: float = 0,
                          funding_rate: float = 0.0,
                          funding_next_ts=None) -> str:
    symbol = pos["symbol"]
    coin = symbol.split("/")[0]
    side = pos.get("side", "short")
    lev = int(pos.get("leverage", 1))
    entry = float(pos.get("entry_price", 0))
    mark = float(pos.get("mark_price", 0))
    liq = float(pos.get("liquidation_price", 0))
    pnl = float(pos.get("unrealized_pnl", 0))
    pct = float(pos.get("percentage", 0))
    margin = float(pos.get("margin", 0))

    # Re-entry info (always shown)
    cycle_count = int(re_rec.get("cycle_count", 0)) if re_rec else 0
    max_cycles = int(re_rec.get("max_cycles", 3)) if re_rec else (
        int(getattr(config, "max_reentry_cycles", 3)) if config else 3
    )
    re_label = f"RE{cycle_count}/{max_cycles}"

    # PnL indicator
    pnl_icon = "🟢" if pnl >= 0 else "🔴"

    # Liq distance
    dist_str = ""
    if liq > 0 and mark > 0:
        dist = abs(mark - liq) / mark * 100
        dist_str = f" ({dist:.1f}% до ликв.)"

    # TP/SL from stored pcts
    stored = tp_sl_pcts.get(symbol, {})
    tp_pct_val = stored.get("tp_pct") or (db_rec.get("tp_pct") if db_rec else None) or 500.0
    sl_pct_val = stored.get("sl_pct") or (db_rec.get("sl_pct") if db_rec else None) or 500.0
    tp_price = _calc_tp_price(entry, lev, tp_pct_val, side)
    sl_price = _calc_sl_price(entry, lev, sl_pct_val, side)

    # Averaging info
    threshold = float(getattr(config, "averaging_threshold", -100)) if config else -100
    avg_amount = float(getattr(config, "averaging_amount", 0.1)) if config else 0.1
    avg_count = int(db_rec.get("averaging_count", 0)) if db_rec else 0
    max_avg = int(getattr(config, "max_averaging_count", 100)) if config else 100
    invested = float(db_rec.get("total_invested", margin)) if db_rec else margin
    # Effective threshold from dynamic rules
    eff_threshold = threshold
    try:
        import json as _json
        from bot import db as _db_pf
        _dyn_raw = _db_pf.get_config("avg_dynamic_rules", "")
        if _dyn_raw:
            for rule in sorted(_json.loads(_dyn_raw), key=lambda r: r["after"]):
                if avg_count >= rule["after"]:
                    eff_threshold = float(rule["pnl"])
    except Exception:
        pass
    # Re-entry reopen margin
    reopen_margin = float(re_rec.get("margin", margin)) if re_rec else margin

    side_icon = "🔴⬇️" if side == "short" else "🟢⬆️"
    lines = [
        f"{coin} {side_icon} {lev}x ${margin:.2f} {re_label}",
        f"▶️ {entry:.6g}",
        f"{pnl_icon} {fmt_usd(pnl)} ({fmt_pct(pct)})",
    ]
    if liq > 0:
        lines.append(f"☠️ {liq:.6g}{dist_str}")
    sl_pnl_pct = _pnl_pct_at_price(entry, lev, sl_price, side)
    lines.append(f"SL:{sl_pnl_pct:+.0f}% ({sl_price:.6g})")
    lines.append(f"TP:{tp_price:.6g} (+{tp_pct_val:.0f}%)")

    # Max leverage / position limit (if provided)
    if max_lev > 0 or max_pos_usdt > 0:
        extra = []
        if max_lev > 0:
            extra.append(f"макс ×{max_lev}")
        if max_pos_usdt > 0:
            extra.append(f"лимит ${max_pos_usdt:,.0f}")
        lines.append("⚙️ " + " · ".join(extra))

    _thr_str = f"{eff_threshold:.0f}%"
    if eff_threshold != threshold:
        _thr_str += f" _(dyn)_"
    lines.append(
        f"🔁 Докупка: при PnL ≤ {_thr_str} · +${avg_amount:.2f}"
        f" · шагов {avg_count}/{max_avg} · вложено ${invested:.2f}"
    )
    lines.append(
        f"🔄 Перезаход: после TP +{tp_pct_val:.0f}%"
        f" → reopen ${reopen_margin:.2f} · циклов {cycle_count}/{max_cycles}"
    )
    funding_str = _fmt_funding(funding_rate, lev, margin, funding_next_ts)
    if funding_str:
        lines.append(funding_str)
    return "\n".join(lines)

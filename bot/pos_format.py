"""Shared position block formatter used by balance and positions handlers."""
from bot.fmt import fmt_pct, fmt_usd
from bot.services.tpsl import calc_tp_price as _calc_tp_price
from bot.services.tpsl import calc_sl_price as _calc_sl_price
from bot.services.tpsl import pnl_pct_at_price as _pnl_pct_at_price


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


def _active_ladder(symbol: str) -> dict | None:
    try:
        from bot import db as _db_pf
        return _db_pf.get_tp_ladder(symbol)
    except Exception:
        return None


def _format_ladder_lines(ladder: dict, entry: float, lev: int, side: str) -> list[str]:
    lines: list[str] = []
    for idx in (1, 2, 3):
        price = float(ladder.get(f"tp{idx}") or 0)
        if price <= 0:
            continue
        pnl_pct = _pnl_pct_at_price(entry, lev, price, side)
        state = " filled" if int(ladder.get(f"filled{idx}") or 0) else ""
        lines.append(f"TP{idx}:{price:.6g} (+{pnl_pct:.0f}%){state}")

    sl_price = float(ladder.get("sl") or 0)
    if int(ladder.get("sl_at_breakeven") or 0):
        sl_price = entry
        sl_note = " breakeven"
    else:
        sl_note = ""
    if sl_price > 0:
        sl_pnl_pct = _pnl_pct_at_price(entry, lev, sl_price, side)
        lines.append(f"SL:{sl_pnl_pct:+.0f}% ({sl_price:.6g}){sl_note}")
    return lines


def _format_order_lines(orders: list[dict], entry: float, lev: int, side: str) -> list[str]:
    if not orders:
        return []
    tp_type, sl_type = (1, 2) if side == "long" else (2, 1)
    tp_prices = [
        float(order.get("trigger_price") or 0)
        for order in orders
        if int(order.get("trigger_type") or 0) == tp_type
        and float(order.get("trigger_price") or 0) > 0
    ]
    sl_prices = [
        float(order.get("trigger_price") or 0)
        for order in orders
        if int(order.get("trigger_type") or 0) == sl_type
        and float(order.get("trigger_price") or 0) > 0
    ]
    tp_prices = sorted(set(tp_prices), reverse=(side == "short"))[:3]

    lines: list[str] = []
    for idx, price in enumerate(tp_prices, 1):
        pnl_pct = _pnl_pct_at_price(entry, lev, price, side)
        lines.append(f"TP{idx}:{price:.6g} (+{pnl_pct:.0f}%)")
    if sl_prices:
        sl_price = min(sl_prices, key=lambda price: abs(price - entry))
        sl_pnl_pct = _pnl_pct_at_price(entry, lev, sl_price, side)
        lines.append(f"SL:{sl_pnl_pct:+.0f}% ({sl_price:.6g})")
    return lines


def _format_config_ladder_lines(entry: float, lev: int, side: str, config,
                                sl_price: float) -> list[str]:
    raw = str(getattr(config, "tp_ladder_pcts", "50,120,250")) if config else "50,120,250"
    try:
        pcts = [float(x) for x in raw.split(",") if x.strip()][:3]
    except Exception:
        pcts = [50.0, 120.0, 250.0]
    while len(pcts) < 3:
        pcts.append(pcts[-1] * 2 if pcts else 100.0)

    lines: list[str] = []
    for idx, pct in enumerate(pcts[:3], 1):
        price = _calc_tp_price(entry, lev, pct, side)
        lines.append(f"TP{idx}:{price:.6g} (+{pct:.0f}%)")
    sl_pnl_pct = _pnl_pct_at_price(entry, lev, sl_price, side)
    lines.append(f"SL:{sl_pnl_pct:+.0f}% ({sl_price:.6g})")
    return lines


def format_position_block(pos: dict, db_rec: dict | None, re_rec: dict | None,
                          config, tp_sl_pcts: dict,
                          max_lev: int = 0, max_pos_usdt: float = 0,
                          funding_rate: float = 0.0,
                          funding_next_ts=None,
                          active_tpsl_orders: list[dict] | None = None) -> str:
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

    # TP/SL from active ladder or stored pcts
    stored = tp_sl_pcts.get(symbol, {})
    tp_pct_val = stored.get("tp_pct") or (db_rec.get("tp_pct") if db_rec else None) or 500.0
    sl_pct_val = stored.get("sl_pct") or (db_rec.get("sl_pct") if db_rec else None) or 500.0
    sl_price = _calc_sl_price(entry, lev, sl_pct_val, side)
    ladder = _active_ladder(symbol)

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
    if ladder:
        lines.extend(_format_ladder_lines(ladder, entry, lev, side))
    elif active_tpsl_orders:
        order_lines = _format_order_lines(active_tpsl_orders, entry, lev, side)
        if order_lines:
            lines.extend(order_lines)
        else:
            lines.extend(_format_config_ladder_lines(entry, lev, side, config, sl_price))
    else:
        lines.extend(_format_config_ladder_lines(entry, lev, side, config, sl_price))

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
    if config and not bool(getattr(config, "averaging_enabled", True)):
        lines.append(
            f"🔁 Averaging: disabled · шагов {avg_count}/{max_avg} · вложено ${invested:.2f}"
        )
    else:
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

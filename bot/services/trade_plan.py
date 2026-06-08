"""Three-TP trade plan math and Telegram formatting."""
from __future__ import annotations

from dataclasses import dataclass

from bot.services.ladder import parse_price
from bot.services.tpsl import pnl_pct_at_price


TP_SHARES = (50.0, 25.0, 25.0)


@dataclass(frozen=True)
class TpPlanLevel:
    index: int
    price: float
    share_pct: float
    move_pct: float
    pnl_pct: float
    profit_usdt: float


@dataclass(frozen=True)
class ThreeTpPlan:
    symbol: str
    side: str
    entry: float
    reference: float
    leverage: int
    margin: float
    levels: tuple[TpPlanLevel, ...]
    sl_price: float
    sl_move_pct: float
    sl_pnl_pct: float
    filtered: bool = False


def _side_name(side: str) -> str:
    value = (side or "").lower()
    return "short" if value in ("sell", "short") else "long"


def _is_valid_tp(side: str, price: float, reference: float) -> bool:
    if price <= 0 or reference <= 0:
        return False
    return price < reference if side == "short" else price > reference


def _is_valid_sl(side: str, price: float, reference: float) -> bool:
    if price <= 0 or reference <= 0:
        return False
    return price > reference if side == "short" else price < reference


def _distance_pct(entry: float, price: float) -> float:
    if entry <= 0:
        return 0.0
    return abs(price - entry) / entry * 100.0


def build_three_tp_plan(
    *,
    symbol: str,
    side: str,
    entry: float,
    reference: float | None,
    leverage: int,
    margin: float,
    tp_prices: list[float] | tuple[float, ...],
    sl_price: float,
    tp_shares: list[float] | tuple[float, ...] | None = None,
    config=None,
) -> ThreeTpPlan:
    """Validate and summarize a 3-TP trade plan."""
    norm_side = _side_name(side)
    entry = float(entry or 0)
    reference = float(reference or entry or 0)
    leverage = int(leverage or 1)
    margin = float(margin or 0)
    sl_price = float(sl_price or 0)
    if entry <= 0 or reference <= 0:
        raise ValueError("entry/reference must be positive")
    if not _is_valid_sl(norm_side, sl_price, reference):
        raise ValueError("SL is invalid for current price")

    normalized_tps = [float(p or 0) for p in list(tp_prices)[:3]]
    while len(normalized_tps) < 3:
        normalized_tps.append(0.0)
    normalized_shares = [float(s or 0) for s in list(tp_shares or TP_SHARES)[:3]]
    while len(normalized_shares) < 3:
        normalized_shares.append(0.0)

    valid_items = [
        (idx, price, share)
        for idx, (price, share) in enumerate(zip(normalized_tps, normalized_shares), 1)
        if _is_valid_tp(norm_side, price, reference)
    ]
    if not valid_items:
        raise ValueError("No valid TP targets remain for current price")
    share_total = sum(share for _idx, _price, share in valid_items) or 100.0

    levels: list[TpPlanLevel] = []
    for idx, price, original_share in valid_items:
        share_pct = original_share / share_total * 100.0
        pnl_pct = pnl_pct_at_price(entry, leverage, price, norm_side)
        profit = margin * (share_pct / 100.0) * max(pnl_pct, 0) / 100.0
        levels.append(
            TpPlanLevel(
                index=idx,
                price=price,
                share_pct=share_pct,
                move_pct=_distance_pct(entry, price),
                pnl_pct=pnl_pct,
                profit_usdt=profit,
            )
        )

    sl_pnl = pnl_pct_at_price(entry, leverage, sl_price, norm_side)
    return ThreeTpPlan(
        symbol=symbol,
        side=norm_side,
        entry=entry,
        reference=reference,
        leverage=leverage,
        margin=margin,
        levels=tuple(levels),
        sl_price=sl_price,
        sl_move_pct=_distance_pct(entry, sl_price),
        sl_pnl_pct=sl_pnl,
        filtered=len(valid_items) < 3,
    )


def plan_from_pick(
    *,
    symbol: str,
    side: str,
    entry: float,
    reference: float | None,
    leverage: int,
    margin: float,
    pick: dict,
    config=None,
) -> ThreeTpPlan:
    return build_three_tp_plan(
        symbol=symbol,
        side=side,
        entry=entry,
        reference=reference,
        leverage=leverage,
        margin=margin,
        tp_prices=[parse_price(pick.get(k, "")) for k in ("tp1", "tp2", "tp3")],
        sl_price=parse_price(pick.get("sl", "")),
        config=config,
    )


def format_three_tp_plan(plan: ThreeTpPlan, *, title: str = "Trade preview") -> str:
    direction = "SHORT" if plan.side == "short" else "LONG"
    lines = [
        f"*{title}*",
        f"{plan.symbol.split('/')[0]} {direction} x{plan.leverage} margin `${plan.margin:.2f}`",
        f"Entry: `{plan.entry:.8g}` | Reference: `{plan.reference:.8g}`",
    ]
    if plan.filtered:
        lines.append("Warning: opening only valid TP targets for current price.")
    for level in plan.levels:
        lines.append(
            f"TP{level.index}: `{level.price:.8g}` | +{level.pnl_pct:.0f}% "
            f"({level.move_pct:.2f}% price) | share {level.share_pct:.0f}% "
            f"| profit `${level.profit_usdt:.2f}`"
        )
    lines.append(
        f"SL: `{plan.sl_price:.8g}` | {plan.sl_pnl_pct:.0f}% "
        f"({plan.sl_move_pct:.2f}% price)"
    )
    return "\n".join(lines)


def plan_fingerprint(plan: ThreeTpPlan) -> tuple:
    return (
        round(plan.entry, 12),
        round(plan.reference, 12),
        int(plan.leverage),
        round(plan.margin, 8),
        tuple((level.index, round(level.price, 12), round(level.share_pct, 8)) for level in plan.levels),
        round(plan.sl_price, 12),
    )


def pick_from_plan(plan: ThreeTpPlan) -> dict:
    pick = {f"tp{idx}": "" for idx in (1, 2, 3)}
    for level in plan.levels:
        pick[f"tp{level.index}"] = str(level.price)
    pick["sl"] = str(plan.sl_price)
    return pick

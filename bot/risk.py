"""Risk guards for futures open/averaging paths.

The functions here are intentionally pure. Trading handlers/jobs should use
these calculations before any exchange order so MEXC contract rounding cannot
silently bypass per-position budgets.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


EPS = 1e-3


@dataclass(frozen=True)
class OrderPlan:
    requested_margin: float
    actual_margin: float
    actual_notional: float
    contracts: int
    price: float
    leverage: int
    contract_size: float


@dataclass(frozen=True)
class AvgBudgetState:
    initial_margin: float
    total_invested: float
    averaging_budget: float
    avg_spent: float
    budget_left: float


@dataclass(frozen=True)
class AvgDecision:
    allowed: bool
    reason: str
    state: AvgBudgetState
    next_margin: float


def plan_contract_order(
    requested_margin: float,
    leverage: int,
    price: float,
    contract_size: float,
    min_contracts: int = 1,
) -> OrderPlan:
    """Return actual order size after MEXC whole-contract rounding.

    requested_margin is the margin the bot wants to spend. MEXC accepts integer
    contract volume, so the actual margin can be materially higher. Budgets must
    be checked against actual_margin.
    """
    requested_margin = float(requested_margin or 0)
    leverage = int(leverage or 1)
    price = float(price or 0)
    contract_size = float(contract_size or 0)
    min_contracts = max(1, int(min_contracts or 1))
    if requested_margin <= 0:
        raise ValueError("requested_margin must be > 0")
    if leverage <= 0:
        raise ValueError("leverage must be > 0")
    if price <= 0:
        raise ValueError("price must be > 0")
    if contract_size <= 0:
        raise ValueError("contract_size must be > 0")

    amount_base = requested_margin * leverage / price
    contracts = max(min_contracts, math.ceil(amount_base / contract_size))
    actual_notional = contracts * contract_size * price
    actual_margin = actual_notional / leverage
    return OrderPlan(
        requested_margin=requested_margin,
        actual_margin=actual_margin,
        actual_notional=actual_notional,
        contracts=contracts,
        price=price,
        leverage=leverage,
        contract_size=contract_size,
    )


def avg_budget_state(
    total_invested: float,
    initial_margin: float,
    averaging_budget: float,
) -> AvgBudgetState:
    initial_margin = max(0.0, float(initial_margin or 0))
    total_invested = max(0.0, float(total_invested or 0))
    averaging_budget = max(0.0, float(averaging_budget or 0))
    avg_spent = max(0.0, total_invested - initial_margin)
    budget_left = max(0.0, averaging_budget - avg_spent)
    return AvgBudgetState(
        initial_margin=initial_margin,
        total_invested=total_invested,
        averaging_budget=averaging_budget,
        avg_spent=avg_spent,
        budget_left=budget_left,
    )


def check_averaging_budget(
    total_invested: float,
    initial_margin: float,
    averaging_budget: float,
    next_margin: float,
    eps: float = EPS,
) -> AvgDecision:
    """Decide whether the next averaging order fits the position budget."""
    state = avg_budget_state(total_invested, initial_margin, averaging_budget)
    next_margin = max(0.0, float(next_margin or 0))
    if next_margin <= 0:
        return AvgDecision(False, "next_margin_zero", state, next_margin)
    if next_margin > state.budget_left + eps:
        return AvgDecision(False, "averaging_budget_exhausted", state, next_margin)
    return AvgDecision(True, "ok", state, next_margin)


def should_reenter_after_close(
    close_reason: str | None,
    closed_by_tp: bool | None = None,
    profitable_sl: bool = False,
    averaging_exhausted: bool = False,
) -> bool:
    """Safe re-entry policy.

    Re-enter only after TP/profitable SL. Never re-enter after liquidation,
    loss-SL, or a position whose averaging budget was exhausted.
    """
    reason = (close_reason or "").strip().lower()
    if averaging_exhausted:
        return False
    if reason == "liquidated":
        return False
    if profitable_sl:
        return True
    if closed_by_tp is True or reason == "tp":
        return True
    return False

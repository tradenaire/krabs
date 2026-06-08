# Zero Drift Open TP/SL Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every position-open and TP/SL setup claim match Binance state exactly, with no user-facing success message until live exchange orders have been validated.

**Architecture:** Add a small TP/SL validation and verification contract in `bot/services/tpsl.py`, use it from single TP/SL, ladder TP/SL, and signal multi-TP flows, and make the audit tool report the same invariant. Invalid exits fail before placement; placed exits are read back from Binance and compared before the bot claims protection exists.

**Tech Stack:** Python 3.12, ccxt Binance USDM, python-telegram-bot, pytest/unittest, SQLite bot config.

---

### Requirements From Current Evidence

- Toronto live state is `provider=binance_testnet`, `exit_mode=single`, `tp_ladder_pcts=50,120,250`, `sl_pct=500`.
- Direct Binance read-only audit showed `positions_count=4` and `tpsl_orders_count=0`.
- Current old LONG x1 positions produce negative SL prices with `sl_pct=500`, so placing those stops would be invalid and must not be represented as active protection.
- Completion requires: plan, tests, code, commit, server restart, and direct Binance audit after restart.

### Files

- Modify: `bot/services/tpsl.py`
- Modify: `bot/services/ladder.py`
- Modify: `bot/signals/execution.py`
- Modify: `bot/services/trading.py`
- Modify: `tools/binance_tpsl_audit.py`
- Test: `tests/test_tpsl_validation.py`
- Test: `tests/test_ladder_filtered_tps.py`
- Test: `tests/test_signal_execution.py`

### Task 1: Validate Exit Prices Before Placement

- [ ] **Step 1: Write failing validation tests**

Create `tests/test_tpsl_validation.py` with:

```python
import unittest

from bot.services.tpsl import validate_exit_prices


class TpslValidationTests(unittest.TestCase):
    def test_rejects_negative_long_sl_before_exchange_call(self):
        with self.assertRaisesRegex(ValueError, "SL.*positive"):
            validate_exit_prices(
                symbol="RENDER/USDT:USDT",
                side="long",
                reference=1.637,
                tp_prices=[2.5, 3.6, 5.8],
                sl_price=-6.716,
            )

    def test_rejects_long_tp_below_or_equal_reference(self):
        with self.assertRaisesRegex(ValueError, "TP1.*above"):
            validate_exit_prices(
                symbol="HOME/USDT:USDT",
                side="long",
                reference=0.02868,
                tp_prices=[0.028, 0.043, 0.063],
                sl_price=0.02,
            )

    def test_accepts_three_long_tps_and_one_sl_on_correct_sides(self):
        validate_exit_prices(
            symbol="HOME/USDT:USDT",
            side="long",
            reference=0.02868,
            tp_prices=[0.043, 0.063, 0.101],
            sl_price=0.02,
        )
```

- [ ] **Step 2: Run validation tests red**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -B -m pytest -q tests/test_tpsl_validation.py
```

Expected: import or missing function failure.

- [ ] **Step 3: Implement `validate_exit_prices`**

In `bot/services/tpsl.py`, add:

```python
def validate_exit_prices(symbol: str, side: str, reference: float,
                         tp_prices: list[float] | tuple[float, ...],
                         sl_price: float | None = None) -> None:
    reference = float(reference or 0)
    if reference <= 0:
        raise ValueError(f"{symbol}: reference price must be positive before TP/SL placement.")
    norm_side = "short" if side in ("short", "sell") else "long"
    for idx, raw_price in enumerate(tp_prices, 1):
        price = float(raw_price or 0)
        if price <= 0:
            raise ValueError(f"{symbol}: TP{idx} must be positive before TP/SL placement.")
        if norm_side == "long" and price <= reference:
            raise ValueError(f"{symbol}: TP{idx} must be above current price for LONG.")
        if norm_side == "short" and price >= reference:
            raise ValueError(f"{symbol}: TP{idx} must be below current price for SHORT.")
    if sl_price is not None:
        sl = float(sl_price or 0)
        if sl <= 0:
            raise ValueError(f"{symbol}: SL must be positive before TP/SL placement.")
        if norm_side == "long" and sl >= reference:
            raise ValueError(f"{symbol}: SL must be below current price for LONG.")
        if norm_side == "short" and sl <= reference:
            raise ValueError(f"{symbol}: SL must be above current price for SHORT.")
```

- [ ] **Step 4: Run validation tests green**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -B -m pytest -q tests/test_tpsl_validation.py
```

Expected: all tests pass.

### Task 2: Verify Exchange Orders After Placement

- [ ] **Step 1: Write failing verification tests**

Extend `tests/test_tpsl_validation.py`:

```python
import asyncio

from bot.services.tpsl import verify_exit_orders


class FakeOrderClient:
    def __init__(self, orders):
        self.orders = orders

    async def get_tp_sl_orders(self, symbol):
        return self.orders


class TpslVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_exact_three_tps_and_one_sl(self):
        client = FakeOrderClient([
            {"trigger_price": 1.2, "trigger_type": 1},
            {"trigger_price": 1.4, "trigger_type": 1},
            {"trigger_price": 1.6, "trigger_type": 1},
            {"trigger_price": 0.8, "trigger_type": 2},
        ])

        result = await verify_exit_orders(
            client,
            "EPIC/USDT:USDT",
            "long",
            tp_prices=[1.2, 1.4, 1.6],
            sl_price=0.8,
        )

        self.assertEqual(result["tp_count"], 3)
        self.assertEqual(result["sl_count"], 1)

    async def test_fails_when_exchange_has_no_orders(self):
        client = FakeOrderClient([])

        with self.assertRaisesRegex(RuntimeError, "expected 3 TP"):
            await verify_exit_orders(
                client,
                "EPIC/USDT:USDT",
                "long",
                tp_prices=[1.2, 1.4, 1.6],
                sl_price=0.8,
            )
```

- [ ] **Step 2: Run verification tests red**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -B -m pytest -q tests/test_tpsl_validation.py -k Verification
```

Expected: missing function failure.

- [ ] **Step 3: Implement `verify_exit_orders`**

In `bot/services/tpsl.py`, add a helper that reads `client.get_tp_sl_orders(symbol)`, maps TP/SL trigger types by side, compares prices with `trigger_price_matches`, and raises `RuntimeError` if any expected TP/SL is missing.

- [ ] **Step 4: Run verification tests green**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -B -m pytest -q tests/test_tpsl_validation.py
```

Expected: all validation and verification tests pass.

### Task 3: Wire Validation And Verification Into Open Paths

- [ ] **Step 1: Add failing ladder test**

In `tests/test_ladder_filtered_tps.py`, add a case where `compute_levels` returns a negative SL for LONG x1 and `setup_on_open` raises before `place_reduce_tp` is called.

- [ ] **Step 2: Add failing signal test**

In `tests/test_signal_execution.py`, add a fake client where `set_multi_tp_sl` appears successful but `get_tp_sl_orders` returns empty; assert `execute_signal` closes the fresh position and raises.

- [ ] **Step 3: Run targeted tests red**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -B -m pytest -q tests/test_ladder_filtered_tps.py tests/test_signal_execution.py
```

Expected: new tests fail.

- [ ] **Step 4: Implement wiring**

Use `validate_exit_prices` before:

- `client.set_tp_sl(...)`
- `client.set_multi_tp_sl(...)`
- `client.place_reduce_sl(...)`
- `client.place_reduce_tp(...)`

Use `verify_exit_orders` after successful placement in signal and ladder flows. Single TP/SL verification may expect one TP and one SL; ladder/signal expect the actual number of TP levels plus one SL.

- [ ] **Step 5: Run targeted tests green**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -B -m pytest -q tests/test_ladder_filtered_tps.py tests/test_signal_execution.py tests/test_tpsl_validation.py
```

Expected: all pass.

### Task 4: Make Audit Tool The Runtime Proof

- [ ] **Step 1: Extend audit verdict**

Update `tools/binance_tpsl_audit.py` so every requested open symbol prints one of:

- `OK_3TP_1SL`
- `UNPROTECTED_NO_ORDERS`
- `UNPROTECTED_INVALID_SL`
- `MISMATCH`

- [ ] **Step 2: Run local audit against Toronto**

Run through SSH or on Toronto:

```bash
cd /root/krabs
.venv/bin/python tools/binance_tpsl_audit.py --symbols RENDER,HYPE,APT,HOME
```

Expected before fixing old positions: current positions are not falsely reported as protected.

### Task 5: Commit, Deploy, Restart, Verify

- [ ] **Step 1: Full local tests**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -B -m pytest -q tests
```

Expected: all tests pass.

- [ ] **Step 2: Commit**

Run:

```bash
git add bot/services/tpsl.py bot/services/ladder.py bot/signals/execution.py bot/services/trading.py tools/binance_tpsl_audit.py tests/test_tpsl_validation.py tests/test_ladder_filtered_tps.py tests/test_signal_execution.py docs/superpowers/plans/2026-06-09-zero-drift-open-tpsl-verification.md
git commit -m "fix(tpsl): verify exchange exits before reporting protection"
```

- [ ] **Step 3: Deploy to Toronto**

Run on Toronto:

```bash
cd /root/krabs
git fetch origin BinanceTest
git reset --hard origin/BinanceTest
systemctl restart krabs.service
systemctl is-active krabs.service
```

Expected: `active`.

- [ ] **Step 4: Direct Binance verification**

Run on Toronto:

```bash
cd /root/krabs
.venv/bin/python tools/binance_tpsl_audit.py --symbols RENDER,HYPE,APT,HOME
```

Expected: no line claims `OK_3TP_1SL` unless Binance readback proves three TP triggers and at least one SL trigger for that symbol.


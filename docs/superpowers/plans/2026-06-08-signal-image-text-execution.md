# Signal Image And Text Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Telegram bot accept a signal image or text, extract symbol/side/entry/SL/TPs/leverage, show a confirmation card, and execute the confirmed signal with 3 TP orders plus 1 SL.

**Architecture:** Add a focused `bot/signals` package for parsing, formatting, pending-state storage, and execution. Wire it into `bot/main.py` before the free-form assistant handler so signal text is handled before generic NLP. Keep exchange behavior behind the existing `BinanceClient` surface, adding a dedicated multi-TP method for partial reduce-only Binance TP orders.

**Tech Stack:** Python 3.12, python-telegram-bot, unittest, ccxt Binance USDM Futures Demo/Live. Binance references: `https://developers.binance.com/docs/derivatives/`, `https://demo-fapi.binance.com/`, `https://developers.binance.com/docs/binance-spot-api-docs/demo-mode/general-info`.

---

### Task 1: Parser And Preview

**Files:**
- Create: `bot/signals/model.py`
- Create: `bot/signals/parser.py`
- Create: `bot/signals/preview.py`
- Test: `tests/test_signal_parser.py`

- [ ] **Step 1: Write failing parser tests**

Cover ChartScanAi text with `EPIC USDT`, `SHORT`, entry range, stop, TP1/TP2/TP3, confidence, and leverage.

- [ ] **Step 2: Implement parser/model/preview**

Use deterministic regex and validation. No GPT/LLM decision making.

- [ ] **Step 3: Run parser tests**

Run: `python -B -m unittest tests.test_signal_parser`

### Task 2: Pending Signal Store

**Files:**
- Create: `bot/signals/store.py`
- Test: `tests/test_signal_store.py`

- [ ] **Step 1: Write store tests**

Store parsed signals in `context.user_data` with short generated IDs, retrieve them, and clear them.

- [ ] **Step 2: Implement store**

Keep storage per-user/session in Telegram `user_data`; do not add DB unless later needed.

- [ ] **Step 3: Run store tests**

Run: `python -B -m unittest tests.test_signal_store`

### Task 3: Binance Multi TP Execution

**Files:**
- Modify: `bot/exchange/binance_client.py`
- Create: `bot/signals/execution.py`
- Test: `tests/test_signal_execution.py`

- [ ] **Step 1: Write execution tests**

Verify TP percentages default to 50/25/25, side mapping is correct, and Binance TP orders use reduce-only quantity rather than close-all.

- [ ] **Step 2: Implement execution**

Open the position with `place_futures_order`, fetch the filled position, place 3 reduce-only `TAKE_PROFIT_MARKET` orders with quantities split by TP shares, and place one close-position `STOP_MARKET` SL.

- [ ] **Step 3: Run execution tests**

Run: `python -B -m unittest tests.test_signal_execution`

### Task 4: Telegram Handler

**Files:**
- Create: `bot/handlers/signals.py`
- Modify: `bot/main.py`
- Test: `tests/test_signal_handler_format.py`

- [ ] **Step 1: Write handler-format tests**

Ensure confirmation text includes signal fields, 3 TP shares, and buttons for `$1`, `$2`, `$5`, `$10`, edit/check, cancel.

- [ ] **Step 2: Implement text/photo entrypoints**

Text handler parses signal-like messages. Photo handler downloads the image and tries OCR if an OCR package is installed; if OCR is unavailable, it asks for the signal text/caption without using GPT.

- [ ] **Step 3: Wire callbacks**

Register signal handlers before `assistant_handler` and callbacks before generic open callbacks.

- [ ] **Step 4: Run handler tests**

Run: `python -B -m unittest tests.test_signal_handler_format`

### Task 5: Full Verification

**Files:**
- Modify: `DEPLOY_TORONTO_BINANCE.md`

- [ ] **Step 1: Update docs**

Document signal flow, no-GPT rule, 3 TP default split, and Binance reference links.

- [ ] **Step 2: Run all tests**

Run: `python -B -m unittest discover`

- [ ] **Step 3: Commit on `BinanceTest` only**

Run: `git status --short --branch`, then commit related files only. Do not touch `main` and do not include `1.txt`.

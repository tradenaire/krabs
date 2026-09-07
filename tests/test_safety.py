"""Offline acceptance checks: python -m unittest discover -s tests -v."""
import asyncio
import datetime as dt
import json
import socket
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import db
from bot.config import Config
from bot.exchange.client import ExchangeClient, available_margin
from bot.lifecycle import register_position, reconcile_closures, adopt_handler
from bot.jobs.main import averaging_job, margin_emergency_job, tpsl_enforce_job, reentry_job


SYMBOL = "ALLO/USDT:USDT"
NOW = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


def position(pid="100", side="short", **values):
    return {"symbol": SYMBOL, "position_id": pid, "opened_at_ms": NOW - 600_000,
            "side": side, "contracts": 100, "entry_price": 100, "leverage": 10,
            "margin": 10, "margin_mode": "cross", "percentage": 0,
            "unrealized_pnl": 0, "settle_currency": "USDT", **values}


class Gateway:
    def __init__(self):
        self.plans, self.placed, self.cancelled, self.submitted = [], [], [], []
        self.fail_kind = None
        self.timeout = False
        self.invisible = False
        self.closed, self.history, self.assets = [], [], [
            {"currency": "USDT", "availableBalance": 100, "availableOpen": 100, "equity": 110}]
        self.markets = {SYMBOL: {"id": "ALLO_USDT", "contractSize": .01, "settle": "USDT"}}
        self.live = [position()]

    async def load_markets(self):
        return self.markets

    def market(self, symbol):
        return self.markets[symbol]

    def price_to_precision(self, symbol, price):
        return str(round(price, 6))

    async def contractPrivateGetPlanorderListOrders(self, params):
        rows = self.plans if not self.invisible else []
        return {"success": True, "data": rows if params["page_num"] == 1 else []}

    async def contractPrivatePostPlanorderPlace(self, params):
        self.placed.append(params)
        kind = "SL" if params["triggerType"] == 1 else "TP"
        if self.timeout:
            raise TimeoutError("simulated lost response")
        if self.fail_kind == kind:
            return {"success": False, "code": 510, "message": "simulated rejection"}
        order_id = str(len(self.placed) + 10)
        self.plans.append({**params, "id": order_id, "state": 1})
        return {"success": True, "data": order_id}

    async def contractPrivatePostPlanorderCancel(self, params):
        self.cancelled.extend(o["orderId"] for o in params)
        self.plans = [p for p in self.plans if str(p["id"]) not in self.cancelled]
        return {"success": True}

    async def contractPrivateGetAccountAssets(self):
        return {"success": True, "data": self.assets}

    async def contractPrivateGetPositionListHistoryPositions(self, params):
        return {"success": True, "data": self.closed}

    async def contractPrivateGetOrderListHistoryOrders(self, params):
        return {"success": True, "data": self.history}

    async def contractPrivatePostOrderSubmit(self, params):
        self.submitted.append(params)
        if params["side"] in (1, 3):
            self.live = [position(pid="bot-next", side="long" if params["side"] == 1 else "short")]
        return {"success": True, "data": "regular-1"}

    async def fetch_ticker(self, symbol):
        return {"last": 100}

    async def set_leverage(self, *args, **kwargs):
        return {"success": True}

    async def contractPrivateGetOrderGetOrderId(self, params):
        return {"success": True, "data": {"positionId": "bot-next"}}


class SafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        test_db = Path(__file__).parent / f"test-{uuid.uuid4().hex}.db"
        for suffix in ("", "-wal", "-shm"):
            self.addCleanup(Path(str(test_db) + suffix).unlink, missing_ok=True)
        self.addCleanup(patch.stopall)
        patch.object(db, "DB_PATH", test_db).start()
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("NETWORK FORBIDDEN")).start()
        patch.object(socket.socket, "connect", side_effect=AssertionError("NETWORK FORBIDDEN")).start()
        db.init_db()
        self.gateway = Gateway()
        self.client = ExchangeClient.__new__(ExchangeClient)
        self.client._exchange = self.gateway
        self.client._mutation_lock = asyncio.Lock()
        self.client.get_positions = AsyncMock(side_effect=lambda: self.gateway.live.copy())
        self.config = Config(allowed_user_ids=[1], averaging_enabled=False)
        self.app = SimpleNamespace(bot_data={"config": self.config, "exchange": self.client},
                                   bot=SimpleNamespace(send_message=AsyncMock()))
        self.record = register_position(position(), self.config, tp_pct=100, sl_pct=100)

    async def asyncTearDown(self):
        # Restored here because event-loop teardown needs real sockets.
        patch.stopall()

    def saved_plan(self, kind, oid="saved", price=None, **values):
        price = price if price is not None else (90 if kind == "TP" else 110)
        plan = {"id": oid, "symbol": "ALLO_USDT", "state": 1, "triggerPrice": str(price),
                "triggerType": 2 if kind == "TP" else 1, "side": 2, "vol": 100,
                "openType": 2, "orderType": 5, "trend": 1, **values}
        self.gateway.plans.append(plan)
        db.save_bot_order(oid, self.record["id"], SYMBOL, kind, price, confirmed=True)
        return plan

    def closed_history(self, *, reason="tp", pnl=1.5379, pid="100", order_id="fill-1"):
        self.gateway.live = []
        self.gateway.closed = [{"positionId": pid, "symbol": "ALLO_USDT", "positionType": 2,
            "createTime": NOW - 600_000, "updateTime": NOW - 120_000, "state": 3,
            "realised": pnl, "closeAvgPrice": 92.123}]
        self.gateway.history = [{"orderId": order_id, "positionId": pid, "symbol": "ALLO_USDT", "side": 2,
            "dealVol": 100, "dealAvgPrice": 92.123, "updateTime": NOW - 120_000,
            "category": 2 if reason == "liquidation" else 4 if reason == "adl" else 1}]
        if reason in ("tp", "sl", "profit_lock"):
            self.saved_plan("TP" if reason == "tp" else "SL")
            self.gateway.plans[-1].update(state=3, orderId=order_id)
            if reason == "profit_lock":
                db.save_bot_order("saved", self.record["id"], SYMBOL, "profit_lock", 95, confirmed=True)
        elif reason in ("manual", "manual_reentry"):
            db.save_bot_order(order_id, self.record["id"], SYMBOL, reason, order_type="regular", confirmed=True)

    async def test_missing_sl_keeps_existing_tp_and_manual_order(self):
        self.saved_plan("TP")
        self.gateway.plans.append({**self.gateway.plans[0], "id": "manual", "triggerPrice": "88"})
        await tpsl_enforce_job(self.app)
        self.assertEqual([o["triggerType"] for o in self.gateway.placed], [1])
        self.assertEqual(self.gateway.cancelled, [])
        self.assertEqual(len(self.gateway.plans), 3)

    async def test_rejection_partial_success_and_no_false_sl(self):
        self.gateway.fail_kind = "SL"
        with self.assertRaisesRegex(RuntimeError, "confirmed: TP"):
            await self.client.set_tp_sl(SYMBOL, 90, 110)
        self.assertEqual([o["kind"] for o in db.get_bot_orders()], ["TP"])
        self.assertIsNone(db.get_position_by_id(self.record["id"])["locked_sl"])

    async def test_tp_failure_retains_confirmed_profit_lock(self):
        self.gateway.fail_kind = "TP"
        with self.assertRaisesRegex(RuntimeError, "confirmed: SL"):
            await self.client.set_tp_sl(SYMBOL, 90, 95, profit_lock_step=100)
        rec = db.get_position_by_id(self.record["id"])
        self.assertEqual(rec["locked_sl"], 95)
        self.assertEqual(rec["profit_lock_step"], 100)

    async def test_accepted_but_invisible_is_not_success(self):
        self.gateway.invisible = True
        with patch("bot.exchange.client.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "not confirmed"):
                await self.client.set_tp_sl(SYMBOL, None, 110)
        self.assertEqual(db.get_bot_orders()[0]["confirmed"], 0)

    async def test_timeout_has_no_automatic_duplicate_retry(self):
        self.gateway.timeout = True
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await self.client.set_tp_sl(SYMBOL, None, 110)
        self.assertEqual(len(self.gateway.placed), 1)

    async def test_wrong_side_volume_direction_price_replaced_individually(self):
        for override in ({"side": 4}, {"vol": 99}, {"triggerType": 1}, {"triggerPrice": "89"}):
            self.gateway.plans, self.gateway.cancelled, self.gateway.placed = [], [], []
            self.saved_plan("TP", **override)
            result = await self.client.set_tp_sl(SYMBOL, 90, None)
            self.assertTrue(result[0]["confirmed"])
            self.assertEqual(self.gateway.cancelled, ["saved"])
            self.assertEqual(len(self.gateway.plans), 1)

    async def test_profit_lock_restart_restores_tight_stop(self):
        await self.client.set_tp_sl(SYMBOL, 90, 95, profit_lock_step=100)
        self.gateway.plans = [o for o in self.gateway.plans if o["triggerType"] == 2]
        self.app.bot_data = {"config": self.config, "exchange": self.client}
        db.init_db()
        await tpsl_enforce_job(self.app)
        self.assertEqual(float(self.gateway.placed[-1]["triggerPrice"]), 95)
        self.assertEqual(db.get_position_by_id(self.record["id"])["profit_lock_step"], 100)

    async def test_manual_replacement_not_managed_in_any_background_path(self):
        self.gateway.live = [position(pid="new-manual", percentage=-500)]
        self.config.averaging_enabled = True
        self.config.averaging_profit_lock_trigger = 100
        await averaging_job(self.app)
        await margin_emergency_job(self.app)
        await tpsl_enforce_job(self.app)
        self.assertEqual(self.gateway.placed + self.gateway.submitted, [])
        self.assertIsNone(db.get_managed_position(self.gateway.live[0]))

    async def test_legacy_symbol_record_is_not_ownership(self):
        db.upsert_position("BTC/USDT:USDT", "short", 100, 10, 10)
        self.assertIsNone(db.get_managed_position(position(pid="old", symbol="BTC/USDT:USDT")))

    async def test_cancel_only_owned_ids_and_normalize_symbols(self):
        self.saved_plan("TP")
        self.gateway.plans.append({**self.gateway.plans[0], "id": "manual"})
        self.assertEqual(self.client.futures_symbol("ALLO_USDT"), SYMBOL)
        await self.client.cancel_tp_sl_orders("ALLO_USDT", self.record["id"])
        self.assertEqual(self.gateway.cancelled, ["saved"])
        self.assertEqual(self.gateway.plans[0]["id"], "manual")

    async def test_zero_margin_emergency_independent_of_averaging_and_restart(self):
        self.config.averaging_enabled = False
        self.app.bot_data["_avg_disabled_until"] = NOW / 1000 + 3600
        self.gateway.assets[0].update(availableOpen=0, availableBalance=0)
        await margin_emergency_job(self.app)
        self.assertEqual(self.gateway.submitted[0]["vol"], 10)
        self.app.bot_data = {"config": self.config, "exchange": self.client}
        await margin_emergency_job(self.app)
        self.assertEqual(len(self.gateway.submitted), 1)
        self.gateway.assets[0].update(availableOpen=100, availableBalance=100)
        await margin_emergency_job(self.app)
        self.gateway.assets[0].update(availableOpen=0)
        await margin_emergency_job(self.app)
        self.assertEqual(len(self.gateway.submitted), 2)

    async def test_assets_preserved_zero_not_replaced_and_no_spot_collateral(self):
        self.gateway.assets[0]["availableOpen"] = 0
        self.gateway.assets.append({"currency": "ETH", "equity": 2, "availableBalance": 1})
        bal = await self.client.get_futures_balance()
        self.assertEqual(available_margin(bal), 0)
        self.assertEqual(available_margin(bal, "ETH"), 1)
        self.assertEqual(bal["total"]["ETH"], 2)
        with self.assertRaises(ValueError):
            available_margin(bal, "BTC")
        from bot.handlers.balance import _build_balance_text
        text = _build_balance_text(bal, [], {}, {}, {}, None,
            {"realized_pnl": -2.8155, "closes": 2, "unknown_pnl": 1}, spot_raw={"total": {"ETH": 3}})
        self.assertIn("нет цены: ETH", text)
        self.assertIn("2 сд.", text)
        self.assertIn("неизвестен: 1", text)
        bal["free"]["USDT"] = -194.81
        text = _build_balance_text(bal, [{"symbol": "BTC/USDT:USDT", "position_id": "manual"}],
            {}, {}, {}, Config(), {"closes": 0})
        self.assertNotIn("availableBalance", text)
        self.assertIn("Сегодня (бот)", text)
        self.assertNotIn("дефицит", text)
        self.assertNotIn("Требуется маржи", text)
        from bot.handlers.balance import _value_assets
        self.assertEqual(_value_assets({"total": {"BTC": 2, "USDT": -10}},
            {"BTC": 100, "USDT": 1}), "≈ 190.00 USDT")
        self.assertEqual(_value_assets({"total": {"BTC": 2}}, {}), "нет цены: BTC")
        for equity in (0, -20):
            snapshot = {"total": {"USDT": equity, "ETH": 2}, "_prices": {"USDT": 1, "ETH": 100},
                "_assets": {"USDT": {"equity": equity, "debtAmount": 20, "contributeMarginAmount": -20},
                            "ETH": {"equity": 2, "contributeMarginAmount": 180}}}
            summary = _build_balance_text(snapshot, [], {}, {}, {}, None, {}, spot_raw={"total": {"USDT": 5}})
            self.assertIn("Всего: ≈ 185.00 USDT", summary)
            self.assertIn("Фьючерсы: ≈ 180.00 USDT", summary)
            self.assertIn("Спот: ≈ 5.00 USDT", summary)
            self.assertIn("Обеспечение MEXC: 160.00 USDT", summary)
            self.assertIn("Свободная маржа: нет подтверждённых данных", summary)

    async def test_close_reason_from_exact_filled_order_not_trigger_or_ticker(self):
        for reason in ("tp", "sl", "profit_lock", "manual", "manual_reentry", "liquidation", "adl", "unknown"):
            self.gateway.plans = []
            with db._connect() as conn:
                conn.execute("DELETE FROM bot_orders")
            self.closed_history(reason=reason)
            result = await self.client.get_closed_position_result(self.record)
            self.assertEqual(result["reason"], reason)
            self.assertEqual(result["pnl"], 1.5379)
            self.assertEqual(result["exit_price"], 92.123)

    async def test_other_position_history_cannot_close_current_position(self):
        self.closed_history(pid="other")
        self.assertIsNone(await self.client.get_closed_position_result(self.record))

    async def test_closed_once_stats_history_and_message_share_pnl(self):
        self.closed_history(pnl=-6.0614)
        await reconcile_closures(self.app)
        await reconcile_closures(self.app)
        stats = db.get_daily_stats(dt.datetime.now(dt.timezone.utc).date().isoformat())
        self.assertEqual(stats["closes"], 1)
        self.assertEqual(stats["realized_pnl"], -6.0614)
        self.assertEqual(stats["losses"], 1)
        self.assertEqual(stats["wins"], 0)  # TP label does not imply a profit.
        self.assertEqual(db.get_last_position_history(SYMBOL)["pnl"], -6.0614)
        self.assertEqual(self.app.bot.send_message.await_count, 1)
        self.assertIn("-6.0614", self.app.bot.send_message.call_args.kwargs["text"])

    async def test_unknown_pnl_not_zero_and_no_reentry(self):
        self.closed_history(reason="unknown", pnl=None)
        db.upsert_reentry(SYMBOL, "short", 10, 10, 100, 100, position_key=self.record["id"])
        await reentry_job(self.app)
        self.assertIsNone(db.get_closure(self.record["id"])["pnl"])
        self.assertEqual(db.get_daily_stats()["unknown_pnl"], 1)
        self.assertIsNone(db.get_reentry(SYMBOL))
        self.assertEqual(self.gateway.submitted, [])

    async def test_reentry_disabled_exhausted_and_sl_disallowed(self):
        self.closed_history(reason="sl", pnl=-2.8155)
        for maximum, cycle, allow in ((0, 0, True), (2, 2, True), (2, 0, False)):
            self.config.reentry_on_sl = allow
            db.upsert_reentry(SYMBOL, "short", 10, 10, 100, 100, max_cycles=maximum,
                             cycle_count=cycle, position_key=self.record["id"])
            await reentry_job(self.app)
            self.assertIsNone(db.get_reentry(SYMBOL))
        self.assertEqual(db.get_daily_stats()["closes"], 1)

    async def test_reentry_sl_allowed_has_loss_label_and_no_double_pnl(self):
        self.closed_history(reason="sl", pnl=-2.8155)
        self.config.reentry_on_sl = True
        self.config.reentry_sl_cooldown_min = 1
        db.upsert_reentry(SYMBOL, "sell", 10, 10, 100, 100, position_key=self.record["id"])
        opened = AsyncMock(return_value={"margin": 10, "entry_price": 100, "protection_status": "confirmed"})
        with patch("bot.handlers.trading.execute_open", opened):
            await reentry_job(self.app)
        opened.assert_awaited_once()
        self.assertEqual(opened.call_args.kwargs["cycle_count"], 1)
        text = self.app.bot.send_message.call_args.kwargs["text"]
        self.assertIn("sl", text)
        self.assertIn("-2.8155", text)
        self.assertNotIn("в прибыль", text)
        self.assertEqual(db.get_daily_stats()["realized_pnl"], -2.8155)

    async def test_new_manual_position_drops_old_reentry(self):
        db.upsert_reentry(SYMBOL, "short", 10, 10, 100, 100, position_key=self.record["id"])
        self.gateway.live = [position(pid="manual-next")]
        await reentry_job(self.app)
        self.assertIsNone(db.get_reentry(SYMBOL))
        self.assertEqual(self.gateway.submitted, [])

    async def test_close_submission_does_not_claim_execution(self):
        from bot.handlers.trading import _do_close
        self.gateway.live[0]["mark_price"] = 999  # must never be written as a fill price
        context = SimpleNamespace(bot_data=self.app.bot_data)
        await _do_close(self.client, context, SYMBOL, False)
        self.assertEqual(db.get_position_by_id(self.record["id"])["status"], "closing")
        self.assertIsNone(db.get_closure(self.record["id"]))
        self.assertEqual(db.get_daily_stats()["closes"], 0)

    async def test_stale_position_cannot_be_closed_or_averaged(self):
        self.gateway.live = [position(pid="replacement")]
        with self.assertRaises(ValueError):
            await self.client.partial_close_futures_position(SYMBOL, 10, expected_position_id="100")
        with self.assertRaises(ValueError):
            await self.client.close_futures_position(SYMBOL, expected_position_id="100")
        with self.assertRaises(ValueError):
            await self.client.place_futures_order(SYMBOL, "sell", 1, 10, expected_position_id="100")
        self.assertEqual(self.gateway.submitted, [])

    async def test_adoption_requires_exact_confirmation_id(self):
        self.gateway.live = [position(pid="manual-next")]
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        context = SimpleNamespace(args=["ALLO"], bot_data=self.app.bot_data, application=self.app)
        await adopt_handler(update, context)
        self.assertIsNone(db.get_managed_position(self.gateway.live[0]))
        context.args = ["ALLO", "confirm", "100"]
        await adopt_handler(update, context)
        self.assertIsNone(db.get_managed_position(self.gateway.live[0]))
        context.args = ["ALLO", "confirm", "manual-next"]
        await adopt_handler(update, context)
        self.assertIsNotNone(db.get_managed_position(self.gateway.live[0]))

    async def test_unauthorized_update_is_stopped(self):
        from bot.lifecycle import authorize_update
        from telegram.ext import ApplicationHandlerStop
        context = SimpleNamespace(bot_data=self.app.bot_data)
        with self.assertRaises(ApplicationHandlerStop):
            await authorize_update(SimpleNamespace(effective_user=SimpleNamespace(id=999)), context)
        await authorize_update(SimpleNamespace(effective_user=SimpleNamespace(id=1)), context)

    async def test_migration_is_repeatable_preserves_config(self):
        db.set_config("test-only", "value")
        db.init_db()
        self.assertEqual(db.get_config("test-only"), "value")
        self.assertEqual(db.get_managed_position(position())["id"], self.record["id"])

    async def test_open_registers_order_position_before_protection_failure(self):
        from bot.handlers.trading import execute_open
        self.gateway.live = []
        self.gateway.fail_kind = "SL"
        with patch("bot.jobs.main._get_btc_rsi_4h", new=AsyncMock(return_value=None)):
            result = await execute_open(self.client, self.app, SYMBOL, "sell", 10, 10, 100, 100)
        managed = db.get_managed_position(self.gateway.live[0])
        self.assertEqual(managed["exchange_position_id"], "bot-next")
        self.assertEqual(db.get_reentry(SYMBOL)["position_key"], managed["id"])
        self.assertEqual(result["tp_price"], 0)
        self.assertEqual(result["sl_price"], 0)
        self.assertEqual(result["protection_status"], "не подтверждена")
        self.assertIn("НЕ подтверждена", self.app.bot.send_message.call_args.kwargs["text"])

    async def test_long_protection_direction_and_factual_leverage(self):
        self.gateway.live = [position(pid="long-next", side="long", leverage=20)]
        register_position(self.gateway.live[0], self.config, tp_pct=100, sl_pct=100)
        self.config.default_leverage = 100
        await tpsl_enforce_job(self.app)
        self.assertEqual([(p["side"], p["triggerType"], float(p["triggerPrice"])) for p in self.gateway.placed],
                         [(4, 2, 95), (4, 1, 105)])

    async def test_orphan_sweep_keeps_live_protection_and_cancels_only_old_ids(self):
        self.saved_plan("TP")
        await tpsl_enforce_job(self.app)
        self.assertNotIn("saved", self.gateway.cancelled)
        self.gateway.live = [position(pid="new-manual")]
        self.gateway.plans.append({**self.gateway.plans[0], "id": "manual"})
        await tpsl_enforce_job(self.app)
        self.assertIn("saved", self.gateway.cancelled)
        self.assertNotIn("manual", self.gateway.cancelled)

    async def test_failed_position_snapshot_cannot_trigger_cleanup(self):
        self.saved_plan("TP")
        self.client.get_positions.side_effect = RuntimeError("exchange unavailable")
        await tpsl_enforce_job(self.app)
        await reentry_job(self.app)
        self.assertEqual(self.gateway.cancelled, [])
        self.assertEqual(db.get_position_by_id(self.record["id"])["status"], "open")

    async def test_concurrent_enforcement_does_not_duplicate_legs(self):
        await asyncio.gather(self.client.set_tp_sl(SYMBOL, 90, 110), self.client.set_tp_sl(SYMBOL, 90, 110))
        self.assertEqual(len(self.gateway.placed), 2)

    async def test_profit_lock_reentry_cooldown_survives_restart(self):
        self.closed_history(reason="profit_lock")
        self.gateway.closed[0]["updateTime"] = NOW - 30_000
        self.gateway.history[0]["updateTime"] = NOW - 30_000
        db.upsert_reentry(SYMBOL, "short", 10, 10, 100, 100, position_key=self.record["id"])
        opened = AsyncMock(return_value={"margin": 10, "entry_price": 100, "protection_status": "confirmed"})
        with patch("bot.handlers.trading.execute_open", opened):
            with patch("bot.jobs.main.time.time", return_value=NOW / 1000):
                await reentry_job(self.app)
                self.app.bot_data = {"config": self.config, "exchange": self.client}
                await reentry_job(self.app)
                opened.assert_not_awaited()
            with patch("bot.jobs.main.time.time", return_value=NOW / 1000 + 31):
                await reentry_job(self.app)
            opened.assert_awaited_once()
        self.assertIn("PnL:", self.app.bot.send_message.call_args.kwargs["text"])

    async def test_delayed_pnl_updates_same_closure_without_second_notification(self):
        self.closed_history(reason="unknown", pnl=None)
        await reconcile_closures(self.app)
        self.gateway.closed[0]["realised"] = -2.8155
        await reconcile_closures(self.app)
        stats = db.get_daily_stats()
        self.assertEqual(stats["closes"], 1)
        self.assertEqual(stats["unknown_pnl"], 0)
        self.assertEqual(stats["realized_pnl"], -2.8155)
        self.assertEqual(self.app.bot.send_message.await_count, 1)

    async def test_three_positions_same_symbol_recorded_separately(self):
        expected = (-6.0614, -2.8155, 1.5379)
        for index, pnl in enumerate(expected):
            pid = str(100 + index)
            self.gateway.live = [position(pid=pid)]
            self.record = register_position(self.gateway.live[0], self.config)
            self.closed_history(pnl=pnl, pid=pid, order_id=f"fill-{index}")
            await reconcile_closures(self.app)
        stats = db.get_daily_stats()
        self.assertEqual(stats["closes"], 3)
        self.assertAlmostEqual(stats["realized_pnl"], sum(expected))
        self.assertEqual(self.app.bot.send_message.await_count, 3)

    async def test_close_timeout_cannot_be_resubmitted(self):
        self.gateway.contractPrivatePostOrderSubmit = AsyncMock(side_effect=TimeoutError("unknown"))
        with self.assertRaises(TimeoutError):
            await self.client.close_futures_position(SYMBOL, expected_position_id="100")
        with self.assertRaises(ValueError):
            await self.client.close_futures_position(SYMBOL, expected_position_id="100")
        self.assertEqual(self.gateway.contractPrivatePostOrderSubmit.await_count, 1)

    async def test_single_contract_trim_reports_actual_percentage(self):
        self.gateway.live[0]["contracts"] = 1
        self.gateway.assets[0]["availableOpen"] = 0
        await margin_emergency_job(self.app)
        self.assertEqual(self.gateway.submitted[0]["vol"], 1)
        self.assertIn("100.0%", self.app.bot.send_message.call_args.kwargs["text"])

    async def test_margin_controls_validate_and_show_separate_values(self):
        from bot.handlers.trading import _validate_margin_setting, _build_avg_text
        for value in (float("nan"), float("inf"), -1, 101):
            with self.assertRaises(ValueError):
                _validate_margin_setting("margin_emergency_threshold_pct", value)
        with self.assertRaises(ValueError):
            _validate_margin_setting("margin_emergency_trim_pct", 0)
        text = _build_avg_text(self.config)
        self.assertIn("Порог доступной маржи", text)
        self.assertIn("Размер сокращения", text)

    async def test_standby_never_imports_or_starts_bot(self):
        import start
        with patch.dict("os.environ", {"KRABS_RUN_MODE": "standby"}), patch.object(start, "serve_standby") as serve:
            with patch.object(start, "_acquire_pid_lock", side_effect=AssertionError("BOT STARTED")):
                start.run()
            serve.assert_called_once()

    async def test_position_display_does_not_claim_calculated_protection_is_active(self):
        from bot.pos_format import format_position_block
        text = format_position_block(position(), self.record, None, self.config, {})
        self.assertIn("Уровни расчётные", text)
        text = format_position_block(position(pid="manual"), self.record, None, self.config, {})
        self.assertIn("не под управлением", text)
        self.assertNotIn("Цель SL", text)

    async def test_non_usdt_assets_are_visible_but_unsupported_contract_not_adopted(self):
        with self.assertRaises(ValueError):
            register_position(position(pid="inverse", settle_currency="ETH"), self.config)

    async def test_reused_id_with_different_open_time_is_not_ownership(self):
        self.assertIsNone(db.get_managed_position(position(opened_at_ms=NOW)))

    async def test_ambiguous_last_fill_is_unknown_not_arbitrary(self):
        self.closed_history(reason="tp")
        self.gateway.history.append({**self.gateway.history[0], "orderId": "another-fill", "category": 2})
        result = await self.client.get_closed_position_result(self.record)
        self.assertEqual(result["reason"], "unknown")

    async def test_unregistered_open_blocks_another_submission(self):
        self.gateway.live = []
        await self.client.place_futures_order(SYMBOL, "sell", 10, 10)
        self.gateway.live = []  # position snapshot has not caught up yet
        with self.assertRaisesRegex(RuntimeError, "Previous opening outcome"):
            await self.client.place_futures_order(SYMBOL, "sell", 10, 10)
        self.assertEqual(len(self.gateway.submitted), 1)


    async def test_protection_audit_is_read_only_and_checks_owned_side_volume(self):
        self.saved_plan("TP")
        self.saved_plan("SL", oid="wrong", side=1, vol=0)
        result = await self.client.audit_protection(SYMBOL)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(result["legs"], {"TP": ["saved"], "SL": []})
        self.assertEqual(self.gateway.placed + self.gateway.cancelled, [])
        self.gateway.live = [position(pid="manual")]
        self.assertEqual((await self.client.audit_protection(SYMBOL))["status"], "UNMANAGED")

    async def test_repair_rejects_changed_position_volume_or_settings_before_mutation(self):
        audit = await self.client.audit_protection(SYMBOL)
        for changed in (position(pid="replacement"), position(contracts=101), position(leverage=20)):
            self.gateway.live = [changed]
            with self.assertRaises(ValueError):
                await self.client.set_tp_sl(SYMBOL, tp_price=90, sl_price=110,
                                           expected_snapshot=audit["snapshot"])
        self.gateway.live = [position()]
        with db._connect() as conn:
            conn.execute("UPDATE positions SET locked_sl=95 WHERE id=?", (self.record["id"],))
        with self.assertRaises(ValueError):
            await self.client.set_tp_sl(SYMBOL, tp_price=90, sl_price=110,
                                       expected_snapshot=audit["snapshot"])
        self.assertEqual(self.gateway.placed + self.gateway.cancelled, [])

    async def test_repair_preview_confirm_once_preserves_tp_and_manual_orders(self):
        from bot.handlers.protection import repair_tpsl_handler, repair_tpsl_callback
        self.saved_plan("TP")
        self.gateway.plans.append({**self.gateway.plans[0], "id": "manual"})
        context = SimpleNamespace(args=[SYMBOL], bot_data=self.app.bot_data, user_data={})
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        await repair_tpsl_handler(update, context)
        self.assertEqual(self.gateway.placed, [])
        nonce = context.user_data["protection_repair"]["nonce"]
        update.callback_query = SimpleNamespace(data=f"repair_tpsl_{nonce}", answer=AsyncMock(), edit_message_text=AsyncMock())
        await repair_tpsl_callback(update, context)
        await repair_tpsl_callback(update, context)
        self.assertEqual(len(self.gateway.placed), 1)
        self.assertEqual(self.gateway.placed[0]["triggerType"], 1)
        self.assertEqual(self.gateway.cancelled, [])
        self.assertEqual((await self.client.audit_protection(SYMBOL))["status"], "CONFIRMED")

    async def test_repair_partial_error_never_reports_success(self):
        from bot.handlers.protection import repair_tpsl_handler, repair_tpsl_callback
        self.gateway.fail_kind = "SL"
        context = SimpleNamespace(args=[SYMBOL], bot_data=self.app.bot_data, user_data={})
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        await repair_tpsl_handler(update, context)
        nonce = context.user_data["protection_repair"]["nonce"]
        update.callback_query = SimpleNamespace(data=f"repair_tpsl_{nonce}", answer=AsyncMock(), edit_message_text=AsyncMock())
        await repair_tpsl_callback(update, context)
        self.assertIn("не подтверждено", update.callback_query.edit_message_text.call_args.args[0])
        self.assertNotIn("protection_repair", context.user_data)

    async def test_repair_expired_confirmation_never_mutates(self):
        from bot.handlers.protection import repair_tpsl_callback
        context = SimpleNamespace(bot_data=self.app.bot_data,
            user_data={"protection_repair": {"nonce": "old", "expires": 0}})
        query = SimpleNamespace(data="repair_tpsl_old", answer=AsyncMock(), edit_message_text=AsyncMock())
        await repair_tpsl_callback(SimpleNamespace(callback_query=query), context)
        self.assertEqual(self.gateway.placed + self.gateway.cancelled, [])


if __name__ == "__main__":
    unittest.main()

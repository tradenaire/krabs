from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.jobs.main import reentry_job, tpsl_enforce_job
from tests.tg_harness import FakeConfig, FakeFuturesClient


def _history(symbol: str = "HYPE/USDT:USDT") -> dict:
    return {
        "symbol": symbol,
        "side": "short",
        "entry_price": 10.0,
        "total_invested": 20.0,
        "initial_margin": 20.0,
        "leverage": 10,
        "opened_at": "2026-06-09T00:00:00",
    }


class ClosedClient(FakeFuturesClient):
    async def get_positions(self):
        return []

    async def get_futures_balance(self):
        return {"_raw": {"availableOpen": 1000}, "free": {"USDT": 1000}}

    async def cancel_plan_orders(self, symbol):
        return 0


class TradeJobDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def _app(self, config=None):
        return SimpleNamespace(
            bot_data={
                "exchange": ClosedClient(),
                "config": config or FakeConfig(),
            }
        )

    async def test_reentry_job_tp_with_reentry_disabled_uses_detailed_close_message(self):
        app = self._app()
        notifications: list[str] = []

        async def notify(_app, text, reply_markup=None):
            notifications.append(text)

        with patch("bot.db.get_all_reentry", return_value=[{
            "symbol": "HYPE/USDT:USDT",
            "side": "sell",
            "margin": 20.0,
            "leverage": 10,
            "cycle_count": 0,
            "max_cycles": 0,
        }]), \
            patch("bot.db.get_last_position_history", return_value=_history()), \
            patch("bot.jobs.main._resolve_close_reason", return_value=(True, False, 9.5)), \
            patch("bot.db.close_position"), \
            patch("bot.db.log_trade"), \
            patch("bot.db.close_position_history"), \
            patch("bot.db.delete_reentry"), \
            patch("bot.jobs.main._notify_all", notify):
            await reentry_job(app)

        self.assertEqual(len(notifications), 1)
        user_text = notifications[0]
        self.assertIn("Причина: тейк-профит", user_text)
        self.assertIn("PnL:", user_text)
        self.assertIn("+$10.00", user_text)
        self.assertIn("Перезаход: нет", user_text)
        self.assertIn("перезаход отключён", user_text)

    async def test_reentry_job_sl_with_cycles_exhausted_uses_detailed_close_message(self):
        app = self._app()
        notifications: list[str] = []

        async def notify(_app, text, reply_markup=None):
            notifications.append(text)

        with patch("bot.db.get_all_reentry", return_value=[{
            "symbol": "HYPE/USDT:USDT",
            "side": "sell",
            "margin": 20.0,
            "leverage": 10,
            "cycle_count": 3,
            "max_cycles": 3,
        }]), \
            patch("bot.db.get_last_position_history", return_value=_history()), \
            patch("bot.jobs.main._resolve_close_reason", return_value=(False, False, 10.5)), \
            patch("bot.db.close_position"), \
            patch("bot.db.log_trade"), \
            patch("bot.db.close_position_history"), \
            patch("bot.db.delete_reentry"), \
            patch("bot.jobs.main._notify_all", notify):
            await reentry_job(app)

        self.assertEqual(len(notifications), 1)
        user_text = notifications[0]
        self.assertIn("Причина: стоп-лосс", user_text)
        self.assertIn("PnL:", user_text)
        self.assertIn("-$10.00", user_text)
        self.assertIn("Перезаход: нет", user_text)
        self.assertIn("циклы исчерпаны", user_text)

    async def test_reentry_job_profit_lock_sl_wait_uses_detailed_close_message(self):
        app = self._app()
        notifications: list[str] = []

        async def notify(_app, text, reply_markup=None):
            notifications.append(text)

        with patch("bot.db.get_all_reentry", return_value=[{
            "symbol": "HYPE/USDT:USDT",
            "side": "sell",
            "margin": 20.0,
            "leverage": 10,
            "cycle_count": 0,
            "max_cycles": 3,
        }]), \
            patch("bot.db.get_last_position_history", return_value=_history()), \
            patch("bot.jobs.main._resolve_close_reason", return_value=(False, True, 9.5)), \
            patch("bot.db.get_open_position", return_value=None), \
            patch("bot.jobs.main._notify_all", notify):
            await reentry_job(app)

        self.assertEqual(len(notifications), 1)
        user_text = notifications[0]
        self.assertIn("Причина: профит-локк SL", user_text)
        self.assertIn("+$10.00", user_text)
        self.assertIn("Перезаход: да", user_text)
        self.assertIn("Пауза 1м", user_text)

    async def test_reentry_job_loss_sl_without_reentry_uses_detailed_close_message(self):
        app = self._app(FakeConfig(reentry_on_sl=False))
        notifications: list[str] = []

        async def notify(_app, text, reply_markup=None):
            notifications.append(text)

        with patch("bot.db.get_all_reentry", return_value=[{
            "symbol": "HYPE/USDT:USDT",
            "side": "sell",
            "margin": 20.0,
            "leverage": 10,
            "cycle_count": 0,
            "max_cycles": 3,
        }]), \
            patch("bot.db.get_last_position_history", return_value=_history()), \
            patch("bot.jobs.main._resolve_close_reason", return_value=(False, False, 10.5)), \
            patch("bot.db.get_open_position", return_value=None), \
            patch("bot.db.delete_reentry"), \
            patch("bot.jobs.main._notify_all", notify):
            await reentry_job(app)

        self.assertEqual(len(notifications), 1)
        user_text = notifications[0]
        self.assertIn("Причина: стоп-лосс", user_text)
        self.assertIn("-$10.00", user_text)
        self.assertIn("Перезаход: нет", user_text)
        self.assertIn("reentry_on_sl выключен", user_text)

    async def test_reentry_job_loss_sl_with_reentry_wait_uses_detailed_close_message(self):
        app = self._app(FakeConfig(reentry_on_sl=True, reentry_sl_cooldown_min=7))
        notifications: list[str] = []

        async def notify(_app, text, reply_markup=None):
            notifications.append(text)

        with patch("bot.db.get_all_reentry", return_value=[{
            "symbol": "HYPE/USDT:USDT",
            "side": "sell",
            "margin": 20.0,
            "leverage": 10,
            "cycle_count": 0,
            "max_cycles": 3,
        }]), \
            patch("bot.db.get_last_position_history", return_value=_history()), \
            patch("bot.jobs.main._resolve_close_reason", return_value=(False, False, 10.5)), \
            patch("bot.db.get_open_position", return_value=None), \
            patch("bot.jobs.main._notify_all", notify):
            await reentry_job(app)

        self.assertEqual(len(notifications), 1)
        user_text = notifications[0]
        self.assertIn("Причина: стоп-лосс", user_text)
        self.assertIn("-$10.00", user_text)
        self.assertIn("Перезаход: да", user_text)
        self.assertIn("Пауза 7м", user_text)

    async def test_reentry_job_successful_reentry_announces_close_details_and_new_entry(self):
        app = self._app()
        notifications: list[str] = []

        async def notify(_app, text, reply_markup=None):
            notifications.append(text)

        async def open_again(*args, **kwargs):
            return {
                "symbol": "HYPE/USDT:USDT",
                "entry_price": 9.4,
                "leverage": 10,
                "tp_price": 8.9,
                "sl_price": 9.9,
            }

        with patch("bot.db.get_all_reentry", return_value=[{
            "symbol": "HYPE/USDT:USDT",
            "side": "sell",
            "margin": 20.0,
            "leverage": 10,
            "cycle_count": 0,
            "max_cycles": 3,
        }]), \
            patch("bot.db.get_last_position_history", return_value=_history()), \
            patch("bot.jobs.main._resolve_close_reason", return_value=(True, False, 9.5)), \
            patch("bot.db.get_open_position", return_value=None), \
            patch("bot.handlers.trading.execute_open", open_again), \
            patch("bot.db.increment_reentry_cycle", return_value=1), \
            patch("bot.db.log_trade"), \
            patch("bot.jobs.main._save_exhausted"), \
            patch("bot.jobs.main._notify_all", notify):
            await reentry_job(app)

        self.assertEqual(len(notifications), 1)
        user_text = notifications[0]
        self.assertIn("Причина: тейк-профит", user_text)
        self.assertIn("+$10.00", user_text)
        self.assertIn("Перезаход: да", user_text)
        self.assertIn("Entry нового входа", user_text)
        self.assertIn("9.4", user_text)

    async def test_tpsl_enforce_external_close_uses_detailed_unknown_pnl_message(self):
        class NoPositionsClient(FakeFuturesClient):
            async def get_positions(self):
                return []

            async def cancel_tp_sl_orders(self, symbol):
                return 0

            async def get_tp_sl_orders(self):
                return []

        app = SimpleNamespace(bot_data={"exchange": NoPositionsClient(), "config": FakeConfig()})
        notifications: list[str] = []

        async def notify(_app, text, reply_markup=None):
            notifications.append(text)

        with patch("bot.db.get_open_positions", return_value=[{"symbol": "HYPE/USDT:USDT"}]), \
            patch("bot.db.get_all_reentry", return_value=[]), \
            patch("bot.db.close_position"), \
            patch("bot.db.close_position_history"), \
            patch("bot.db.get_last_position_history", return_value=_history()), \
            patch("bot.jobs.main._notify_all", notify):
            await tpsl_enforce_job(app)

        self.assertEqual(len(notifications), 1)
        user_text = notifications[0]
        self.assertIn("Причина: позиция исчезла с биржи", user_text)
        self.assertIn("PnL: `неизвестно`", user_text)
        self.assertIn("Перезаход: нет", user_text)
        self.assertIn("нельзя честно определить", user_text)


if __name__ == "__main__":
    unittest.main()

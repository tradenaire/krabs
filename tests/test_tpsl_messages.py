import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.assistant import assistant_handler
from bot.handlers.trading import _reapply_tpsl_all, format_tpsl_results


class TpslMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_nlp_open_reports_actual_margin_and_protection_status(self):
        for confirmed in (False, True):
            message = SimpleNamespace(text="открой BTC", chat=SimpleNamespace(type="private"),
                                      reply_text=AsyncMock())
            config = SimpleNamespace(default_trade_usdt=1, default_leverage=10, tp_pct=500, sl_pct=500)
            context = SimpleNamespace(user_data={}, bot_data={
                "config": config, "exchange": SimpleNamespace(futures_symbol=lambda _: "BTC/USDT:USDT")},
                application=SimpleNamespace())
            result = {"entry_price": 100, "leverage": 10, "margin": 2,
                      "tp_price": 95, "sl_price": 105,
                      "protection_status": "TP и SL подтверждены" if confirmed else "не подтверждена"}
            with patch("bot.handlers.trading.execute_open", new=AsyncMock(return_value=result)):
                await assistant_handler(SimpleNamespace(message=message), context)
            text = message.reply_text.await_args.args[0]
            self.assertIn("Маржа: `$2.00`", text)
            if confirmed:
                self.assertIn("TP `95` | SL `105` подтверждены", text)
                self.assertNotIn("+500%", text)
            else:
                self.assertIn("TP/SL НЕ подтверждены", text)
                self.assertNotIn("✅ TP", text)

    async def test_reapply_reports_confirmed_and_failed_positions(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[
                {"symbol": "BTC/USDT:USDT", "entry_price": 100, "leverage": 10, "side": "short"},
                {"symbol": "ETH/USDT:USDT", "entry_price": 100, "leverage": 10, "side": "short"},
            ]),
            set_tp_sl=AsyncMock(side_effect=[
                [
                    {"type": "TP", "price": 95, "id": "tp-1", "confirmed": True},
                    {"type": "SL", "price": 105, "id": "sl-1", "confirmed": True},
                ],
                RuntimeError("exchange timeout"),
            ]),
        )
        app = SimpleNamespace(bot_data={"exchange": client, "tp_sl_pcts": {}})
        config = SimpleNamespace(tp_pct=500, sl_pct=500)
        conn = SimpleNamespace(execute=lambda *args: None)

        with patch("bot.db.get_managed_position", side_effect=lambda pos: {"id": pos["symbol"]}), \
             patch("bot.db.update_position_tpsl") as update, \
             patch("bot.db._connect") as connect:
            connect.return_value.__enter__.return_value = conn
            results = await _reapply_tpsl_all(app, config)

        self.assertEqual([r["ok"] for r in results], [True, False])
        self.assertEqual(results[0]["tp_price"], 95)
        self.assertEqual(results[0]["sl_price"], 105)
        self.assertEqual(results[0]["tp_id"], "tp-1")
        self.assertEqual(results[0]["sl_id"], "sl-1")
        update.assert_called_once()
        text = format_tpsl_results(results)
        self.assertIn("BTC", text)
        self.assertIn("95", text)
        self.assertIn("105", text)
        self.assertIn("tp-1", text)
        self.assertIn("sl-1", text)
        self.assertIn("подтверждены", text)
        self.assertIn("ETH", text)
        self.assertIn("НЕ подтверждена", text)

    async def test_assistant_does_not_claim_tpsl_applied_after_rejection(self):
        message = SimpleNamespace(
            text="500",
            chat=SimpleNamespace(type="private"),
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(message=message)
        context = SimpleNamespace(
            args=[],
            user_data={"pending_set": "tp"},
            bot_data={"config": SimpleNamespace(tp_pct=500, sl_pct=500)},
            application=SimpleNamespace(),
        )
        rejected = [{"symbol": "BTC/USDT:USDT", "ok": False, "error": "exchange timeout"}]
        with patch("bot.db.set_config"), patch(
            "bot.handlers.trading._reapply_tpsl_all",
            new=AsyncMock(return_value=rejected),
        ):
            await assistant_handler(update, context)

        text = message.reply_text.await_args.args[0]
        self.assertIn("сохранён", text)
        self.assertIn("НЕ подтверждена", text)
        self.assertNotIn("применён", text)

    async def test_assistant_persists_direct_confirmed_tpsl(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[
                {"symbol": "BTC/USDT:USDT", "entry_price": 100, "leverage": 10, "side": "short"},
            ]),
            set_tp_sl=AsyncMock(return_value=[
                {"type": "TP", "price": 95, "id": "tp-direct", "confirmed": True},
                {"type": "SL", "price": 105, "id": "sl-direct", "confirmed": True},
            ]),
        )
        message = SimpleNamespace(
            text="тп 10% сл 20%",
            chat=SimpleNamespace(type="private"),
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(message=message)
        context = SimpleNamespace(
            args=[],
            user_data={},
            bot_data={"exchange": client, "tp_sl_pcts": {}},
            application=SimpleNamespace(),
        )
        conn = SimpleNamespace(execute=lambda *args: None)
        with patch("bot.db.update_position_tpsl") as update_tpsl, patch("bot.db._connect") as connect:
            connect.return_value.__enter__.return_value = conn
            await assistant_handler(update, context)

        update_tpsl.assert_called_once_with("BTC/USDT:USDT", 10.0, 20.0)
        self.assertIn("95", message.reply_text.await_args.args[0])
        self.assertIn("105", message.reply_text.await_args.args[0])
        self.assertIn("tp-direct", message.reply_text.await_args.args[0])
        self.assertIn("sl-direct", message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()

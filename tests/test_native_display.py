import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.positions import _format_native_protection, _send_positions


def _position(**overrides):
    position = {
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "position_id": 42,
        "leverage": 10,
        "percentage": 1,
        "unrealized_pnl": 0,
    }
    position.update(overrides)
    return position


def _native(**overrides):
    row = {
        "symbol": "BTC_USDT",
        "positionId": 42,
        "positionType": 1,
        "state": 1,
        "isFinished": 0,
        "id": "stop-1",
        "orderId": 0,
        "takeProfitPrice": 105,
        "stopLossPrice": 95,
        "vol": 0,
        "volType": 1,
    }
    row.update(overrides)
    return row


class NativeDisplayTests(unittest.IsolatedAsyncioTestCase):
    def test_matches_position_identity_and_keeps_zero_volume(self):
        text = _format_native_protection(_position(), [_native()])
        self.assertIn("TP `105` | SL `95` | id `stop-1`", text)
        self.assertIn("Объём покрытия не подтверждён", text)
        self.assertIn("id `stop-1`", text)

    def test_excludes_wrong_id_side_symbol_and_inactive_rows(self):
        rows = [
            _native(positionId=99),
            _native(positionType=2),
            _native(symbol="ETH_USDT"),
            _native(state=2),
            _native(isFinished=1),
        ]
        self.assertIn("активные native записи не найдены", _format_native_protection(_position(), rows))

    def test_fetch_failure_is_explicit(self):
        self.assertIn("native TP/SL данные недоступны", _format_native_protection(_position(), None))

    def test_prices_are_not_rounded_and_unknown_side_never_matches(self):
        row = _native(takeProfitPrice='0.123456789123', stopLossPrice='NaN')
        text = _format_native_protection(_position(), [row])
        self.assertIn('TP `0.123456789123`', text)
        self.assertIn('SL `нет данных`', text)
        self.assertIn('записи не найдены', _format_native_protection(_position(side='unknown'), [row]))

    async def test_positions_fetch_native_rows_once_for_all_positions(self):
        positions = [_position(), _position(symbol="ETH/USDT:USDT", position_id=43, side="short")]
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=positions),
            get_native_stop_orders=AsyncMock(return_value=[_native()]),
            get_max_leverage=AsyncMock(return_value=20),
            get_position_limit_usdt=AsyncMock(return_value=100),
            get_funding_rate=AsyncMock(return_value={"rate": 0}),
        )
        message = SimpleNamespace(reply_text=AsyncMock(), edit_text=AsyncMock())
        context = SimpleNamespace(bot_data={"exchange": client, "tp_sl_pcts": {}})
        with patch("bot.db.get_managed_position", return_value=None), \
             patch("bot.db.get_all_reentry", return_value=[]), \
             patch("bot.pos_format.format_position_block", return_value="POSITION"):
            await _send_positions(message, context)

        client.get_native_stop_orders.assert_awaited_once_with()
        text = message.reply_text.await_args.args[0]
        self.assertEqual(text.count("Native TP/SL"), 2)
        self.assertIn("активные native записи не найдены", text)

    async def test_native_fetch_failure_is_shown_in_positions(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[_position()]),
            get_native_stop_orders=AsyncMock(side_effect=TimeoutError()),
            get_max_leverage=AsyncMock(return_value=20),
            get_position_limit_usdt=AsyncMock(return_value=100),
            get_funding_rate=AsyncMock(return_value={"rate": 0}),
        )
        message = SimpleNamespace(reply_text=AsyncMock(), edit_text=AsyncMock())
        context = SimpleNamespace(bot_data={"exchange": client, "tp_sl_pcts": {}})
        with patch("bot.db.get_managed_position", return_value=None), \
             patch("bot.db.get_all_reentry", return_value=[]), \
             patch("bot.pos_format.format_position_block", return_value="POSITION"):
            await _send_positions(message, context)

        self.assertIn("native TP/SL данные недоступны", message.reply_text.await_args.args[0])

    async def test_bulk_funding_snapshot_skips_per_symbol_fetch(self):
        positions = [_position(funding_rate=0.000123)]
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=positions),
            get_native_stop_orders=AsyncMock(return_value=[]),
            get_max_leverage=AsyncMock(return_value=20),
            get_position_limit_usdt=AsyncMock(return_value=100),
            get_funding_rate=AsyncMock(side_effect=AssertionError("stale per-symbol funding")),
        )
        message = SimpleNamespace(reply_text=AsyncMock(), edit_text=AsyncMock())
        context = SimpleNamespace(bot_data={"exchange": client, "tp_sl_pcts": {}})
        formatted = []

        def capture_format(*_args, **kwargs):
            formatted.append(kwargs)
            return "POSITION"

        with patch("bot.db.get_managed_position", return_value=None), \
             patch("bot.db.get_all_reentry", return_value=[]), \
             patch("bot.pos_format.format_position_block", side_effect=capture_format):
            await _send_positions(message, context)

        client.get_funding_rate.assert_not_awaited()
        self.assertEqual(formatted[0]["funding_rate"], 0.000123)
        from bot.pos_format import _fmt_funding
        self.assertEqual(_fmt_funding(None, 10, 1), "ℹ️ Фандинг: нет данных")
        self.assertIn("0.0000%", _fmt_funding(0, 10, 1))
        self.assertNotIn("/8h", _fmt_funding(0.000123, 10, 1))
        self.assertNotIn("/день", _fmt_funding(0.000123, 10, 1))


if __name__ == "__main__":
    unittest.main()

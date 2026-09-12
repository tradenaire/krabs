import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.config import Config
from bot.handlers.ask import ask_handler
from bot.handlers.balance import _fetch_all, _build_balance_text
from bot.handlers.pin import pin_handler, pin_update_job
from bot.jobs.main import setup_scheduler


class StateVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_pin_success_persists_new_ids(self):
        sent = SimpleNamespace(message_id=7)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=42),
            message=SimpleNamespace(reply_text=AsyncMock(return_value=sent)),
        )
        bot = SimpleNamespace(pin_chat_message=AsyncMock())
        context = SimpleNamespace(bot_data={'exchange': object()}, bot=bot)
        with patch('bot.handlers.pin._build_pin_text', new=AsyncMock(return_value='fresh')), \
             patch('bot.handlers.pin.db_mod.get_config', return_value=''), \
             patch('bot.handlers.pin.db_mod.set_config') as set_config:
            await pin_handler(update, context)
        self.assertEqual(context.bot_data['pin_chat_id'], 42)
        self.assertEqual(context.bot_data['pin_message_id'], 7)
        self.assertEqual(set_config.call_args_list[0].args, ('pin_chat_id', '42'))
        self.assertEqual(set_config.call_args_list[1].args, ('pin_message_id', '7'))

    async def test_pin_failure_keeps_previous_ids_and_reports_failure(self):
        sent = SimpleNamespace(message_id=7)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=42),
            message=SimpleNamespace(reply_text=AsyncMock(return_value=sent)),
        )
        bot = SimpleNamespace(pin_chat_message=AsyncMock(side_effect=RuntimeError('forbidden')))
        context = SimpleNamespace(bot_data={
            'exchange': object(), 'pin_chat_id': 9, 'pin_message_id': 8,
        }, bot=bot)
        with patch('bot.handlers.pin._build_pin_text', new=AsyncMock(return_value='fresh')), \
             patch('bot.handlers.pin.db_mod.set_config') as set_config:
            await pin_handler(update, context)
        self.assertEqual((context.bot_data['pin_chat_id'], context.bot_data['pin_message_id']), (9, 8))
        set_config.assert_not_called()
        self.assertEqual(update.message.reply_text.await_count, 2)
        self.assertIn('закрепление не подтверждено', update.message.reply_text.await_args_list[1].args[0])
        self.assertIn('Настройки автообновления не изменены', update.message.reply_text.await_args_list[1].args[0])

    async def test_ask_uses_fresh_snapshot_or_explicit_unknown_never_stale_cache(self):
        ai = MagicMock()
        ai.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='ответ'))]))
        message = SimpleNamespace(edit_text=AsyncMock())
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock(return_value=message)))
        exchange = SimpleNamespace(get_positions=AsyncMock(return_value=[]))
        context = SimpleNamespace(args=['статус'], bot_data={
            'config': Config(openrouter_api_key='dummy'), 'exchange': exchange,
            '_pos_cache': [{'symbol': 'STALE/USDT:USDT'}], '_pos_cache_ts': 1})
        with patch('openai.AsyncOpenAI', return_value=ai), patch('bot.handlers.ask._read_log', return_value=''):
            await ask_handler(update, context)
            prompt = ai.chat.completions.create.call_args.kwargs['messages'][0]['content']
            self.assertIn('подтверждён пустой список', prompt)
            self.assertNotIn('STALE', prompt)
            exchange.get_positions.side_effect = TimeoutError()
            await ask_handler(update, context)
            prompt = ai.chat.completions.create.call_args.kwargs['messages'][0]['content']
            self.assertIn('свежие данные недоступны', prompt)
            self.assertNotIn('STALE', prompt)
        self.assertIn('торговые команды не выполнялись', message.edit_text.call_args.args[0])

    async def test_pin_is_scheduled_and_failed_refresh_does_not_claim_fresh_data(self):
        app = SimpleNamespace(bot_data={'config': Config(), 'pin_chat_id': 1, 'pin_message_id': 2,
                                       'exchange': object()},
                              bot=SimpleNamespace(edit_message_text=AsyncMock()))
        with patch('bot.jobs.main.SCHEDULER') as scheduler:
            setup_scheduler(app)
            calls = [c for c in scheduler.add_job.call_args_list if c.args[0] is pin_update_job]
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].kwargs['max_instances'], 1)
            self.assertEqual(calls[0].kwargs['trigger'].interval.total_seconds(), 30)
        with patch('bot.handlers.balance._fetch_all', new=AsyncMock(side_effect=TimeoutError())):
            await pin_update_job(app)
        text = app.bot.edit_message_text.call_args.kwargs['text']
        self.assertIn('Свежий баланс не получен', text)
        self.assertNotIn('снимок получен', text)

    async def test_slow_optional_balance_data_has_deadline_and_unknown_status(self):
        cancelled = []
        async def slow():
            try:
                await asyncio.Future()
            finally:
                cancelled.append(True)
        client = SimpleNamespace(get_futures_balance=AsyncMock(return_value={'total': {'USDT': 10}}),
                                 get_positions=AsyncMock(return_value=[]),
                                 get_spot_balance=slow, get_asset_prices=slow,
                                 get_futures_margin_summary=slow)
        original_wait = asyncio.wait_for
        limits = []
        async def fast_deadline(awaitable, timeout):
            limits.append(timeout)
            return await original_wait(awaitable, .01)
        with patch('bot.handlers.balance.asyncio.wait_for', side_effect=fast_deadline), \
             patch('bot.db.get_all_reentry', return_value=[]), patch('bot.db.get_daily_stats', return_value={}):
            result = await _fetch_all(client, SimpleNamespace(bot_data={}))
        self.assertEqual(sorted(limits), [3, 3, 3, 10, 10])
        self.assertEqual(len(cancelled), 3)
        self.assertIn('нет данных спота', _build_balance_text(*result))


if __name__ == '__main__':
    unittest.main()

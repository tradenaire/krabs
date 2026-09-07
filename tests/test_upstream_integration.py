"""Offline regressions for the selected BinanceTest -> MEXC integration."""
import json
import logging
import os
from pathlib import Path
import uuid
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.ai.analyst import parse_short_candidates, parse_analyst_blocks, _clean_model_text, deep_short_analysis
from bot.ai.research_snapshot import build_research_snapshot, format_research_snapshot
from bot.exchange.client import ExchangeClient
from bot.config import Config
from bot import event_logger


class UpstreamIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_long_or_unknown_side_never_becomes_automatic_short(self):
        text = "COIN: ETH\nSIDE: LONG\nRISK: 1/10\nCOIN: BTC\nSIDE: SHORT\nRISK: 2/10\nCOIN: SOL\nSIDE: UNKNOWN"
        self.assertEqual([p['ticker'] for p in parse_short_candidates(text)], ['BTC'])
        table = "| Ticker | Side | Entry | SL | Risk |\n| --- | --- | --- | --- | --- |\n| ETH | LONG | 100 | 90 | 2 |\n| BTC | SHORT | 100 | 110 | 3 |"
        self.assertEqual([p['ticker'] for p in parse_short_candidates(table)], ['BTC'])

    def test_latest_parser_preserves_levels_and_strips_reasoning(self):
        text = "<think>hidden</think>\nIntro\nCOIN: BTC\nSIDE: SHORT\nTP1: 90\nTP2: 80\nTP3: 70\nSL: 110\nSENTIMENT: note\nExtra"
        cleaned = _clean_model_text(text)
        self.assertNotIn('hidden', cleaned)
        self.assertNotIn('Extra', cleaned)
        self.assertEqual(parse_analyst_blocks(cleaned)[0]['tp3'], '70')

    async def test_snapshot_failure_is_unknown_and_does_not_leak_error_secrets(self):
        client = SimpleNamespace(get_futures_balance=AsyncMock(side_effect=RuntimeError('SECRET credential')),
                                 get_positions=AsyncMock(side_effect=TimeoutError('SECRET')),
                                 get_tp_sl_orders=AsyncMock(side_effect=RuntimeError('SECRET')))
        snapshot = await build_research_snapshot(client, Config(mexc_secret='SECRET'), [])
        text = format_research_snapshot(snapshot)
        self.assertIn('balance=unavailable', text)
        self.assertIn('OPEN POSITIONS: unavailable', text)
        self.assertIn('TP/SL ORDERS: unavailable', text)
        self.assertNotIn('SECRET', text)

    async def test_snapshot_reports_zero_balance_and_empty_positions_when_confirmed(self):
        client = SimpleNamespace(get_futures_balance=AsyncMock(return_value={'USDT': {'free': 0, 'total': 0}}),
                                 get_positions=AsyncMock(return_value=[]), get_tp_sl_orders=AsyncMock(return_value=[]))
        text = format_research_snapshot(await build_research_snapshot(client, Config(), []))
        self.assertIn('free_usdt=0.00', text)
        self.assertIn('OPEN POSITIONS: none', text)
        self.assertIn('provider=mexc', text)

    async def test_analyst_receives_mexc_snapshot_and_closes_http_client(self):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock()
        client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='COIN: BTC\nSIDE: SHORT'))], usage=None))
        with patch('openai.AsyncOpenAI', return_value=client):
            result = await deep_short_analysis([], 'dummy', research_snapshot={'provider': 'mexc'})
        messages = client.chat.completions.create.call_args.kwargs['messages']
        self.assertIn('provider=mexc', messages[1]['content'])
        self.assertNotIn('BINANCE', messages[1]['content'])
        self.assertIsNone(result.error)
        client.__aexit__.assert_awaited_once()

    async def test_mexc_shutdown_does_not_create_or_resurrect_sessions(self):
        client = ExchangeClient('dummy', 'dummy')
        await client.close()
        self.assertIsNone(client._exchange.session)
        self.assertIsNone(client._spot.session)

    async def test_mexc_shutdown_releases_created_sessions(self):
        client = ExchangeClient('dummy', 'dummy')
        sessions = [client._exchange.session, client._spot.session]
        await client.close()
        self.assertTrue(all(s.closed for s in sessions))
        self.assertIsNone(client._exchange.session)
        self.assertIsNone(client._spot.session)

    def test_audit_masks_key_fields_and_embedded_known_credentials(self):
        with patch.object(event_logger, '_secrets', ('known-secret-value',)):
            result = event_logger.sanitize({'mexc_api_key': 'abc', 'error': 'URL known-secret-value',
                                            'nested': [{'authorization': 'Bearer token'}]})
        text = json.dumps(result)
        self.assertNotIn('known-secret-value', text)
        self.assertNotIn('Bearer token', text)
        self.assertNotIn('abc', text)

    async def test_audit_telegram_metadata_omits_setkey_body(self):
        update = SimpleNamespace(update_id=99, effective_user=SimpleNamespace(id=1),
                                 effective_message=SimpleNamespace(text='/setkey mexc_secret SECRET'))
        with patch.object(event_logger, 'log_event') as log:
            await event_logger.telegram_update_logger(update, None)
        self.assertNotIn('SECRET', repr(log.call_args))

    def test_audit_writes_bounded_jsonl_without_credentials(self):
        folder = Path(__file__).parent / f'tmp-audit-{uuid.uuid4().hex}'
        handlers, level = event_logger.logger.handlers[:], event_logger.logger.level
        try:
            with patch.object(event_logger.db, 'DB_PATH', folder / 'bot.db'), patch.object(event_logger, '_secrets', ()):
                event_logger.configure_audit(Config(mexc_secret='private-value'))
                event_logger.log_event('test_event', error='private-value', api_key='raw-value')
                handler = event_logger.logger.handlers[0]
                handler.flush()
                self.assertEqual(handler.maxBytes, 2_000_000)
                self.assertEqual(handler.backupCount, 4)
                text = (folder / 'logs/audit.jsonl').read_text(encoding='utf-8')
                self.assertEqual(json.loads(text)['event'], 'test_event')
                self.assertNotIn('private-value', text)
                self.assertNotIn('raw-value', text)
        finally:
            for handler in event_logger.logger.handlers:
                handler.close()
            event_logger.logger.handlers = handlers
            event_logger.logger.setLevel(level)
            (folder / 'logs/audit.jsonl').unlink(missing_ok=True)
            if (folder / 'logs').exists(): (folder / 'logs').rmdir()
            if folder.exists(): folder.rmdir()

    async def test_auto_scan_rejects_long_model_output_before_order_submission(self):
        from bot.jobs.main import auto_scan_job
        config = Config(auto_scan_enabled=True, openrouter_api_key='dummy', allowed_user_ids=[1])
        client = SimpleNamespace(get_positions=AsyncMock(return_value=[]), place_futures_order=AsyncMock())
        app = SimpleNamespace(bot_data={'config': config, 'exchange': client}, bot=SimpleNamespace(send_message=AsyncMock()))
        snapshot = {'provider': 'mexc'}
        with patch('bot.jobs.main._get_btc_rsi_4h', new=AsyncMock(return_value=40)), \
             patch('bot.ai.scanner.scan_overbought', new=AsyncMock(return_value=([], 0))), \
             patch('bot.ai.research_snapshot.research_context', new=AsyncMock(return_value=snapshot)), \
             patch('bot.ai.analyst.deep_short_analysis', new=AsyncMock(return_value=SimpleNamespace(
                 text='COIN: ETH\nSIDE: LONG\nRISK: 1/10', error=None))) as analyst:
            await auto_scan_job(app)
        client.place_futures_order.assert_not_awaited()
        self.assertEqual(analyst.call_args.kwargs['research_snapshot'], snapshot)

    def test_live_handler_registration_keeps_authorization_before_logging_and_commands(self):
        # Import only; polling/startup and config storage are replaced below.
        with patch.dict(os.environ, {"NUMBA_DISABLE_JIT": "1"}):
            from bot import main
        self.assertEqual(logging.getLogger('httpx').getEffectiveLevel(), logging.WARNING)
        self.assertEqual(logging.getLogger('httpcore').getEffectiveLevel(), logging.WARNING)
        app, builder = MagicMock(), MagicMock()
        builder.token.return_value = builder
        builder.post_init.return_value = builder
        builder.post_shutdown.return_value = builder
        builder.build.return_value = app
        with patch.object(main.db_mod, 'init_db'), patch.object(main.db_mod, 'get_all_config', return_value={}), \
             patch.object(main.Config, 'from_dict', return_value=Config(telegram_token='dummy')), \
             patch.object(main.Application, 'builder', return_value=builder), patch.object(main, 'ExchangeClient'):
            main.main()
        calls = app.add_handler.call_args_list
        callbacks = [(c.args[0].callback.__name__, c.kwargs.get('group', 0)) for c in calls]
        self.assertIn(('authorize_update', -2), callbacks)
        self.assertIn(('telegram_update_logger', -1), callbacks)
        self.assertIn(('repair_tpsl_handler', 0), callbacks)
        self.assertIn(('repair_tpsl_callback', 0), callbacks)
        self.assertIn(('short_handler', 0), callbacks)


if __name__ == '__main__':
    unittest.main()

"""Entry point — Telegram bot."""
import logging
import sys
from pathlib import Path

from telegram import Update
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, TypeHandler, filters)

from bot import db as db_mod
from bot.config import Config
from bot.exchange.client import ExchangeClient
from bot.event_logger import (patch_bot_logging, telegram_error_logger,
                              telegram_update_logger)
from bot.handlers.scan import (scan_handler, open_callback, open_confirm_callback,
                               open_anyway_callback, scan_avg_callback, avg_force_callback,
                               scan_preview_callback, scan_confirm_callback)
from bot.handlers.balance import balance_handler, balance_callback
from bot.handlers.positions import positions_handler, positions_callback
from bot.handlers.trading import (short_handler, close_handler, avg_handler, setkey_handler,
                                   setbet_handler, setstop_handler, settp_handler, avg_callback,
                                   min_open_callback, avgunlock_callback,
                                   close_reentry_callback, close_final_callback, close_cancel_callback)
from bot.handlers.assistant import assistant_handler, nlp_close_callback
from bot.handlers.signals import signal_callback, signal_photo_handler
from bot.handlers.stats import stats_handler
from bot.jobs.main import setup_scheduler
from bot.handlers.monitor_callbacks import (monitor_close_callback, monitor_close_confirm_callback,
                                             monitor_close_cancel_callback, monitor_stats_callback)
from bot.handlers.paper import paper_handler, paper_callback
from bot.handlers.automode import automode_handler
from bot.handlers.pin import pin_handler
from bot.handlers.ask import ask_handler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(str(Path(__file__).parent.parent / "data" / "bot.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


async def start_handler(update: Update, context):
    await update.message.reply_text(
        "*Krabs3 — Binance Futures Bot*\n\n"
        "/scan — AI шорт-пикер (LLM + web search)\n"
        "/balance — фьючерсный баланс и маржа\n"
        "/positions — открытые позиции\n"
        "/short SYMBOL [amount] — открыть шорт\n"
        "/close SYMBOL — закрыть позицию\n"
        "/avg — все настройки (ставка, тп, сл, докупка)\n"
        "/setbet — изменить ставку\n"
        "/setstops — изменить стоплосс\n"
        "/settakes — изменить тейкпрофит\n"
        "/stats — статистика за день\n"
        "/setkey KEY VALUE — сохранить API-ключ\n\n"
        "*Binance API keys:*\n"
        "Demo account: https://demo.binance.com/en/my/settings/api-management\n"
        "`/setkey exchange_provider binance_testnet`\n"
        "`/setkey binance_api_key YOUR_DEMO_API_KEY`\n"
        "`/setkey binance_secret YOUR_DEMO_SECRET`\n\n"
        "Real account: https://www.binance.com/en/my/settings/api-management\n"
        "`/setkey exchange_provider binance`\n"
        "`/setkey binance_api_key YOUR_REAL_API_KEY`\n"
        "`/setkey binance_secret YOUR_REAL_SECRET`\n\n"
        "*Signal screenshots/text:*\n"
        "Send a signal image or text with SYMBOL, LONG/SHORT, Entry, SL, TP1/TP2/TP3.\n"
        "Images are decoded by the configured vision model into order variables.\n"
        "`/setkey openrouter_api_key YOUR_KEY`\n"
        "`/setkey signal_vision_model openai/gpt-5.5`\n"
        "The bot will show a confirmation card first; GPT does not open trades without your button.\n\n"
        "💬 *Текстовые команды (без /)* — пиши как хочешь:\n"
        "• `открой CHIP` — шорт со стандартными настройками\n"
        "• `закрой BTC` — закрыть (с подтверждением)\n"
        "• `закрой все` — закрыть все (с подтверждением)\n"
        "• `тп 200% сл 1000%` — поменять у всех позиций\n"
        "• `тп 300% у SOL` — только у одной монеты\n"
        "• `сл 500%` — только стоп\n"
        "• `баланс` — показать баланс\n"
        "• `позиции` — список позиций\n"
        "• `статистика` / `стат` — дневная статистика\n"
        "• `сетап` — текущие настройки бота",
        parse_mode="Markdown",
    )


def main():
    db_mod.init_db()
    cfg_dict = db_mod.get_all_config()
    config = Config.from_dict(cfg_dict)

    if not config.telegram_token:
        logger.error("No telegram_token. Run: python start.py --setup")
        sys.exit(1)

    from bot.exchange.factory import create_exchange_client
    client = create_exchange_client(config)

    async def post_init(application: Application):
        application.bot_data["config"] = config
        application.bot_data["exchange"] = client
        # Infra layer: typed shared state + event bus (additive; legacy bot_data
        # keys remain in place until fully migrated).
        from bot.infra.state import get_state
        from bot.infra.event_bus import get_bus
        get_state(application)
        get_bus(application)
        patch_bot_logging(application.bot)
        # Pre-populate tp_sl_pcts from config so tpsl_enforce_job uses correct values after restart
        tp_pct = float(getattr(config, "tp_pct", 500))
        sl_pct = float(getattr(config, "sl_pct", 500))
        from bot import db as db_mod
        tp_sl_pcts = {
            p["symbol"]: {"tp_pct": tp_pct, "sl_pct": sl_pct}
            for p in db_mod.get_open_positions()
        }
        application.bot_data["tp_sl_pcts"] = tp_sl_pcts
        from bot.jobs.main import _load_exhausted
        application.bot_data["_avg_notified_exhausted"] = _load_exhausted()
        await setup_scheduler(application)
        from bot.testing_update_endpoint import maybe_start_test_update_endpoint
        await maybe_start_test_update_endpoint(application, config)

        # Deduplicate + sync DB with exchange on startup
        dupes = db_mod.dedupe_open_positions()
        try:
            live_positions = await client.get_positions()
            live_symbols = {p["symbol"] for p in live_positions}
            closed = db_mod.sync_closed_positions(live_symbols)
            if dupes or closed:
                logger.info("Startup sync: removed %d dupes, closed %d stale DB records", dupes, len(closed))
            # Auto-register exchange positions missing from DB
            cfg_tp = float(getattr(config, "tp_pct", 500))
            cfg_sl = float(getattr(config, "sl_pct", 500))
            cfg_avg_amount = float(getattr(config, "averaging_amount", 0.25))
            cfg_max_count = int(getattr(config, "max_averaging_count", 200))
            registered = 0
            for lp in live_positions:
                sym = lp["symbol"]
                if not db_mod.get_open_position(sym):
                    cur_margin = float(lp.get("margin", cfg_avg_amount) or cfg_avg_amount)
                    est_count = max(0, round(cur_margin / cfg_avg_amount) - 1) if cfg_avg_amount > 0 else 0
                    db_mod.upsert_position(
                        sym, lp.get("side", "short"),
                        float(lp.get("entry_price", 0) or 0),
                        int(lp.get("leverage", 1) or 1),
                        cur_margin,
                        tp_pct=cfg_tp, sl_pct=cfg_sl,
                        budget=cfg_avg_amount * cfg_max_count,
                        total_invested=cur_margin,
                        avg_count=est_count,
                    )
                    application.bot_data.setdefault("tp_sl_pcts", {}).setdefault(
                        sym, {"tp_pct": cfg_tp, "sl_pct": cfg_sl}
                    )
                    registered += 1
                    logger.info("Startup: auto-registered position %s in DB", sym)
            if registered:
                logger.info("Startup: registered %d untracked positions from exchange", registered)
        except Exception as e:
            logger.warning("Startup sync failed (exchange unavailable): %s", e)

        from telegram import BotCommand
        await application.bot.set_my_commands([
            BotCommand("balance", "Баланс и позиции"),
            BotCommand("scan", "AI шорт-пикер"),
            BotCommand("avg", "Все настройки"),
            BotCommand("stats", "Статистика за день"),
            BotCommand("paper", "Бумажный портфель $500"),
            BotCommand("automode", "Авто-скан и открытие позиций"),
            BotCommand("pin", "Закрепить баланс (авто-обновление)"),
            BotCommand("ask", "Спросить AI"),
        ])
        logger.info("Bot started.")

    async def post_shutdown(application: Application):
        from bot.testing_update_endpoint import stop_test_update_endpoint
        await stop_test_update_endpoint(application)
        mgr = application.bot_data.get("engine_manager")
        if mgr is not None:
            await mgr.stop_all()
        worker = application.bot_data.get("scanner_worker")
        if worker is not None:
            await worker.stop()
        try:
            await client.close()
        except Exception:
            pass

    app = (
        Application.builder()
        .token(config.telegram_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(TypeHandler(Update, telegram_update_logger), group=-100)
    app.add_error_handler(telegram_error_logger)
    app.add_handler(CommandHandler("help", start_handler))
    app.add_handler(CommandHandler("scan", scan_handler))
    app.add_handler(CommandHandler("balance", balance_handler))
    app.add_handler(CommandHandler("positions", positions_handler))
    app.add_handler(CommandHandler("short", short_handler))
    app.add_handler(CommandHandler("close", close_handler))
    app.add_handler(CommandHandler("avg", avg_handler))
    app.add_handler(CommandHandler("stats", stats_handler))
    app.add_handler(CommandHandler("setkey", setkey_handler))
    app.add_handler(CommandHandler("setbet", setbet_handler))
    app.add_handler(CommandHandler("setstop", setstop_handler))
    app.add_handler(CommandHandler("setstops", setstop_handler))
    app.add_handler(CommandHandler("settp", settp_handler))
    app.add_handler(CommandHandler("settakes", settp_handler))
    app.add_handler(CommandHandler("paper", paper_handler))
    app.add_handler(CallbackQueryHandler(paper_callback, pattern="^paper_reset"))
    app.add_handler(CommandHandler("automode", automode_handler))
    app.add_handler(CommandHandler("pin", pin_handler))
    app.add_handler(CommandHandler("ask", ask_handler))

    app.add_handler(CallbackQueryHandler(signal_callback, pattern=r"^sig_"))
    app.add_handler(CallbackQueryHandler(min_open_callback, pattern=r"^min_open_"))
    app.add_handler(CallbackQueryHandler(open_confirm_callback, pattern=r"^open_confirm_"))
    app.add_handler(CallbackQueryHandler(open_anyway_callback, pattern=r"^open_anyway_"))
    app.add_handler(CallbackQueryHandler(scan_preview_callback, pattern=r"^scan_preview_"))
    app.add_handler(CallbackQueryHandler(scan_confirm_callback, pattern=r"^scan_confirm_"))
    app.add_handler(CallbackQueryHandler(scan_avg_callback, pattern=r"^scan_avg_"))
    app.add_handler(CallbackQueryHandler(avg_force_callback, pattern=r"^avg_force_"))
    app.add_handler(CallbackQueryHandler(open_callback, pattern=r"^open_"))
    app.add_handler(CallbackQueryHandler(balance_callback, pattern=r"^balance_"))
    app.add_handler(CallbackQueryHandler(balance_callback, pattern=r"^bal_close_"))
    app.add_handler(CallbackQueryHandler(balance_callback, pattern=r"^positions_show$"))
    app.add_handler(CallbackQueryHandler(positions_callback, pattern=r"^(pos_|positions_)"))
    app.add_handler(CallbackQueryHandler(monitor_close_confirm_callback, pattern=r"^mon_close_confirm_"))
    app.add_handler(CallbackQueryHandler(monitor_close_cancel_callback, pattern=r"^mon_close_cancel$"))
    app.add_handler(CallbackQueryHandler(monitor_close_callback, pattern=r"^mon_close_"))
    app.add_handler(CallbackQueryHandler(monitor_stats_callback, pattern=r"^mon_stats$"))
    app.add_handler(CallbackQueryHandler(balance_callback, pattern=r"^bal_close_confirm_"))
    app.add_handler(CallbackQueryHandler(balance_callback, pattern=r"^bal_close_cancel$"))
    app.add_handler(CallbackQueryHandler(balance_callback, pattern=r"^transfer_"))
    app.add_handler(CallbackQueryHandler(balance_callback, pattern=r"^bal_toggle_"))

    app.add_handler(CallbackQueryHandler(close_reentry_callback, pattern=r"^close_reentry_"))
    app.add_handler(CallbackQueryHandler(close_final_callback, pattern=r"^close_final_"))
    app.add_handler(CallbackQueryHandler(close_cancel_callback, pattern=r"^close_cancel_"))

    app.add_handler(CallbackQueryHandler(avg_callback, pattern=r"^avg_"))
    app.add_handler(CallbackQueryHandler(avg_callback, pattern=r"^dyn_"))
    app.add_handler(CallbackQueryHandler(nlp_close_callback, pattern=r"^nlp_close_"))
    app.add_handler(CallbackQueryHandler(avgunlock_callback, pattern=r"^avgunlock_"))

    # NLP free-form text (lowest priority — after all commands and callbacks)
    app.add_handler(MessageHandler(filters.PHOTO, signal_photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, assistant_handler))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

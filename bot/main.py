"""Entry point — Telegram bot."""
import logging
import sys
from pathlib import Path

from telegram import Update
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, filters)

from bot import db as db_mod
from bot.config import Config
from bot.exchange.client import ExchangeClient
from bot.handlers.scan import scan_handler, open_callback
from bot.handlers.balance import balance_handler, balance_callback
from bot.handlers.positions import positions_handler, positions_callback
from bot.handlers.trading import (short_handler, close_handler, avg_handler, setkey_handler,
                                   setbet_handler, setstop_handler, settp_handler, avg_callback,
                                   setmexc_handler)
from bot.handlers.assistant import assistant_handler, nlp_close_callback
from bot.handlers.stats import stats_handler
from bot.jobs.main import setup_scheduler
from bot.handlers.monitor_callbacks import (monitor_close_callback, monitor_close_confirm_callback,
                                             monitor_close_cancel_callback, monitor_stats_callback)
from bot.handlers.paper import paper_handler, paper_callback
from bot.handlers.automode import automode_handler
from bot.handlers.pin import pin_handler

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
        "*Krabs3 — MEXC Futures Bot*\n\n"
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

    client = ExchangeClient(config.mexc_api_key, config.mexc_secret)

    async def post_init(application: Application):
        application.bot_data["config"] = config
        application.bot_data["exchange"] = client
        # Pre-populate tp_sl_pcts from config so tpsl_enforce_job uses correct values after restart
        tp_pct = float(getattr(config, "tp_pct", 500))
        sl_pct = float(getattr(config, "sl_pct", 500))
        from bot import db as db_mod
        tp_sl_pcts = {
            p["symbol"]: {"tp_pct": tp_pct, "sl_pct": sl_pct}
            for p in db_mod.get_open_positions()
        }
        application.bot_data["tp_sl_pcts"] = tp_sl_pcts
        setup_scheduler(application)

        # Deduplicate + sync DB with exchange on startup
        dupes = db_mod.dedupe_open_positions()
        try:
            live_positions = await client.get_positions()
            live_symbols = {p["symbol"] for p in live_positions}
            closed = db_mod.sync_closed_positions(live_symbols)
            if dupes or closed:
                logger.info("Startup sync: removed %d dupes, closed %d stale DB records", dupes, len(closed))
        except Exception as e:
            logger.warning("Startup sync failed (exchange unavailable): %s", e)

        from telegram import BotCommand
        await application.bot.set_my_commands([
            BotCommand("start", "Помощь"),
            BotCommand("balance", "Баланс и позиции"),
            BotCommand("positions", "Открытые позиции"),
            BotCommand("scan", "AI шорт-пикер"),
            BotCommand("short", "Открыть шорт"),
            BotCommand("close", "Закрыть позицию"),
            BotCommand("avg", "Все настройки"),
            BotCommand("setbet", "Изменить ставку"),
            BotCommand("setstops", "Изменить стоплосс"),
            BotCommand("settakes", "Изменить тейкпрофит"),
            BotCommand("stats", "Статистика за день"),
            BotCommand("paper", "Бумажный портфель $500"),
            BotCommand("automode", "Авто-скан и открытие позиций"),
            BotCommand("pin", "Закрепить баланс (авто-обновление)"),
            BotCommand("setmexc", "Заменить MEXC ключи (с проверкой)"),
        ])
        logger.info("Bot started.")

    app = (
        Application.builder()
        .token(config.telegram_token)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", start_handler))
    app.add_handler(CommandHandler("scan", scan_handler))
    app.add_handler(CommandHandler("balance", balance_handler))
    app.add_handler(CommandHandler("positions", positions_handler))
    app.add_handler(CommandHandler("short", short_handler))
    app.add_handler(CommandHandler("close", close_handler))
    app.add_handler(CommandHandler("avg", avg_handler))
    app.add_handler(CommandHandler("stats", stats_handler))
    app.add_handler(CommandHandler("setkey", setkey_handler))
    app.add_handler(CommandHandler("setmexc", setmexc_handler))
    app.add_handler(CommandHandler("setbet", setbet_handler))
    app.add_handler(CommandHandler("setstop", setstop_handler))
    app.add_handler(CommandHandler("setstops", setstop_handler))
    app.add_handler(CommandHandler("settp", settp_handler))
    app.add_handler(CommandHandler("settakes", settp_handler))
    app.add_handler(CommandHandler("paper", paper_handler))
    app.add_handler(CallbackQueryHandler(paper_callback, pattern="^paper_reset"))
    app.add_handler(CommandHandler("automode", automode_handler))
    app.add_handler(CommandHandler("pin", pin_handler))

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

    app.add_handler(CallbackQueryHandler(avg_callback, pattern=r"^avg_"))
    app.add_handler(CallbackQueryHandler(nlp_close_callback, pattern=r"^nlp_close_"))

    # NLP free-form text (lowest priority — after all commands and callbacks)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, assistant_handler))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

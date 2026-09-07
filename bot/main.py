"""Entry point — Telegram bot."""
import logging
import sys
import os
from dataclasses import fields
from pathlib import Path

from telegram import Update
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, filters, TypeHandler)

from bot import db as db_mod
from bot.config import Config
from bot.exchange.client import ExchangeClient
from bot.handlers.scan import (scan_handler, open_callback, open_confirm_callback,
                               open_anyway_callback, scan_avg_callback, avg_force_callback)
from bot.handlers.balance import balance_handler, balance_callback
from bot.handlers.positions import positions_handler, positions_callback
from bot.handlers.trading import (short_handler, close_handler, avg_handler, setkey_handler,
                                   setbet_handler, setstop_handler, settp_handler, avg_callback,
                                   min_open_callback, avgunlock_callback,
                                   close_reentry_callback, close_final_callback, close_cancel_callback)
from bot.handlers.assistant import assistant_handler, nlp_close_callback
from bot.handlers.stats import stats_handler
from bot.jobs.main import setup_scheduler
from bot.handlers.monitor_callbacks import (monitor_close_callback, monitor_close_confirm_callback,
                                             monitor_close_cancel_callback, monitor_stats_callback)
from bot.handlers.paper import paper_handler, paper_callback
from bot.handlers.automode import automode_handler
from bot.handlers.pin import pin_handler
from bot.handlers.ask import ask_handler
from bot.handlers.protection import repair_tpsl_handler, repair_tpsl_callback
from bot.event_logger import configure_audit, telegram_update_logger, telegram_error_logger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
# Telegram request URLs contain the bot token; never emit HTTP wire logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


async def start_handler(update: Update, context):
    await update.message.reply_text(
        "*Krabs3 — MEXC Futures Bot*\n\n"
        "/scan — AI шорт-пикер (LLM + web search)\n"
        "/balance — фьючерсный баланс и маржа\n"
        "/positions — открытые позиции\n"
        "/adopt SYMBOL — принять ручную позицию\n"
        "`/repair_tpsl SYMBOL` — проверить и восстановить защиту\n"
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
    cfg_dict.update({f.name: os.environ[f.name.upper()] for f in fields(Config) if f.name.upper() in os.environ})
    config = Config.from_dict(cfg_dict)
    if config.exchange_provider != "mexc":
        raise ValueError("This deployment supports MEXC; BinanceTest requires a separate validated deployment")

    if not config.telegram_token:
        logger.error("No telegram_token. Run: python start.py --setup")
        sys.exit(1)

    client = ExchangeClient(config.mexc_api_key, config.mexc_secret)

    async def post_init(application: Application):
        application.bot_data["config"] = config
        application.bot_data["exchange"] = client
        configure_audit(config)
        application.bot_data["tp_sl_pcts"] = {
            p["symbol"]: {"tp_pct": p["tp_pct"], "sl_pct": p["sl_pct"]}
            for p in db_mod.get_open_positions() if p.get("exchange_position_id")
        }
        setup_scheduler(application)

        from telegram import BotCommand
        await application.bot.set_my_commands([
            BotCommand("start", "Помощь"),
            BotCommand("balance", "Баланс и позиции"),
            BotCommand("positions", "Открытые позиции"),
            BotCommand("repair_tpsl", "Проверить и восстановить TP/SL"),
            BotCommand("adopt", "Принять ручную позицию"),
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
            BotCommand("ask", "Спросить AI"),
        ])
        logger.info("Bot started.")

    async def post_shutdown(application):
        await client.close()

    app = (
        Application.builder()
        .token(config.telegram_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    from bot.lifecycle import adopt_handler, authorize_update
    app.add_handler(TypeHandler(Update, authorize_update), group=-2)
    app.add_handler(TypeHandler(Update, telegram_update_logger), group=-1)
    app.add_error_handler(telegram_error_logger)
    app.add_handler(CommandHandler("repair_tpsl", repair_tpsl_handler))
    app.add_handler(CallbackQueryHandler(repair_tpsl_callback, pattern=r"^repair_tpsl_"))
    app.add_handler(CommandHandler("adopt", adopt_handler))
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

    app.add_handler(CallbackQueryHandler(min_open_callback, pattern=r"^min_open_"))
    app.add_handler(CallbackQueryHandler(open_confirm_callback, pattern=r"^open_confirm_"))
    app.add_handler(CallbackQueryHandler(open_anyway_callback, pattern=r"^open_anyway_"))
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, assistant_handler))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

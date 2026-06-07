"""/automode — настройка автоматического сканирования и открытия позиций."""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from bot import db as db_mod
from bot.config import Config
from bot.jobs.main import reschedule_auto_scan, _get_manager

logger = logging.getLogger(__name__)

_HELP = (
    "*Auto Scan — автоматический поиск и открытие шортов*\n\n"
    "`/automode on` — включить\n"
    "`/automode off` — выключить\n"
    "`/automode interval 30` — сканировать раз в 30 мин\n"
    "`/automode maxpos 3` — макс. позиций (если больше — не открывать)\n"
    "`/automode maxrisk 7` — только если RISK ≤ 7/10\n"
)


def _status_text(config: Config) -> str:
    import datetime as _dt
    enabled = getattr(config, "auto_scan_enabled", False)
    interval = int(getattr(config, "auto_scan_interval_min", 30))
    maxpos = int(getattr(config, "auto_scan_max_positions", 3))
    maxrisk = int(getattr(config, "auto_scan_max_risk", 7))
    icon = "🟢 ВКЛ" if enabled else "🔴 ВЫКЛ"
    lines = [
        f"🤖 *Auto Scan*: {icon}",
        f"Интервал: каждые *{interval} мин*",
        f"Макс. позиций: *{maxpos}* | Макс. риск: *{maxrisk}/10*",
    ]
    mgr = _get_manager()
    eng = mgr.get("auto_scan") if mgr else None
    if eng and eng.last_run_ts:
        local_next = _dt.datetime.fromtimestamp(eng.next_run_ts())
        lines.append(f"Следующий скан: *{local_next.strftime('%H:%M')}*")
    lines.append("\n" + _HELP)
    return "\n".join(lines)


def _save(key: str, value) -> None:
    db_mod.set_config(key, str(value))


async def automode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config: Config = context.bot_data["config"]
    args = context.args or []

    if not args:
        await update.message.reply_text(_status_text(config), parse_mode="Markdown")
        return

    cmd = args[0].lower()

    if cmd == "on":
        config.auto_scan_enabled = True
        _save("auto_scan_enabled", "true")
        interval = int(getattr(config, "auto_scan_interval_min", 30))
        reschedule_auto_scan(interval)
        await update.message.reply_text(
            f"🟢 Auto Scan *включён* — каждые {interval} мин\n"
            f"Макс. позиций: {getattr(config, 'auto_scan_max_positions', 3)} | "
            f"Макс. риск: {getattr(config, 'auto_scan_max_risk', 7)}/10",
            parse_mode="Markdown",
        )
        return

    if cmd == "off":
        config.auto_scan_enabled = False
        _save("auto_scan_enabled", "false")
        await update.message.reply_text("🔴 Auto Scan *выключен*", parse_mode="Markdown")
        return

    if cmd == "interval" and len(args) >= 2:
        try:
            val = max(1, min(int(args[1]), 1440))
        except ValueError:
            await update.message.reply_text("❌ Укажи число минут: `/automode interval 30`",
                                            parse_mode="Markdown")
            return
        config.auto_scan_interval_min = val
        _save("auto_scan_interval_min", val)
        reschedule_auto_scan(val)
        await update.message.reply_text(f"⏱ Интервал скана: каждые *{val} мин*", parse_mode="Markdown")
        return

    if cmd == "maxpos" and len(args) >= 2:
        try:
            val = max(1, min(int(args[1]), 20))
        except ValueError:
            await update.message.reply_text("❌ Укажи число: `/automode maxpos 3`",
                                            parse_mode="Markdown")
            return
        config.auto_scan_max_positions = val
        _save("auto_scan_max_positions", val)
        await update.message.reply_text(f"📊 Макс. позиций авто-скана: *{val}*", parse_mode="Markdown")
        return

    if cmd == "maxrisk" and len(args) >= 2:
        try:
            val = max(1, min(int(args[1]), 10))
        except ValueError:
            await update.message.reply_text("❌ Укажи 1-10: `/automode maxrisk 7`",
                                            parse_mode="Markdown")
            return
        config.auto_scan_max_risk = val
        _save("auto_scan_max_risk", val)
        await update.message.reply_text(
            f"⚠️ Макс. риск для авто-открытия: *{val}/10*\n"
            f"_(пики с RISK > {val}/10 будут пропущены)_",
            parse_mode="Markdown",
        )
        return

    # Unknown sub-command
    await update.message.reply_text(_HELP, parse_mode="Markdown")

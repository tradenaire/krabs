"""/automode — настройка автоматического сканирования и открытия позиций."""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from bot import db as db_mod
from bot.config import Config
from bot.handlers import wizard
from bot.handlers.wizard import Step
from bot.jobs.main import reschedule_auto_scan, SCHEDULER

logger = logging.getLogger(__name__)


AUTOMODE_STEPS: list[Step] = [
    Step(key="auto_scan_enabled",       attr="auto_scan_enabled",
         prompt="Auto Scan включён?",       kind="bool"),
    Step(key="auto_scan_interval_min",  attr="auto_scan_interval_min",
         prompt="Интервал скана, минут",     kind="int:1:1440"),
    Step(key="auto_scan_max_positions", attr="auto_scan_max_positions",
         prompt="Макс. открытых позиций",    kind="int:1:20"),
    Step(key="auto_scan_max_risk",      attr="auto_scan_max_risk",
         prompt="Макс. риск для авто-открытия", kind="int:1:10"),
]


def _save(key: str, value) -> None:
    db_mod.set_config(key, str(value))


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
    job = SCHEDULER.get_job("auto_scan")
    if job and job.next_run_time:
        local_next = job.next_run_time.astimezone(_dt.timezone.utc).astimezone()
        lines.append(f"Следующий скан: *{local_next.strftime('%H:%M')}*")
    return "\n".join(lines)


def _intro(context: ContextTypes.DEFAULT_TYPE) -> str:
    config = context.bot_data.get("config")
    if config is None:
        return ""
    return _status_text(config)


def _apply_changes(config: Config, values: dict) -> None:
    for attr, value in values.items():
        setattr(config, attr, value)
        if isinstance(value, bool):
            _save(attr, "true" if value else "false")
        else:
            _save(attr, value)
    if "auto_scan_interval_min" in values or values.get("auto_scan_enabled") is True:
        interval = int(getattr(config, "auto_scan_interval_min", 30))
        reschedule_auto_scan(interval)


async def _finish(context: ContextTypes.DEFAULT_TYPE, chat_id: int, wizard_state: dict) -> None:
    config: Config = context.bot_data["config"]
    values = wizard_state.get("changed", {}) or {}
    if not values:
        await context.bot.send_message(chat_id=chat_id, text="Ничего не изменено.")
        return

    _apply_changes(config, values)

    labels = {s.key: s.prompt for s in AUTOMODE_STEPS}
    lines = ["✅ *Auto Scan обновлён:*", ""]
    for attr, value in values.items():
        step = next((s for s in AUTOMODE_STEPS if s.key == attr), None)
        if step is None:
            continue
        lines.append(f"{labels[attr]}: `{wizard.format_value(step, value)}`")
    lines.append("")
    lines.append(_status_text(config))
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")


wizard.register("automode", AUTOMODE_STEPS, _finish, intro=_intro)


async def _start(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    wizard.start_wizard(context, "automode")
    await wizard.send_intro(context.bot, chat_id, "automode", context)
    await wizard.render_step(context.bot, chat_id, "automode", 0, context)


async def automode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config: Config = context.bot_data["config"]
    args = context.args or []

    if not args:
        await _start(context, update.message.chat_id)
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

    # Unknown sub-command → wizard.
    await _start(context, update.message.chat_id)


async def automode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await wizard.handle_callback(update, context, "automode")

"""/automode — настройка автоматического сканирования и открытия позиций."""
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot import db as db_mod
from bot.config import Config
from bot.jobs.main import reschedule_auto_scan, SCHEDULER

logger = logging.getLogger(__name__)

AUTOMODE_WIZARD_STEPS = [
    ("enabled", "auto_scan_enabled", "Auto Scan включён?", "bool"),
    ("interval", "auto_scan_interval_min", "Интервал скана, минут", "int:1:1440"),
    ("maxpos", "auto_scan_max_positions", "Макс. открытых позиций", "int:1:20"),
    ("maxrisk", "auto_scan_max_risk", "Макс. риск для авто-открытия", "int:1:10"),
]

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
    job = SCHEDULER.get_job("auto_scan")
    if job and job.next_run_time:
        local_next = job.next_run_time.astimezone(_dt.timezone.utc).astimezone()
        lines.append(f"Следующий скан: *{local_next.strftime('%H:%M')}*")
    lines.append("\n" + _HELP)
    return "\n".join(lines)


def _wizard_keyboard(step_idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭", callback_data=f"automode_skip_{step_idx}"),
        InlineKeyboardButton("◀️", callback_data="automode_back"),
        InlineKeyboardButton("✖️", callback_data="automode_cancel"),
    ]])


def _format_value(value, kind: str) -> str:
    if kind == "bool":
        return "ВКЛ" if bool(value) else "ВЫКЛ"
    if kind.startswith("int:"):
        return str(int(value))
    return str(value)


def _current_wizard_value(config: Config, wizard: dict, attr: str):
    return wizard.get("values", {}).get(attr, getattr(config, attr))


def _parse_bool(text: str) -> bool:
    value = text.strip().lower()
    if value in ("1", "да", "д", "yes", "y", "on", "вкл", "включи", "включить", "true"):
        return True
    if value in ("0", "нет", "н", "no", "n", "off", "выкл", "выключи", "выключить", "false"):
        return False
    raise ValueError("ответь да/нет или вкл/выкл")


def _parse_step_value(text: str, kind: str):
    if kind == "bool":
        return _parse_bool(text)
    if kind.startswith("int:"):
        _, min_s, max_s = kind.split(":")
        try:
            value = int(text.strip())
        except ValueError:
            raise ValueError("нужно целое число")
        min_v, max_v = int(min_s), int(max_s)
        if value < min_v or value > max_v:
            raise ValueError(f"допустимо от {min_v} до {max_v}")
        return value
    return text.strip()


async def send_automode_wizard_step(bot, chat_id: int, step_idx: int, config: Config,
                                    wizard: dict | None = None) -> None:
    wizard = wizard or {"values": {}}
    _, attr, label, kind = AUTOMODE_WIZARD_STEPS[step_idx]
    current = _current_wizard_value(config, wizard, attr)
    total = len(AUTOMODE_WIZARD_STEPS)
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"*{label}* ({step_idx + 1}/{total})\n"
            f"Сейчас: `{_format_value(current, kind)}`\n"
            "Введи новое значение:"
        ),
        parse_mode="Markdown",
        reply_markup=_wizard_keyboard(step_idx),
    )


def _apply_wizard_changes(config: Config, values: dict) -> None:
    for attr, value in values.items():
        setattr(config, attr, value)
        if isinstance(value, bool):
            _save(attr, "true" if value else "false")
        else:
            _save(attr, value)

    if "auto_scan_interval_min" in values or values.get("auto_scan_enabled") is True:
        interval = int(getattr(config, "auto_scan_interval_min", 30))
        reschedule_auto_scan(interval)


async def finish_automode_wizard(chat_id: int, context: ContextTypes.DEFAULT_TYPE,
                                 wizard: dict, config: Config) -> None:
    values = wizard.get("values", {})
    if not values:
        await context.bot.send_message(chat_id=chat_id, text="Ничего не изменено.")
        return

    _apply_wizard_changes(config, values)

    labels = {attr: label for _, attr, label, _ in AUTOMODE_WIZARD_STEPS}
    kinds = {attr: kind for _, attr, _, kind in AUTOMODE_WIZARD_STEPS}
    lines = ["✅ *Auto Scan обновлён:*", ""]
    for attr, value in values.items():
        lines.append(f"{labels[attr]}: `{_format_value(value, kinds[attr])}`")
    lines.append("")
    lines.append(_status_text(config))
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")


async def handle_automode_wizard_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    wizard = context.user_data.get("automode_wizard")
    if wizard is None:
        return False

    msg = update.message.text.strip()
    lo = msg.lower()
    if lo in ("отмена", "стоп", "cancel", "выход", "x", "✖", "✖️"):
        context.user_data.pop("automode_wizard", None)
        await update.message.reply_text("✖️ Настройка Auto Scan отменена.")
        return True

    config = context.bot_data.get("config")
    step_idx = wizard["step"]
    key, attr, _, kind = AUTOMODE_WIZARD_STEPS[step_idx]

    if msg != ".":
        try:
            value = _parse_step_value(msg.replace(",", "."), kind)
        except ValueError as e:
            await update.message.reply_text(
                f"Не подходит: {e}. Введи значение, `.` чтобы пропустить, или `отмена`.",
                parse_mode="Markdown",
            )
            return True
        wizard.setdefault("values", {})[attr] = value
        wizard.setdefault("changed", {})[key] = value

    next_step = step_idx + 1
    if next_step >= len(AUTOMODE_WIZARD_STEPS):
        context.user_data.pop("automode_wizard", None)
        await finish_automode_wizard(update.message.chat_id, context, wizard, config)
    else:
        wizard["step"] = next_step
        await send_automode_wizard_step(context.bot, update.message.chat_id, next_step, config, wizard)
    return True


def _save(key: str, value) -> None:
    db_mod.set_config(key, str(value))


async def automode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config: Config = context.bot_data["config"]
    args = context.args or []

    if not args:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✏️", callback_data="automode_edit")]])
        await update.message.reply_text(_status_text(config), parse_mode="Markdown", reply_markup=kb)
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


async def automode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    config: Config = context.bot_data.get("config")
    if not config:
        await q.edit_message_text("❌ Конфиг недоступен.")
        return

    if q.data == "automode_edit":
        wizard = {"step": 0, "values": {}, "changed": {}}
        context.user_data["automode_wizard"] = wizard
        await q.edit_message_reply_markup(reply_markup=None)
        await send_automode_wizard_step(context.bot, q.message.chat_id, 0, config, wizard)
        return

    if q.data == "automode_cancel":
        context.user_data.pop("automode_wizard", None)
        await q.edit_message_text("✖️ Настройка Auto Scan отменена.")
        return

    wizard = context.user_data.get("automode_wizard")
    if not wizard:
        await q.edit_message_text("Сессия устарела. Используй /automode заново.")
        return

    if q.data == "automode_back":
        await q.edit_message_reply_markup(reply_markup=None)
        wizard["step"] = max(0, int(wizard.get("step", 0)) - 1)
        await send_automode_wizard_step(context.bot, q.message.chat_id, wizard["step"], config, wizard)
        return

    if q.data.startswith("automode_skip_"):
        await q.edit_message_reply_markup(reply_markup=None)
        step_idx = int(wizard.get("step", 0))
        _, attr, _, _ = AUTOMODE_WIZARD_STEPS[step_idx]
        wizard.get("values", {}).pop(attr, None)
        next_step = step_idx + 1
        if next_step >= len(AUTOMODE_WIZARD_STEPS):
            context.user_data.pop("automode_wizard", None)
            await finish_automode_wizard(q.message.chat_id, context, wizard, config)
        else:
            wizard["step"] = next_step
            await send_automode_wizard_step(context.bot, q.message.chat_id, next_step, config, wizard)

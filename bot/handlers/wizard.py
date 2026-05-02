"""Shared step-by-step wizard helper.

Per-user state lives in ``context.user_data["{prefix}_wizard"]``:
    {"step": int, "values": {key: parsed_value}, "changed": {key: parsed_value},
     "initial": {key: parsed_value}}

Each handler module registers a wizard via ``register(prefix, steps, finish)``.
The wizard helper handles the universal `_back`, `_skip_N`, `_cancel`, `_pick_N_X`
callback suffixes and dispatches free-text input to whichever wizard is active.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


# ── Step descriptor ───────────────────────────────────────────────

ChoiceList = list[tuple[str, str]]
ChoiceProvider = Callable[[ContextTypes.DEFAULT_TYPE], ChoiceList]


@dataclass
class Step:
    key: str
    prompt: str
    kind: str = "text"               # "text" | "int:lo:hi" | "float" | "bool" | "choice"
    attr: Optional[str] = None       # config attribute used for "Сейчас: X" line
    optional: bool = True            # when False — the SKIP button is hidden
    parser: Optional[Callable[[str], Any]] = None
    formatter: Optional[Callable[[Any], str]] = None
    choices: Optional[Union[ChoiceList, ChoiceProvider]] = None
    unit: str = ""                   # cosmetic suffix used by default formatter


# ── Registry ──────────────────────────────────────────────────────

FinishFn = Callable[[ContextTypes.DEFAULT_TYPE, int, dict], Awaitable[None]]
IntroFn = Callable[[ContextTypes.DEFAULT_TYPE], str]


@dataclass
class _Spec:
    steps: list[Step]
    finish: FinishFn
    intro: Optional[Union[str, IntroFn]] = None


_REGISTRY: dict[str, _Spec] = {}


def register(prefix: str, steps: list[Step], finish: FinishFn,
             *, intro: Optional[Union[str, IntroFn]] = None) -> None:
    _REGISTRY[prefix] = _Spec(steps=steps, finish=finish, intro=intro)


def steps_of(prefix: str) -> list[Step]:
    return _REGISTRY[prefix].steps


def is_registered(prefix: str) -> bool:
    return prefix in _REGISTRY


# ── State helpers ─────────────────────────────────────────────────

def _wizard_key(prefix: str) -> str:
    return f"{prefix}_wizard"


def active_wizard(context: ContextTypes.DEFAULT_TYPE) -> Optional[tuple[str, dict]]:
    """Return (prefix, wizard_dict) for whichever wizard is open, or None."""
    if not getattr(context, "user_data", None):
        return None
    for prefix in _REGISTRY:
        w = context.user_data.get(_wizard_key(prefix))
        if w is not None:
            return prefix, w
    return None


def _cancel_other(context: ContextTypes.DEFAULT_TYPE, keep_prefix: str) -> None:
    for p in list(_REGISTRY):
        if p == keep_prefix:
            continue
        context.user_data.pop(_wizard_key(p), None)


def start_wizard(context: ContextTypes.DEFAULT_TYPE, prefix: str,
                 *, initial_values: Optional[dict] = None) -> dict:
    """Create + return wizard state dict; cancels any other active wizard
    AND очищает pending-state из assistant_handler (pending_transfer,
    pending_mexc, pending_set, _mexc_secret), чтобы они не перехватывали
    ввод пользователя в визарде. Без этого баг: пользователь раньше открывал
    /balance перевод или /setmexc, не закончил, и затем открыл /avg —
    его «100» уходило в pending_transfer как сумма перевода вместо плеча."""
    _cancel_other(context, prefix)
    for stale_key in ("pending_transfer", "pending_mexc", "pending_set", "_mexc_secret",
                      "_close_choices"):
        context.user_data.pop(stale_key, None)
    wizard = {
        "step": 0,
        "values": dict(initial_values or {}),
        "changed": dict(initial_values or {}),
        "initial": dict(initial_values or {}),
    }
    context.user_data[_wizard_key(prefix)] = wizard
    return wizard


def pop_wizard(context: ContextTypes.DEFAULT_TYPE, prefix: str) -> Optional[dict]:
    return context.user_data.pop(_wizard_key(prefix), None)


# ── Parsing ───────────────────────────────────────────────────────

def _parse_bool(text: str) -> bool:
    v = text.strip().lower()
    if v in ("1", "да", "д", "yes", "y", "on", "вкл", "включи", "включить", "true"):
        return True
    if v in ("0", "нет", "н", "no", "n", "off", "выкл", "выключи", "выключить", "false"):
        return False
    raise ValueError("ответь да/нет или вкл/выкл")


def _parse_int_range(text: str, kind: str) -> int:
    _, lo_s, hi_s = kind.split(":")
    try:
        v = int(text.strip())
    except ValueError:
        raise ValueError("нужно целое число")
    lo, hi = int(lo_s), int(hi_s)
    if v < lo or v > hi:
        raise ValueError(f"допустимо от {lo} до {hi}")
    return v


def _parse_float(text: str) -> float:
    try:
        return float(text.strip().replace(",", "."))
    except ValueError:
        raise ValueError("нужно число")


def parse_step_value(step: Step, text: str) -> Any:
    if step.parser:
        return step.parser(text)
    kind = step.kind
    if kind == "bool":
        return _parse_bool(text)
    if kind.startswith("int:"):
        return _parse_int_range(text, kind)
    if kind == "float":
        return _parse_float(text)
    return text.strip()


def format_value(step: Step, value: Any) -> str:
    if value is None:
        return "—"
    if step.formatter:
        return step.formatter(value)
    if step.kind == "bool":
        return "ВКЛ" if bool(value) else "ВЫКЛ"
    if step.unit == "$":
        return f"${float(value):.2f}"
    if step.unit == "%":
        return f"{float(value):.0f}%"
    if step.unit == "#":
        return str(int(value))
    if step.kind.startswith("int:"):
        return str(int(value))
    return str(value)


# ── Keyboard ──────────────────────────────────────────────────────

def build_wizard_kb(prefix: str, step_idx: int, *, optional: bool,
                    extra_rows: Optional[list[list[InlineKeyboardButton]]] = None
                    ) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if extra_rows:
        rows.extend(extra_rows)
    nav = [InlineKeyboardButton("◀️", callback_data=f"{prefix}_back")]
    if optional:
        nav.append(InlineKeyboardButton("⏭ SKIP", callback_data=f"{prefix}_skip_{step_idx}"))
    # ✅ Готово = «применить уже введённое и выйти». Если ничего не изменено —
    # эквивалентно отмене (см. handle_callback ветка cancel). Имя кнопки выбрано
    # так, чтобы пользователь не боялся жать её посреди визарда: его ввод не
    # пропадёт. Старое название '✖️ EXIT' смущало (звучало как «отменить»).
    nav.append(InlineKeyboardButton("✅ Готово", callback_data=f"{prefix}_cancel"))
    rows.append(nav)
    return InlineKeyboardMarkup(rows)


def _resolve_choices(step: Step, context: ContextTypes.DEFAULT_TYPE) -> ChoiceList:
    if callable(step.choices):
        return list(step.choices(context))
    return list(step.choices or [])


def _current_value(prefix: str, step: Step, wizard: dict,
                   context: ContextTypes.DEFAULT_TYPE) -> Any:
    if step.key in wizard.get("values", {}):
        return wizard["values"][step.key]
    if step.attr:
        config = context.bot_data.get("config")
        if config is not None and hasattr(config, step.attr):
            return getattr(config, step.attr)
    return None


# ── Rendering ─────────────────────────────────────────────────────

async def render_step(bot, chat_id: int, prefix: str, step_idx: int,
                      context: ContextTypes.DEFAULT_TYPE) -> None:
    spec = _REGISTRY[prefix]
    steps = spec.steps
    step = steps[step_idx]
    wizard = context.user_data.get(_wizard_key(prefix))
    if wizard is None:
        return

    extra_rows: list[list[InlineKeyboardButton]] = []
    if step.kind == "choice":
        choices = _resolve_choices(step, context)
        for label, value in choices:
            extra_rows.append([
                InlineKeyboardButton(label, callback_data=f"{prefix}_pick_{step_idx}_{value}"),
            ])
    elif step.kind == "bool":
        # Удобный одноклик для bool-шагов вместо ввода 'вкл'/'выкл' текстом.
        # Каст 'true'/'false' → bool делается в handle_callback ветке pick_.
        extra_rows.append([
            InlineKeyboardButton("✅ ВКЛ",  callback_data=f"{prefix}_pick_{step_idx}_true"),
            InlineKeyboardButton("⛔ ВЫКЛ", callback_data=f"{prefix}_pick_{step_idx}_false"),
        ])

    kb = build_wizard_kb(prefix, step_idx, optional=step.optional, extra_rows=extra_rows)

    cur = _current_value(prefix, step, wizard, context)
    cur_line = ""
    if cur is not None:
        cur_line = f"\nСейчас: `{format_value(step, cur)}`"

    total = len(steps)
    # 'choice' и 'bool' выбираются кнопкой — просить ввести текст не нужно.
    hint = "" if step.kind in ("choice", "bool") else "\nВведи новое значение:"
    text = f"*{step.prompt}* ({step_idx + 1}/{total}){cur_line}{hint}"

    await bot.send_message(
        chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb,
    )


async def send_intro(bot, chat_id: int, prefix: str,
                     context: ContextTypes.DEFAULT_TYPE) -> None:
    spec = _REGISTRY[prefix]
    if spec.intro is None:
        return
    text = spec.intro(context) if callable(spec.intro) else spec.intro
    if not text:
        return
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")


# ── Advance / finish ──────────────────────────────────────────────

async def _advance(context: ContextTypes.DEFAULT_TYPE, prefix: str,
                   chat_id: int) -> None:
    spec = _REGISTRY[prefix]
    wizard = context.user_data.get(_wizard_key(prefix))
    if wizard is None:
        return
    cur_idx = int(wizard.get("step", 0))
    next_idx = cur_idx + 1
    if next_idx >= len(spec.steps):
        pop_wizard(context, prefix)
        await spec.finish(context, chat_id, wizard)
    else:
        wizard["step"] = next_idx
        await render_step(context.bot, chat_id, prefix, next_idx, context)


async def _exit_wizard(context: ContextTypes.DEFAULT_TYPE, prefix: str,
                       chat_id: int, reply: Any) -> None:
    """Apply-if-changed exit: спасает накопленные изменения если пользователь
    нажал ✅ Готово / написал 'отмена' посреди визарда.

    Поведение:
      - есть `wizard["changed"]` непустой → call spec.finish (как при дойдe до конца),
        он применит изменения, отправит сводку и pop'нет state.
      - changed пуст → обычная отмена, pop state, короткое подтверждение.

    `reply` — callable (text -> awaitable). Используется для вывода «Отменено» в обоих
    путях вызова (callback edit_message_text vs text reply_text), чтобы код был общий.
    """
    wizard = context.user_data.get(_wizard_key(prefix))
    if wizard is None:
        return
    spec = _REGISTRY[prefix]
    changed = wizard.get("changed") or {}
    if changed:
        # Apply path: то же что и при достижении конца визарда.
        pop_wizard(context, prefix)
        await spec.finish(context, chat_id, wizard)
    else:
        pop_wizard(context, prefix)
        try:
            await reply("✖️ Отменено.")
        except Exception:
            pass


# ── Text router ──────────────────────────────────────────────────

_CANCEL_WORDS = {"отмена", "стоп", "cancel", "выход", "x", "exit", "✖", "✖️"}


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Dispatch free-text input to the active wizard. Returns True if consumed."""
    if not update.message or not update.message.text:
        return False
    state = active_wizard(context)
    if state is None:
        return False
    prefix, wizard = state
    spec = _REGISTRY[prefix]
    msg = update.message.text.strip()
    lo = msg.lower()

    if lo in _CANCEL_WORDS:
        # Apply-if-changed: пользователь мог ввести что-то и потом передумать
        # отвечать на ВСЕ оставшиеся шаги. Если у него есть накопленные изменения —
        # применяем их, не теряем работу. Если ничего не введено — обычная отмена.
        await _exit_wizard(context, prefix, update.message.chat_id, update.message.reply_text)
        return True

    step_idx = int(wizard.get("step", 0))
    step = spec.steps[step_idx]

    # On choice steps we ignore free text — user must press a button.
    if step.kind == "choice":
        await update.message.reply_text("Выбери вариант кнопкой ниже.")
        return True

    if msg == ".":
        # Power-user shortcut: dot = SKIP
        if step.optional:
            await _advance(context, prefix, update.message.chat_id)
            return True
        await update.message.reply_text("Этот шаг обязательный.")
        return True

    try:
        value = parse_step_value(step, msg)
    except ValueError as e:
        await update.message.reply_text(
            f"Не подходит: {e}. Попробуй ещё раз или нажми кнопку.",
            parse_mode="Markdown",
        )
        return True

    wizard.setdefault("values", {})[step.key] = value
    wizard.setdefault("changed", {})[step.key] = value
    await _advance(context, prefix, update.message.chat_id)
    return True


# ── Callback router ──────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          prefix: str) -> bool:
    """Universal handler for `_back`, `_skip_N`, `_cancel`, `_pick_N_X`."""
    q = update.callback_query
    if q is None:
        return False
    data = q.data or ""
    if not data.startswith(prefix + "_"):
        return False
    suffix = data[len(prefix) + 1:]
    chat_id = q.message.chat_id

    if suffix == "cancel":
        await q.answer()
        # Тот же механизм apply-if-changed как в text router. Пользователь нажал
        # ✅ Готово (бывший ✖️ EXIT) — если что-то изменено, применяем; иначе отмена.
        # edit_message_text используем для обычного «Отменено» (заменяем prompt на
        # короткое подтверждение). Для apply-ветки spec.finish сам шлёт сообщение
        # через bot.send_message — кнопочный prompt оставим (или можно убрать
        # клавиатуру edit_message_reply_markup). Делаем убрать, чтобы пользователь
        # не путался.
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        async def _reply(text, **_):
            await context.bot.send_message(chat_id=chat_id, text=text)
        await _exit_wizard(context, prefix, chat_id, _reply)
        return True

    wizard = context.user_data.get(_wizard_key(prefix))
    if wizard is None:
        await q.answer()
        try:
            await q.edit_message_text("Сессия устарела. Запусти команду заново.")
        except Exception:
            pass
        return True

    spec = _REGISTRY[prefix]

    if suffix == "back":
        await q.answer()
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        wizard["step"] = max(0, int(wizard.get("step", 0)) - 1)
        await render_step(context.bot, chat_id, prefix, wizard["step"], context)
        return True

    if suffix.startswith("skip_"):
        await q.answer()
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        try:
            step_idx = int(suffix.split("_", 1)[1])
        except ValueError:
            step_idx = int(wizard.get("step", 0))
        # Don't write a value — leave whatever was there (or absent)
        wizard["step"] = step_idx
        await _advance(context, prefix, chat_id)
        return True

    if suffix.startswith("pick_"):
        await q.answer()
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        rest = suffix[len("pick_"):]
        # rest = "{step_idx}_{value}" — value may contain underscores
        head, _, value = rest.partition("_")
        try:
            step_idx = int(head)
        except ValueError:
            return True
        step = spec.steps[step_idx]
        # Для bool-шагов кнопки шлют 'true'/'false' строкой — кастуем в bool
        # чтобы _apply_changes (Config.from_dict / setattr) получил правильный тип.
        # Choice-шаги остаются строкой как раньше.
        cast_value: Any = value
        if step.kind == "bool":
            try:
                cast_value = parse_step_value(step, value)
            except ValueError:
                cast_value = value.lower() == "true"
        wizard.setdefault("values", {})[step.key] = cast_value
        wizard.setdefault("changed", {})[step.key] = cast_value
        wizard["step"] = step_idx
        await _advance(context, prefix, chat_id)
        return True

    return False


# ── Mid-wizard command guard ──────────────────────────────────────

_GUARD_NOTICE = "✖️ Wizard прерван — выполняю команду."


async def abort_active_wizard(context: ContextTypes.DEFAULT_TYPE,
                              chat_id: int, bot) -> None:
    """If any wizard is open, pop it and notify the chat."""
    state = active_wizard(context)
    if state is None:
        return
    prefix, _ = state
    pop_wizard(context, prefix)
    try:
        await bot.send_message(chat_id=chat_id, text=_GUARD_NOTICE)
    except Exception:
        pass

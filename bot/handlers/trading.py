"""/short, /close, /avg + setbet/setstop/settp/setkey — торговые команды."""
import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes

from bot.handlers import wizard
from bot.handlers.wizard import Step

logger = logging.getLogger(__name__)


def _funding_line(rate: float, leverage: int) -> str:
    """One-line funding summary for post-open message."""
    if rate == 0:
        return ""
    pct = rate * 100
    daily_pct = abs(pct) * 3 * leverage
    sign = "+" if rate > 0 else ""
    icon = "💰" if rate > 0 else ("⚠️" if rate > -0.001 else "🚨")
    direction = "получаем" if rate > 0 else "платим"
    return f"{icon} Фандинг: `{sign}{pct:.4f}%`/8h → ~`{daily_pct:.1f}%` маржи/день ({direction})"


def _funding_warning(rate: float, margin: float) -> str:
    if rate >= -0.0005:
        return ""
    pct = rate * 100
    daily_cost_pct = abs(pct) * 3 * 100
    if rate <= -0.003:
        return (
            f"🚨 *Дорогой фандинг!* `{pct:.4f}%`/8h\n"
            f"Шорт платит `~{daily_cost_pct:.2f}%` от нотионала в день.\n"
            f"Рекомендуется пропустить или держать очень короткое время."
        )
    return (
        f"⚠️ *Негативный фандинг* `{pct:.4f}%`/8h — шорт платит.\n"
        f"Дневные расходы: `~{daily_cost_pct:.2f}%` нотионала."
    )


def _calc_tp_price(entry: float, leverage: int, tp_pct: float, side: str) -> float:
    move = entry * tp_pct / 100 / leverage
    return entry - move if side == "short" else entry + move


def _calc_sl_price(entry: float, leverage: int, sl_pct: float, side: str) -> float:
    move = entry * sl_pct / 100 / leverage
    return entry + move if side == "short" else entry - move


async def execute_open(client, app, symbol: str, side: str,
                       margin: float, leverage: int | None = None,
                       tp_pct: float = 500, sl_pct: float = 500) -> dict:
    """Open a futures position with TP/SL and register re-entry."""
    config = app.bot_data.get("config")

    if leverage is None or leverage <= 0:
        try:
            leverage = await client.get_max_leverage(symbol)
        except Exception:
            leverage = 25

    order = await client.place_futures_order(symbol, side, margin, leverage)
    actual_lev = order.get("leverage", leverage) or leverage

    await asyncio.sleep(2)
    pos = await client.get_position(symbol)

    tp_price = sl_price = None
    entry = order.get("price", 0)
    liq = 0

    if pos:
        entry = pos["entry_price"]
        liq = pos.get("liquidation_price", 0)
        pos_side = pos["side"]
        tp_price = _calc_tp_price(entry, actual_lev, tp_pct, pos_side)
        sl_price = _calc_sl_price(entry, actual_lev, sl_pct, pos_side)
        try:
            await client.cancel_tp_sl_orders(symbol)
            await client.set_tp_sl(symbol, tp_price=tp_price, sl_price=sl_price)
        except Exception as e:
            logger.warning("TP/SL set failed for %s: %s", symbol, e)

    from bot import db as db_mod
    fsym = client.futures_symbol(symbol)
    max_avg_count = int(getattr(config, "max_averaging_count", 100)) if config else 100
    avg_amount = float(getattr(config, "averaging_amount", 0.5)) if config else 0.5
    budget = max_avg_count * avg_amount
    db_mod.upsert_position(
        symbol=fsym, side=side if side in ("long", "short") else ("short" if side == "sell" else "long"),
        entry_price=entry, leverage=actual_lev, margin=margin,
        tp_pct=tp_pct, sl_pct=sl_pct, budget=budget,
    )

    db_mod.log_trade(fsym, "open", amount=margin, note=f"lev={actual_lev}")

    hist_side = "short" if side == "sell" else "long"
    db_mod.open_position_history(
        fsym, hist_side, actual_lev, entry, margin,
        tp_pct=tp_pct, sl_pct=sl_pct,
        avg_threshold=float(getattr(config, "averaging_threshold", -100)) if config else -100,
        avg_amount=float(getattr(config, "averaging_amount", 0)) if config else 0,
        avg_budget=budget,
        avg_max_count=max_avg_count,
        avg_interval=int(getattr(config, "averaging_interval", 0)) if config else 0,
    )

    max_cycles = int(getattr(config, "max_reentry_cycles", 3)) if config else 3
    if max_cycles == 0:
        db_mod.delete_reentry(fsym)
    else:
        db_mod.upsert_reentry(
            symbol=fsym,
            side=side,
            margin=margin,
            leverage=actual_lev,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            max_cycles=max_cycles,
            cycle_count=0,
        )

    tp_sl_pcts = app.bot_data.setdefault("tp_sl_pcts", {})
    tp_sl_pcts[fsym] = {"tp_pct": tp_pct, "sl_pct": sl_pct}

    return {
        "symbol": symbol,
        "side": side,
        "margin": margin,
        "leverage": actual_lev,
        "entry_price": entry,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "liquidation_price": liq,
        "order_id": order.get("id"),
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
    }


# ── /short ────────────────────────────────────────────────────────

async def _open_short(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                      symbol_raw: str, margin: float | None) -> None:
    """Shared open-short flow. ``margin=None`` → use config default."""
    config = context.bot_data["config"]
    client = context.bot_data["exchange"]
    if margin is None:
        margin = float(getattr(config, "default_trade_usdt", 0.20))
    tp_pct = float(getattr(config, "tp_pct", 500))
    sl_pct = float(getattr(config, "sl_pct", 500))

    # Если контракт не найден — НЕ продолжаем со фейковым символом (это раньше
    # давало криптические ошибки биржи). Подсказываем близкие тикеры.
    from bot.ai.scanner import mexc_find_futures_symbol, mexc_suggest_tickers
    sym = await mexc_find_futures_symbol(client, symbol_raw)
    if not sym:
        suggestions = await mexc_suggest_tickers(client, symbol_raw, n=3)
        if suggestions:
            hint = ", ".join(f"`{s}`" for s in suggestions)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Фьючерс `{symbol_raw}` не найден на MEXC.\n"
                     f"Может имел в виду: {hint}?\n"
                     f"Попробуй: `/short {suggestions[0]}`",
                parse_mode="Markdown",
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Фьючерс `{symbol_raw}` не найден на MEXC и нет похожих тикеров.",
                parse_mode="Markdown",
            )
        return

    coin = sym.split("/")[0]

    funding_warn = ""
    rate = 0.0
    try:
        fr = await client.get_funding_rate(sym)
        rate = fr["rate"]
        funding_warn = _funding_warning(rate, margin)
    except Exception:
        pass

    if funding_warn:
        await context.bot.send_message(chat_id=chat_id, text=funding_warn,
                                       parse_mode="Markdown")

    # Budget check: (margin + avg_budget) * sl_pct/100 = worst-case capital at risk
    try:
        free = await client.get_free_futures_balance()
    except Exception:
        free = float(context.bot_data.get("_bal_cache", 0.0))
    avg_amount = float(getattr(config, "averaging_amount", 0.10))
    avg_budget = float(getattr(config, "averaging_budget", 5.00))
    profit_lock_trigger = float(getattr(config, "averaging_profit_lock_trigger", 0))
    base_budget = margin + avg_budget
    full_budget = base_budget if profit_lock_trigger > 0 else base_budget * (sl_pct / 100.0)
    max_steps = int(avg_budget / avg_amount) if avg_amount > 0 else 0
    if free < margin:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Недостаточно баланса: `${free:.2f}` < маржа `${margin:.2f}`",
            parse_mode="Markdown"
        )
        return
    if free < full_budget:
        positions_possible = int(free / full_budget) if full_budget > 0 else 0
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        margin_milli = int(margin * 1000)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"⚠️ Открыть ({positions_possible} полных поз доступно)",
                callback_data=f"open_confirm_sell_{margin_milli}_{sym}"
            )
        ]])
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ *{coin}* SHORT: недостаточный бюджет\n"
                f"Свободно `${free:.2f}` · нужно `${full_budget:.2f}` на 1 поз\n"
                f"_(маржа+докупки `${base_budget:.2f}` × SL {sl_pct:.0f}%)_\n"
                f"Хватит на `{positions_possible}` полных позиций. Открыть всё равно?"
            ),
            parse_mode="Markdown",
            reply_markup=kb
        )
        return

    await context.bot.send_message(chat_id=chat_id,
                                   text=f"🔻 Открываю SHORT `{coin}` ${margin:g}...",
                                   parse_mode="Markdown")
    try:
        result = await execute_open(
            client, context.application, sym, "sell", margin,
            tp_pct=tp_pct, sl_pct=sl_pct,
        )
        lines = [
            f"*{coin}* 🔻×{result['leverage']} `${margin:.2f}`",
            f"▶ Entry: `{result['entry_price']:.6g}`",
        ]
        if result.get("liquidation_price"):
            lines.append(f"💀 Liq: `{result['liquidation_price']:.6g}`")
        if result.get("tp_price"):
            lines.append(f"✅ TP: `{result['tp_price']:.6g}` (+{tp_pct:.0f}%)")
        if result.get("sl_price"):
            lines.append(f"🛑 SL: `{result['sl_price']:.6g}` (-{sl_pct:.0f}%)")
        funding = _funding_line(rate, result["leverage"])
        if funding:
            lines.append(funding)
        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines),
                                       parse_mode="Markdown")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Ошибка: {e}")


SHORT_STEPS: list[Step] = [
    Step(key="symbol", prompt="Тикер монеты (BTC, SOL, …)", kind="text",
         optional=False, parser=lambda s: s.strip().upper()),
    Step(key="margin", attr="default_trade_usdt",
         prompt="Маржа в USDT (SKIP — использовать дефолт)",
         kind="float", unit="$", optional=True),
]


async def _short_finish(context, chat_id: int, wizard_state: dict) -> None:
    values = wizard_state.get("values", {}) or {}
    symbol = values.get("symbol")
    if not symbol:
        await context.bot.send_message(chat_id=chat_id, text="❌ Тикер не указан.")
        return
    margin = values.get("margin")  # None → default in _open_short
    await _open_short(context, chat_id, symbol, margin)


wizard.register("short", SHORT_STEPS, _short_finish)


async def short_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/short [SYMBOL [amount]] — открыть шорт. Без args — визард."""
    args = context.args or []
    if not args:
        wizard.start_wizard(context, "short")
        await wizard.render_step(context.bot, update.message.chat_id, "short", 0, context)
        return

    symbol_raw = args[0].upper()
    margin = float(args[1]) if len(args) > 1 else None
    await _open_short(context, update.message.chat_id, symbol_raw, margin)


# ── /close ────────────────────────────────────────────────────────

async def _close_symbol(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                        symbol_raw: str) -> None:
    client = context.bot_data["exchange"]
    from bot.ai.scanner import mexc_find_futures_symbol
    sym = await mexc_find_futures_symbol(client, symbol_raw)
    if not sym:
        sym = client.futures_symbol(symbol_raw)

    coin = sym.split("/")[0]
    await context.bot.send_message(chat_id=chat_id, text=f"Закрываю `{coin}`...",
                                   parse_mode="Markdown")
    try:
        pos = await client.get_position(sym)
        pnl = float(pos.get("unrealized_pnl", 0)) if pos else 0.0
        margin = float(pos.get("margin", 0)) if pos else 0.0
        exit_price = float(pos.get("mark_price", 0)) if pos else 0.0
        await client.cancel_tp_sl_orders(sym)
        await client.close_futures_position(sym)
        from bot import db as db_mod
        db_mod.close_position(sym)
        db_mod.delete_reentry(sym)
        db_mod.log_trade(sym, "close", amount=margin, pnl=pnl, note="manual")
        db_mod.close_position_history(sym, exit_price, pnl, "manual")
        await context.bot.send_message(chat_id=chat_id, text=f"✅ *{coin}* закрыт.",
                                       parse_mode="Markdown")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Ошибка закрытия: {e}")


def _close_choices(context: ContextTypes.DEFAULT_TYPE) -> list[tuple[str, str]]:
    return list(context.user_data.get("_close_choices", []))


CLOSE_STEPS: list[Step] = [
    Step(key="coin", prompt="Какую позицию закрыть?",
         kind="choice", optional=False, choices=_close_choices),
]


async def _close_finish(context, chat_id: int, wizard_state: dict) -> None:
    values = wizard_state.get("values", {}) or {}
    coin = values.get("coin")
    context.user_data.pop("_close_choices", None)
    if not coin:
        await context.bot.send_message(chat_id=chat_id, text="✖️ Отменено.")
        return
    await _close_symbol(context, chat_id, coin)


wizard.register("close", CLOSE_STEPS, _close_finish)


async def close_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/close [SYMBOL] — закрыть позицию. Без args — визард с выбором из открытых."""
    args = context.args or []
    if args:
        await _close_symbol(context, update.message.chat_id, args[0].upper())
        return

    client = context.bot_data["exchange"]
    try:
        positions = await client.get_positions()
    except Exception as e:
        await update.message.reply_text(f"❌ Не получилось получить позиции: {e}")
        return

    if not positions:
        await update.message.reply_text("Нет открытых позиций.")
        return

    choices: list[tuple[str, str]] = []
    for p in positions:
        coin = p["symbol"].split("/")[0]
        side = p.get("side", "")
        icon = "🔻" if side == "short" else "🟩"
        pnl = p.get("unrealized_pnl")
        try:
            pnl_str = f" ({float(pnl):+.2f}$)" if pnl is not None else ""
        except (TypeError, ValueError):
            pnl_str = ""
        choices.append((f"{icon} {coin}{pnl_str}", coin))

    context.user_data["_close_choices"] = choices
    wizard.start_wizard(context, "close")
    await wizard.render_step(context.bot, update.message.chat_id, "close", 0, context)


# ── /avg ──────────────────────────────────────────────────────────

AVG_STEPS: list[Step] = [
    Step(key="default_trade_usdt",  attr="default_trade_usdt",
         prompt="Маржа на сделку",         kind="float", unit="$"),
    Step(key="default_leverage",    attr="default_leverage",
         prompt="Плечо (0=макс)",          kind="int:0:200", unit="#"),
    Step(key="tp_pct",              attr="tp_pct",
         prompt="Тейкпрофит",              kind="float", unit="%"),
    Step(key="sl_pct",              attr="sl_pct",
         prompt="Стоплосс",                kind="float", unit="%"),
    Step(key="averaging_threshold", attr="averaging_threshold",
         prompt="Докупка при PnL",         kind="float", unit="%"),
    Step(key="averaging_amount",    attr="averaging_amount",
         prompt="Сумма докупки",           kind="float", unit="$"),
    Step(key="max_averaging_count", attr="max_averaging_count",
         prompt="Макс докупок",            kind="int:0:1000", unit="#"),
    Step(key="max_reentry_cycles",  attr="max_reentry_cycles",
         prompt="Перезаходов макс",        kind="int:0:1000", unit="#"),
    # Опасный флаг: True = перезаходить даже после loss-SL. Дефолт False.
    # См. bot/jobs/main.py reentry_job — ветка `not closed_by_tp and not profitable_sl`.
    Step(key="reenter_on_loss_sl",  attr="reenter_on_loss_sl",
         prompt="Перезаход после loss-SL (опасно)", kind="bool"),
    # Profit-lock: при достижении PnL ≥ trigger переставить SL на lock_sl PnL.
    # 0 в trigger = выкл (используется prod jobs/main.py averaging_job).
    Step(key="averaging_profit_lock_trigger", attr="averaging_profit_lock_trigger",
         prompt="Локк SL при профите ≥ (0=выкл)", kind="float", unit="%"),
    Step(key="averaging_profit_lock_sl_pct",  attr="averaging_profit_lock_sl_pct",
         prompt="Поставить SL на PnL",            kind="float", unit="%"),
    # Auto-scan capital risk: 0 = требовать полный бюджет, 100 = всегда открывать.
    Step(key="auto_scan_capital_pct",         attr="auto_scan_capital_pct",
         prompt="Авто-капитал риск (0..100)",     kind="float", unit="%"),
]


def _build_avg_text(config) -> str:
    # Помимо базовых настроек — отображаем профит-локк и авто-капитал риск
    # (поля принесены из prod вместе с config-keys и ловятся jobs/main.py averaging_job).
    lock_trig = float(getattr(config, "averaging_profit_lock_trigger", 0) or 0)
    lock_sl   = float(getattr(config, "averaging_profit_lock_sl_pct", 0) or 0)
    scan_cap  = float(getattr(config, "auto_scan_capital_pct", 0) or 0)
    lock_line = (f"  Профит-локк: при `+{lock_trig:.0f}%` → SL в `+{lock_sl:.0f}%`"
                 if lock_trig > 0 else "  Профит-локк: выкл")
    scan_line = f"  Авто-капитал риск: `{scan_cap:.0f}%`"
    lines = [
        "*Торговые настройки*",
        "",
        "*Вход*",
        f"  Маржа: `${config.default_trade_usdt:.2f}`",
        f"  Плечо: `{'макс' if not config.default_leverage else f'×{config.default_leverage}'}`",
        f"  TP:    `{config.tp_pct:.0f}%`",
        f"  SL:    `{config.sl_pct:.0f}%`",
        "",
        "*Докупка*",
        f"  При PnL: `{config.averaging_threshold:.0f}%`",
        f"  Сумма:   `${config.averaging_amount:.2f}`",
        f"  Макс:    `{config.max_averaging_count}` докупок",
        f"  Интервал:`{config.averaging_interval}s`",
        lock_line,
        scan_line,
    ]
    return "\n".join(lines)


def _avg_intro(context: ContextTypes.DEFAULT_TYPE) -> str:
    config = context.bot_data.get("config")
    if config is None:
        return ""
    return _build_avg_text(config)


async def _avg_finish(context, chat_id: int, wizard_state: dict) -> None:
    config = context.bot_data["config"]
    changed = wizard_state.get("changed", {}) or {}
    if not changed:
        await context.bot.send_message(chat_id=chat_id, text="Ничего не изменено.")
        return

    from bot import db as db_mod
    for attr, value in changed.items():
        setattr(config, attr, value)
        db_mod.set_config(attr, str(value))

    lines = ["✅ *Настройки обновлены:*", ""]
    for step in AVG_STEPS:
        if step.key not in changed:
            continue
        lines.append(f"  {step.prompt}: `{wizard.format_value(step, changed[step.key])}`")
    lines.append("")
    lines.append(_build_avg_text(config))
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines),
                                   parse_mode="Markdown")

    if "tp_pct" in changed or "sl_pct" in changed:
        await _reapply_tpsl_all(context.application, config)
    if "averaging_amount" in changed or "max_averaging_count" in changed:
        new_budget = config.max_averaging_count * config.averaging_amount
        with db_mod._connect() as conn:
            conn.execute("UPDATE positions SET averaging_budget=? WHERE status='open'",
                         (new_budget,))


wizard.register("avg", AVG_STEPS, _avg_finish, intro=_avg_intro)


_AVG_PARAM_TO_ATTR = {
    "bet":          ("default_trade_usdt", float),
    "leverage":     ("default_leverage", int),
    "tp":           ("tp_pct", float),
    "sl":           ("sl_pct", float),
    "threshold":    ("averaging_threshold", float),
    "amount":       ("averaging_amount", float),
    "interval":     ("averaging_interval", int),
    "maxavg":       ("max_averaging_count", int),
    # Принесено из prod: profit-lock + auto-scan capital — позволяет
    # /avg lock_trigger 100 / /avg lock_sl 50 / /avg scan_cap 50 без визарда.
    "lock_trigger": ("averaging_profit_lock_trigger", float),
    "lock_sl":      ("averaging_profit_lock_sl_pct", float),
    "scan_cap":     ("auto_scan_capital_pct", float),
    # bool: 1/yes/true/вкл = True, 0/no/false/выкл = False (см. _bool_arg ниже)
    "reenter_on_loss_sl": ("reenter_on_loss_sl", "bool"),
}


def _bool_arg(s: str) -> bool:
    """Принимает '1'/'0'/'true'/'false'/'on'/'off'/'вкл'/'выкл' и т.п."""
    v = str(s).strip().lower()
    if v in ("1", "true", "yes", "y", "on", "вкл", "включи", "включить"):
        return True
    if v in ("0", "false", "no", "n", "off", "выкл", "выключи", "выключить"):
        return False
    raise ValueError(f"ожидался bool, получено: {s}")


async def avg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/avg [param] [value] — все торговые настройки. Без args/неизвестный — визард."""
    config = context.bot_data["config"]
    args = context.args or []

    if len(args) < 2 or args[0].lower() not in _AVG_PARAM_TO_ATTR:
        wizard.start_wizard(context, "avg")
        await wizard.send_intro(context.bot, update.message.chat_id, "avg", context)
        await wizard.render_step(context.bot, update.message.chat_id, "avg", 0, context)
        return

    param, val_str = args[0].lower(), args[1]
    if param not in _AVG_PARAM_TO_ATTR:
        await update.message.reply_text(f"Неизвестный параметр: `{param}`",
                                        parse_mode="Markdown")
        return

    attr, cast = _AVG_PARAM_TO_ATTR[param]
    # Маркер "bool" → особая обработка через _bool_arg, остальные cast — float/int.
    try:
        if cast == "bool":
            casted = _bool_arg(val_str)
            db_value = "true" if casted else "false"
        else:
            val = float(val_str)
            casted = cast(val)
            db_value = str(val)
    except ValueError as e:
        await update.message.reply_text(f"Неверное значение: {val_str} ({e})")
        return

    setattr(config, attr, casted)
    from bot import db as db_mod
    db_mod.set_config(attr, db_value)

    label_map = {"bet": f"${casted:.2f}" if isinstance(casted, (int, float)) else str(casted),
                 "tp":  f"{casted:.0f}%" if isinstance(casted, (int, float)) else str(casted),
                 "sl":  f"{casted:.0f}%" if isinstance(casted, (int, float)) else str(casted)}
    label = label_map.get(param, ("ВКЛ" if casted else "ВЫКЛ") if isinstance(casted, bool) else str(casted))
    await update.message.reply_text(f"✅ `{param}` = `{label}`", parse_mode="Markdown")

    if param in ("tp", "sl"):
        await _reapply_tpsl_all(context.application, config)

    if param == "interval":
        from bot.jobs.main import reschedule_averaging
        reschedule_averaging(context.application, int(casted))

    if param in ("amount", "maxavg"):
        new_budget = config.max_averaging_count * config.averaging_amount
        with db_mod._connect() as conn:
            conn.execute("UPDATE positions SET averaging_budget=? WHERE status='open'",
                         (new_budget,))


async def _reapply_tpsl_all(app, config) -> None:
    """Re-apply TP/SL to all open positions after tp_pct/sl_pct change."""
    from bot import db as db_mod
    from bot.jobs.main import _calc_tp_price, _calc_sl_price
    client = app.bot_data.get("exchange")
    if not client:
        return
    tp_pct = float(getattr(config, "tp_pct", 500))
    sl_pct = float(getattr(config, "sl_pct", 500))
    tp_sl_pcts: dict = app.bot_data.setdefault("tp_sl_pcts", {})
    try:
        positions = await client.get_positions()
    except Exception:
        return
    for pos in positions:
        symbol = pos["symbol"]
        entry = float(pos.get("entry_price", 0) or 0)
        lev = int(pos.get("leverage", 1) or 1)
        side = pos.get("side", "short")
        if not entry:
            continue
        tp_price = _calc_tp_price(entry, lev, tp_pct, side)
        sl_price = _calc_sl_price(entry, lev, sl_pct, side)
        try:
            await client.cancel_tp_sl_orders(symbol)
            await client.set_tp_sl(symbol, tp_price=tp_price, sl_price=sl_price, pos_data=pos)
            tp_sl_pcts[symbol] = {"tp_pct": tp_pct, "sl_pct": sl_pct}
            db_mod.update_position_tpsl(symbol, tp_pct, sl_pct)
            with db_mod._connect() as conn:
                conn.execute(
                    "UPDATE reentry SET tp_pct=?, sl_pct=? WHERE symbol=?",
                    (tp_pct, sl_pct, symbol),
                )
        except Exception as e:
            logger.warning("reapply tpsl %s: %s", symbol, e)


async def avg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await wizard.handle_callback(update, context, "avg")


# ── Single-step setting wizards ───────────────────────────────────

SETBET_STEPS: list[Step] = [
    Step(key="default_trade_usdt", attr="default_trade_usdt",
         prompt="Ставка в USDT (например 1.50)", kind="float", unit="$"),
]


async def _setbet_finish(context, chat_id: int, wizard_state: dict) -> None:
    config = context.bot_data["config"]
    changed = wizard_state.get("changed", {}) or {}
    if "default_trade_usdt" not in changed:
        await context.bot.send_message(chat_id=chat_id, text="Без изменений.")
        return
    val = float(changed["default_trade_usdt"])
    config.default_trade_usdt = val
    from bot import db as db_mod
    db_mod.set_config("default_trade_usdt", str(val))
    await context.bot.send_message(chat_id=chat_id,
                                   text=f"✅ *Ставка* применена: `${val:.2f}`",
                                   parse_mode="Markdown")


wizard.register("setbet", SETBET_STEPS, _setbet_finish)


SETSTOP_STEPS: list[Step] = [
    Step(key="sl_pct", attr="sl_pct",
         prompt="Стоплосс в % от маржи (например 500)", kind="float", unit="%"),
]


async def _setstop_finish(context, chat_id: int, wizard_state: dict) -> None:
    config = context.bot_data["config"]
    changed = wizard_state.get("changed", {}) or {}
    if "sl_pct" not in changed:
        await context.bot.send_message(chat_id=chat_id, text="Без изменений.")
        return
    val = float(changed["sl_pct"])
    config.sl_pct = val
    from bot import db as db_mod
    db_mod.set_config("sl_pct", str(val))
    await context.bot.send_message(chat_id=chat_id,
                                   text=f"✅ *Стоплосс* применён: `{val:.0f}%`",
                                   parse_mode="Markdown")
    await _reapply_tpsl_all(context.application, config)


wizard.register("setstop", SETSTOP_STEPS, _setstop_finish)


SETTP_STEPS: list[Step] = [
    Step(key="tp_pct", attr="tp_pct",
         prompt="Тейкпрофит в % от маржи (например 500)", kind="float", unit="%"),
]


async def _settp_finish(context, chat_id: int, wizard_state: dict) -> None:
    config = context.bot_data["config"]
    changed = wizard_state.get("changed", {}) or {}
    if "tp_pct" not in changed:
        await context.bot.send_message(chat_id=chat_id, text="Без изменений.")
        return
    val = float(changed["tp_pct"])
    config.tp_pct = val
    from bot import db as db_mod
    db_mod.set_config("tp_pct", str(val))
    await context.bot.send_message(chat_id=chat_id,
                                   text=f"✅ *Тейкпрофит* применён: `{val:.0f}%`",
                                   parse_mode="Markdown")
    await _reapply_tpsl_all(context.application, config)


wizard.register("settp", SETTP_STEPS, _settp_finish)


async def setbet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setbet — однокнопочный визард для ставки."""
    wizard.start_wizard(context, "setbet")
    await wizard.render_step(context.bot, update.message.chat_id, "setbet", 0, context)


async def setstop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setstop — однокнопочный визард для стоплосса."""
    wizard.start_wizard(context, "setstop")
    await wizard.render_step(context.bot, update.message.chat_id, "setstop", 0, context)


async def settp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/settp — однокнопочный визард для тейкпрофита."""
    wizard.start_wizard(context, "settp")
    await wizard.render_step(context.bot, update.message.chat_id, "settp", 0, context)


# ── /setkey ───────────────────────────────────────────────────────

def _setkey_parse_key(text: str) -> str:
    return text.strip()


SETKEY_STEPS: list[Step] = [
    Step(key="key", prompt="Имя ключа конфига (напр. `openrouter_api_key`)",
         kind="text", optional=False, parser=_setkey_parse_key),
    Step(key="value", prompt="Значение", kind="text", optional=False),
]


def _coerce_to_attr_type(config, key: str, value: str):
    attr = getattr(config, key, None)
    if isinstance(attr, bool):
        return value.lower() in ("1", "true", "yes", "on", "вкл", "да")
    if isinstance(attr, int):
        try:
            return int(value)
        except ValueError:
            return value
    if isinstance(attr, float):
        try:
            return float(value)
        except ValueError:
            return value
    return value


async def _setkey_finish(context, chat_id: int, wizard_state: dict) -> None:
    config = context.bot_data["config"]
    values = wizard_state.get("values", {}) or {}
    key = values.get("key")
    value = values.get("value")
    if not key or value is None:
        await context.bot.send_message(chat_id=chat_id, text="Не хватает данных.")
        return
    if not hasattr(config, key):
        await context.bot.send_message(chat_id=chat_id,
                                       text=f"❌ Неизвестный ключ: `{key}`",
                                       parse_mode="Markdown")
        return
    from bot import db as db_mod
    db_mod.set_config(key, value)
    coerced = _coerce_to_attr_type(config, key, value)
    setattr(config, key, coerced)
    await context.bot.send_message(chat_id=chat_id,
                                   text=f"✅ Сохранено: `{key}` = `{value}`",
                                   parse_mode="Markdown")


wizard.register("setkey", SETKEY_STEPS, _setkey_finish)


async def setkey_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setkey [KEY [VALUE]] — сохранить ключ в конфиг.

    Без args — визард. С 1 arg — визард с предзаполненным KEY. С 2+ args — прямой путь.
    """
    args = context.args or []
    config = context.bot_data["config"]

    if len(args) >= 2:
        key, value = args[0], " ".join(args[1:])
        if not hasattr(config, key):
            await update.message.reply_text(f"Неизвестный ключ: {key}")
            return
        from bot import db as db_mod
        db_mod.set_config(key, value)
        coerced = _coerce_to_attr_type(config, key, value)
        setattr(config, key, coerced)
        await update.message.reply_text(f"✅ Сохранено: `{key}` = `{value}`",
                                        parse_mode="Markdown")
        return

    initial = {"key": args[0]} if len(args) == 1 else None
    state = wizard.start_wizard(context, "setkey", initial_values=initial)
    if initial:
        # Skip the first step
        state["step"] = 1
    await wizard.render_step(context.bot, update.message.chat_id, "setkey",
                             state["step"], context)


# ── /setmexc — двухшаговая замена MEXC ключей с проверкой ────────
# Это НЕ wizard.register-визард, а stateful диалог через context.user_data["pending_mexc"].
# Причина: secret и api_key вводятся обычным текстом, и сообщение пользователя должно быть
# удалено сразу после получения (free-form input + side-effect delete). Wizard framework не
# поддерживает такую логику. Обработка ввода живёт в bot/handlers/assistant.py:assistant_handler
# (блок `pending_mexc`).
async def setmexc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setmexc — заменить MEXC API key+secret с предварительной проверкой."""
    context.user_data["pending_mexc"] = {"step": "secret"}
    # Очистим возможный остаток от предыдущей попытки.
    context.user_data.pop("_mexc_secret", None)
    await update.message.reply_text(
        "🔐 *Замена MEXC ключей*\n\n"
        "Шаг 1/2 — введи *secret* (из настроек MEXC API).\n\n"
        "_Сообщение с ключом удалю сразу после получения._\n"
        "Напиши `отмена` чтобы прервать.",
        parse_mode="Markdown",
    )

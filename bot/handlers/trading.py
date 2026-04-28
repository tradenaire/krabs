"""/short, /close, /avg — торговые команды."""
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def _funding_line(rate: float, leverage: int) -> str:
    """One-line funding summary for post-open message."""
    if rate == 0:
        return ""
    pct = rate * 100
    daily_pct = abs(pct) * 3 * leverage  # 3 periods/day × leverage impact on margin
    sign = "+" if rate > 0 else ""
    icon = "💰" if rate > 0 else ("⚠️" if rate > -0.001 else "🚨")
    direction = "получаем" if rate > 0 else "платим"
    return f"{icon} Фандинг: `{sign}{pct:.4f}%`/8h → ~`{daily_pct:.1f}%` маржи/день ({direction})"


def _funding_warning(rate: float, margin: float) -> str:
    """Pre-open warning message if funding is costly."""
    if rate >= -0.0005:  # better than -0.05% → no warning needed
        return ""
    pct = rate * 100
    daily_cost_pct = abs(pct) * 3 * 100  # rough: per 100% notional (ignoring leverage)
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
    """entry + leverage move that gives tp_pct PnL on margin."""
    move = entry * tp_pct / 100 / leverage
    return entry - move if side == "short" else entry + move


def _calc_sl_price(entry: float, leverage: int, sl_pct: float, side: str) -> float:
    """entry + leverage move that gives -sl_pct PnL on margin."""
    move = entry * sl_pct / 100 / leverage
    return entry + move if side == "short" else entry - move


async def execute_open(client, app, symbol: str, side: str,
                       margin: float, leverage: int | None = None,
                       tp_pct: float = 500, sl_pct: float = 500) -> dict:
    """Open a futures position with TP/SL and register re-entry."""
    config = app.bot_data.get("config")

    # Resolve max leverage if not given
    if leverage is None or leverage <= 0:
        try:
            leverage = await client.get_max_leverage(symbol)
        except Exception:
            leverage = 25

    order = await client.place_futures_order(symbol, side, margin, leverage)
    actual_lev = order.get("leverage", leverage) or leverage

    # Wait for MEXC to settle the position
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

    # Persist in DB
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

    # Log to stats
    db_mod.log_trade(fsym, "open", amount=margin, note=f"lev={actual_lev}")

    # Full position history
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

    # Register re-entry (skip if disabled)
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

    # Store tp_sl_pcts for averaging recalc
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


async def short_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/short SYMBOL [amount] — открыть шорт с максимальным плечом."""
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /short SYMBOL [amount_usdt]")
        return

    symbol_raw = args[0].upper()
    config = context.bot_data["config"]
    client = context.bot_data["exchange"]
    default_margin = float(getattr(config, "default_trade_usdt", 0.20))
    margin = float(args[1]) if len(args) > 1 else default_margin
    tp_pct = float(getattr(config, "tp_pct", 500))
    sl_pct = float(getattr(config, "sl_pct", 500))

    from bot.ai.scanner import mexc_find_futures_symbol
    sym = await mexc_find_futures_symbol(client, symbol_raw)
    if not sym:
        sym = client.futures_symbol(symbol_raw)

    coin = sym.split("/")[0]

    # Fetch funding rate before opening — warn if negative
    funding_warn = ""
    try:
        fr = await client.get_funding_rate(sym)
        rate = fr["rate"]
        funding_warn = _funding_warning(rate, margin)
    except Exception:
        rate = 0.0

    if funding_warn:
        await update.message.reply_text(funding_warn, parse_mode="Markdown")

    await update.message.reply_text(f"🔻 Открываю SHORT `{coin}` ${margin:g}...",
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
        lines.append(_funding_line(rate, result["leverage"]))
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def close_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/close SYMBOL — закрыть позицию."""
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /close SYMBOL")
        return

    symbol_raw = args[0].upper()
    client = context.bot_data["exchange"]

    from bot.ai.scanner import mexc_find_futures_symbol
    sym = await mexc_find_futures_symbol(client, symbol_raw)
    if not sym:
        sym = client.futures_symbol(symbol_raw)

    coin = sym.split("/")[0]
    await update.message.reply_text(f"Закрываю `{coin}`...", parse_mode="Markdown")
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
        await update.message.reply_text(f"✅ *{coin}* закрыт.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка закрытия: {e}")


# ── Avg wizard ────────────────────────────────────────────────────

AVG_WIZARD_STEPS = [
    ("bet",       "default_trade_usdt",   float, "Маржа на сделку",   "$"),
    ("leverage",  "default_leverage",     int,   "Плечо (0=макс)",    "#"),
    ("tp",        "tp_pct",               float, "Тейкпрофит",        "%"),
    ("sl",        "sl_pct",               float, "Стоплосс",          "%"),
    ("threshold", "averaging_threshold",  float, "Докупка при PnL",   "%"),
    ("amount",    "averaging_amount",     float, "Сумма докупки",     "$"),
    ("maxavg",    "max_averaging_count",  int,   "Макс докупок",      "#"),
    ("reentry",   "max_reentry_cycles",   int,   "Перезаходов макс",  "#"),
]


def _avg_fmt(config, attr: str, unit: str) -> str:
    val = getattr(config, attr, 0)
    if unit == "$":
        return f"${float(val):.2f}"
    if unit == "#":
        return str(int(val))
    return f"{float(val):.0f}%"


async def send_avg_wizard_step(bot, chat_id: int, step_idx: int, config) -> None:
    key, attr, _, label, unit = AVG_WIZARD_STEPS[step_idx]
    cur = _avg_fmt(config, attr, unit)
    total = len(AVG_WIZARD_STEPS)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭ Пропустить", callback_data=f"avg_skip_{step_idx}"),
        InlineKeyboardButton("✖ Отмена", callback_data="avg_cancel"),
    ]])
    await bot.send_message(
        chat_id=chat_id,
        text=f"*{label}* ({step_idx + 1}/{total})\nСейчас: `{cur}`\nВведи новое значение:",
        parse_mode="Markdown",
        reply_markup=kb,
    )


def _build_avg_text(config) -> str:
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
    ]
    return "\n".join(lines)


async def avg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/avg [param] [value] — все торговые настройки."""
    config = context.bot_data["config"]
    args = context.args or []

    if not args:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ Изменить", callback_data="avg_edit"),
        ]])
        await update.message.reply_text(_build_avg_text(config),
                                        parse_mode="Markdown", reply_markup=kb)
        return

    if len(args) < 2:
        await update.message.reply_text(
            "Использование: `/avg param value`\n"
            "Параметры: `bet`, `tp`, `sl`, `threshold`, `amount`, `budget`, `interval`",
            parse_mode="Markdown"
        )
        return

    param, val_str = args[0].lower(), args[1]
    try:
        val = float(val_str)
    except ValueError:
        await update.message.reply_text(f"Неверное значение: {val_str}")
        return

    field_map = {
        "bet":       ("default_trade_usdt", float),
        "leverage":  ("default_leverage", int),
        "tp":        ("tp_pct", float),
        "sl":        ("sl_pct", float),
        "threshold": ("averaging_threshold", float),
        "amount":    ("averaging_amount", float),
        "interval":  ("averaging_interval", int),
        "maxavg":    ("max_averaging_count", int),
    }
    if param not in field_map:
        await update.message.reply_text(
            f"Неизвестный параметр: `{param}`\n"
            "Доступны: `bet`, `leverage`, `tp`, `sl`, `threshold`, `amount`, `interval`, `maxavg`",
            parse_mode="Markdown"
        )
        return

    attr, cast = field_map[param]
    setattr(config, attr, cast(val))
    from bot import db as db_mod
    db_mod.set_config(attr, str(val))

    label = {"bet": f"${val:.2f}", "tp": f"{val:.0f}%", "sl": f"{val:.0f}%"}.get(param, str(val))
    await update.message.reply_text(f"✅ `{param}` = `{label}`", parse_mode="Markdown")

    if param in ("tp", "sl"):
        await _reapply_tpsl_all(context.application, config)

    if param == "interval":
        from bot.jobs.main import reschedule_averaging
        reschedule_averaging(context.application, int(val))

    if param in ("amount", "maxavg"):
        new_budget = config.max_averaging_count * config.averaging_amount
        with db_mod._connect() as conn:
            conn.execute("UPDATE positions SET averaging_budget=? WHERE status='open'", (new_budget,))


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
    """Callback for /avg inline wizard."""
    q = update.callback_query
    await q.answer()
    config = context.bot_data.get("config")
    if not config:
        await q.edit_message_text("❌ Конфиг недоступен.")
        return

    if q.data == "avg_edit":
        context.user_data["avg_wizard"] = {"step": 0, "changed": {}}
        await q.edit_message_reply_markup(reply_markup=None)
        await send_avg_wizard_step(context.bot, q.message.chat_id, 0, config)
        return

    if q.data == "avg_cancel":
        context.user_data.pop("avg_wizard", None)
        await q.edit_message_text("✖ Изменение настроек отменено.")
        return

    if q.data.startswith("avg_skip_"):
        wizard = context.user_data.get("avg_wizard")
        if not wizard:
            await q.edit_message_text("Сессия устарела. Используй /avg заново.")
            return
        await q.edit_message_reply_markup(reply_markup=None)
        next_step = wizard["step"] + 1
        if next_step >= len(AVG_WIZARD_STEPS):
            context.user_data.pop("avg_wizard", None)
            await _finish_wizard(q.message.chat_id, context, wizard["changed"], config)
        else:
            wizard["step"] = next_step
            await send_avg_wizard_step(context.bot, q.message.chat_id, next_step, config)


async def _finish_wizard(chat_id: int, context, changed: dict, config) -> None:
    if not changed:
        await context.bot.send_message(chat_id=chat_id, text="Ничего не изменено.")
        return
    lines = ["✅ *Настройки обновлены:*", ""]
    unit_map = {k: u for k, _, _, _, u in AVG_WIZARD_STEPS}
    for key, val in changed.items():
        unit = unit_map.get(key, "")
        fmt = f"${val:.2f}" if unit == "$" else (str(int(val)) if unit == "#" else f"{val:.0f}%")
        labels = {"bet": "Маржа", "tp": "TP", "sl": "SL",
                  "threshold": "Докупка при", "amount": "Сумма докупки",
                  "budget": "Бюджет", "maxavg": "Макс докупок"}
        lines.append(f"  {labels.get(key, key)}: `{fmt}`")
    lines.append("")
    lines.append(_build_avg_text(config))
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")
    if changed:
        await _reapply_tpsl_all(context.application, config)
    if "averaging_amount" in changed or "max_averaging_count" in changed:
        from bot import db as db_mod
        new_budget = config.max_averaging_count * config.averaging_amount
        with db_mod._connect() as conn:
            conn.execute("UPDATE positions SET averaging_budget=? WHERE status='open'", (new_budget,))


async def setbet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setbet — запросить новую ставку."""
    context.user_data["pending_set"] = "bet"
    await update.message.reply_text(
        "Введи новую ставку в USDT (например `1.50`):", parse_mode="Markdown"
    )


async def setstop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setstop — запросить новый стоплосс."""
    context.user_data["pending_set"] = "sl"
    await update.message.reply_text(
        "Введи стоплосс в % от маржи (например `500`):", parse_mode="Markdown"
    )


async def settp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/settp — запросить новый тейкпрофит."""
    context.user_data["pending_set"] = "tp"
    await update.message.reply_text(
        "Введи тейкпрофит в % от маржи (например `500`):", parse_mode="Markdown"
    )


async def setkey_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setkey KEY VALUE — сохранить ключ в конфиг."""
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Использование: /setkey KEY VALUE")
        return
    key, value = args[0], " ".join(args[1:])
    config = context.bot_data["config"]
    if not hasattr(config, key):
        await update.message.reply_text(f"Неизвестный ключ: {key}")
        return
    from bot import db as db_mod
    db_mod.set_config(key, value)
    # Update live config
    try:
        attr = getattr(config, key)
        if isinstance(attr, float):
            setattr(config, key, float(value))
        elif isinstance(attr, int):
            setattr(config, key, int(value))
        else:
            setattr(config, key, value)
    except Exception:
        setattr(config, key, value)
    await update.message.reply_text(f"✅ Сохранено: `{key}` = `{value}`", parse_mode="Markdown")

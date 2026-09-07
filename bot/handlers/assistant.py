"""NLP text handler — free-form commands in Russian/English."""
import logging
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# TP/SL extraction patterns
_RE_TP_SL = re.compile(
    r'тп[\s:]+(\d+(?:[.,]\d+)?)\s*%.*?сл[\s:]+(\d+(?:[.,]\d+)?)\s*%',
    re.IGNORECASE
)
_RE_SL_TP = re.compile(
    r'сл[\s:]+(\d+(?:[.,]\d+)?)\s*%.*?тп[\s:]+(\d+(?:[.,]\d+)?)\s*%',
    re.IGNORECASE
)
_RE_TP_ONLY = re.compile(r'тп[\s:]+(\d+(?:[.,]\d+)?)\s*%', re.IGNORECASE)
_RE_SL_ONLY = re.compile(r'сл[\s:]+(\d+(?:[.,]\d+)?)\s*%', re.IGNORECASE)
_RE_CLOSE_ALL = re.compile(r'закр[оыийь]+й?\s+все', re.IGNORECASE)
_RE_CLOSE_SYM = re.compile(r'закр[оыийь]+й?\s+([A-Za-z0-9]+)', re.IGNORECASE)
_RE_OPEN_SYM = re.compile(r'откр[оыийь]+й?\s+([A-Za-z0-9]+)', re.IGNORECASE)
_RE_TARGET = re.compile(r'у\s+(?:всех|всё|все)\s+позиций?|у\s+всех', re.IGNORECASE)
_RE_TARGET_SYM = re.compile(r'у\s+([A-Za-z0-9]+)', re.IGNORECASE)


def _parse_float(s: str) -> float:
    return float(s.replace(",", "."))


_PENDING_PROMPTS = {
    "bet": ("default_trade_usdt", "Введи новую ставку в USDT (например `1.50`):", "$", float),
    "tp":  ("tp_pct",             "Введи тейкпрофит в % (например `500`):", "%", float),
    "sl":  ("sl_pct",             "Введи стоплосс в % (например `500`):", "%", float),
}


async def assistant_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-form text commands."""
    if not update.message or not update.message.text:
        return

    # In group chats — only react when bot is @mentioned
    chat_type = update.message.chat.type
    if chat_type in ("group", "supergroup"):
        bot_username = (await context.bot.get_me()).username
        if f"@{bot_username}" not in update.message.text:
            return

    msg = update.message.text.strip()
    lo = msg.lower()

    client = context.bot_data.get("exchange")
    app = context.application

    # ── Avg picker (single-field editor) ─────────────────────────
    avg_pending = context.user_data.get("avg_pending")
    if avg_pending is not None:
        from bot.handlers.trading import (_build_avg_select_kb, apply_avg_pending_value,
                                          avg_question_text)

        if lo in ("отмена", "стоп", "cancel", "выход"):
            context.user_data.pop("avg_pending", None)
            config = context.bot_data.get("config")
            await update.message.reply_text(
                "Выбери параметр для изменения:",
                reply_markup=_build_avg_select_kb(config),
            )
            return

        config = context.bot_data.get("config")
        try:
            result = await apply_avg_pending_value(context, avg_pending, msg)
        except ValueError as e:
            await update.message.reply_text(
                f"❌ {e}\n\n" + avg_question_text(config, avg_pending),
                parse_mode="Markdown",
            )
            return
        context.user_data.pop("avg_pending", None)
        await update.message.reply_text(
            f"✅ {result}\n\nВыбери следующий номер `1`-`14` или нажми *Готово*:",
            parse_mode="Markdown",
            reply_markup=_build_avg_select_kb(config),
        )
        return

    # ── Avg select mode: digit shortcuts ─────────────────────────
    if context.user_data.get("avg_select_mode") and msg.isdigit():
        from bot.handlers.trading import avg_pending_for_number, avg_question_text
        config = context.bot_data.get("config")
        pending = avg_pending_for_number(int(msg))
        if not pending:
            await update.message.reply_text("Номер должен быть от 1 до 14.")
            return
        context.user_data["avg_pending"] = pending
        await update.message.reply_text(
            avg_question_text(config, pending),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад", callback_data="avg_back")]]),
        )
        return

    # ── Dynamic averaging wizard ──────────────────────────────────────
    dyn = context.user_data.get("dyn_wizard")
    if dyn is not None:
        import json as _json
        from bot import db as db_mod
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton

        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("✖ Отмена", callback_data="dyn_cancel")]])

        if lo in ("отмена", "стоп", "cancel", "выход"):
            context.user_data.pop("dyn_wizard", None)
            await update.message.reply_text("✖ Настройка динамики отменена.")
            return

        phase = dyn["phase"]

        # ── Phase: enter total step count ───────────────────────────
        if phase == "count":
            try:
                n = int(float(msg.replace(",", ".")))
            except ValueError:
                await update.message.reply_text("Введи целое число (например `3`).", parse_mode="Markdown")
                return
            if n <= 0:
                db_mod.set_config("avg_dynamic_rules", "")
                context.user_data.pop("dyn_wizard", None)
                await update.message.reply_text("✅ Динамика докупки отключена.")
                return
            dyn["total"] = n
            dyn["current"] = 0
            dyn["rules"] = []
            dyn["phase"] = "after"
            await update.message.reply_text(
                f"*Ступень 1 из {n}*\nПосле скольких докупок применять это правило?\n_(0 = с самого начала)_",
                parse_mode="Markdown", reply_markup=cancel_kb,
            )
            return

        step_n = dyn["current"] + 1
        total = dyn["total"]

        # ── Phase: enter "after N averagings" ────────────────────────
        if phase == "after":
            try:
                after = int(float(msg.replace(",", ".")))
            except ValueError:
                await update.message.reply_text("Введи целое число (например `10`).", parse_mode="Markdown")
                return
            dyn["_cur_after"] = after
            dyn["phase"] = "pnl"
            await update.message.reply_text(
                f"*Ступень {step_n} из {total}*\nПри каком PnL % докупать?\n_(например `-50` = когда PnL ≤ −50%)_",
                parse_mode="Markdown", reply_markup=cancel_kb,
            )
            return

        # ── Phase: enter PnL threshold ───────────────────────────────
        if phase == "pnl":
            try:
                pnl = float(msg.replace(",", "."))
                if pnl > 0:
                    pnl = -pnl  # ensure negative
            except ValueError:
                await update.message.reply_text("Введи число (например `-50` или `50`).", parse_mode="Markdown")
                return

            dyn["_cur_pnl"] = pnl
            dyn["phase"] = "amount"
            await update.message.reply_text(
                f"*Ступень {step_n} из {total}*\nСумма докупки в $ (например `0.10`):",
                parse_mode="Markdown", reply_markup=cancel_kb,
            )
            return

        # ── Phase: enter averaging amount ───────────────────────────
        if phase == "amount":
            try:
                amt = float(msg.replace(",", "."))
                if amt <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Введи положительное число (например `0.10`).", parse_mode="Markdown")
                return

            dyn["rules"].append({
                "after": dyn.pop("_cur_after"),
                "pnl": dyn.pop("_cur_pnl"),
                "amount": amt,
            })
            dyn["current"] += 1

            if dyn["current"] >= total:
                rules = sorted(dyn["rules"], key=lambda r: r["after"])
                db_mod.set_config("avg_dynamic_rules", _json.dumps(rules))
                context.user_data.pop("dyn_wizard", None)

                lines = ["✅ *Динамика докупки сохранена:*", ""]
                for i, r in enumerate(rules, 1):
                    lines.append(
                        f"  Ступень {i}: после `{r['after']}` докупок → "
                        f"PnL ≤ `{r['pnl']:.0f}%`, сумма `${r['amount']:.2f}`"
                    )
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            else:
                next_n = dyn["current"] + 1
                dyn["phase"] = "after"
                await update.message.reply_text(
                    f"*Ступень {next_n} из {total}*\nПосле скольких докупок применять это правило?",
                    parse_mode="Markdown", reply_markup=cancel_kb,
                )
            return

    # ── Transfer dialog ──────────────────────────────────────────────
    pending_transfer = context.user_data.get("pending_transfer")
    if pending_transfer:
        context.user_data.pop("pending_transfer", None)
        try:
            amount = float(msg.replace(",", "."))
        except ValueError:
            await update.message.reply_text("Не похоже на число. Перевод отменён.")
            return
        direction = pending_transfer.get("dir", "s2f")
        avail = pending_transfer.get("avail", 0.0)
        if amount <= 0:
            await update.message.reply_text("Сумма должна быть больше нуля.")
            return
        if amount > avail:
            await update.message.reply_text(f"❌ Недостаточно средств. Доступно: `${avail:.2f}`", parse_mode="Markdown")
            return
        client = context.bot_data.get("exchange")
        if not client:
            await update.message.reply_text("❌ Клиент биржи недоступен.")
            return
        label = "Спот → Фьючерсы" if direction == "s2f" else "Фьючерсы → Спот"
        try:
            await client.transfer_usdt(amount, direction)
            await update.message.reply_text(
                f"✅ *{label}*: `${amount:.2f}` USDT переведено.", parse_mode="Markdown"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка перевода: {e}")
        return

    # ── Pending input state (two-step dialog) ─────────────────────
    _TRIGGER_WORDS = ("setbet", "сетбет", "setstop", "setstops", "сетстоп", "settp", "settakes", "сеттп",
                      "стоплосс", "тейкпрофит", "ставка")
    pending = context.user_data.get("pending_set")
    if pending and pending in _PENDING_PROMPTS:
        # If the message looks like a new command — cancel state and fall through
        if any(w in lo for w in _TRIGGER_WORDS):
            context.user_data.pop("pending_set", None)
        else:
            try:
                val = float(msg.replace(",", "."))
            except ValueError:
                await update.message.reply_text(
                    "Не похоже на число. Попробуй ещё раз или напиши другую команду.",
                    parse_mode="Markdown"
                )
                context.user_data.pop("pending_set", None)
                return

            attr, _, unit, cast = _PENDING_PROMPTS[pending]
            config = context.bot_data.get("config")
            if config:
                setattr(config, attr, cast(val))
                from bot import db as db_mod
                db_mod.set_config(attr, str(val))
                label = f"${val:.2f}" if unit == "$" else f"{val:.0f}%"
                labels = {"bet": "Ставка", "tp": "Тейкпрофит", "sl": "Стоплосс"}
                await update.message.reply_text(
                    f"✅ *{labels[pending]}* применён: `{label}`", parse_mode="Markdown"
                )
                if pending in ("tp", "sl"):
                    from bot.handlers.trading import _reapply_tpsl_all
                    await _reapply_tpsl_all(context.application, config)
            context.user_data.pop("pending_set", None)
            return

    # ── TP/SL change ─────────────────────────────────────────────
    tp_pct = sl_pct = None

    m = _RE_TP_SL.search(lo)
    if m:
        tp_pct = _parse_float(m.group(1))
        sl_pct = _parse_float(m.group(2))
    else:
        m = _RE_SL_TP.search(lo)
        if m:
            sl_pct = _parse_float(m.group(1))
            tp_pct = _parse_float(m.group(2))
        else:
            m = _RE_TP_ONLY.search(lo)
            if m:
                tp_pct = _parse_float(m.group(1))
            m2 = _RE_SL_ONLY.search(lo)
            if m2:
                sl_pct = _parse_float(m2.group(1))

    if tp_pct is not None or sl_pct is not None:
        # Determine target: all or specific symbol
        target_all = bool(_RE_TARGET.search(lo)) or "всех" in lo or "все" in lo
        target_sym = None
        if not target_all:
            ms = _RE_TARGET_SYM.search(lo)
            if ms:
                target_sym = ms.group(1).upper()

        try:
            positions = await client.get_positions()
        except Exception as e:
            await update.message.reply_text(f"❌ Позиции: {e}")
            return

        if not positions:
            await update.message.reply_text("Нет открытых позиций.")
            return

        # Filter
        if target_sym:
            targets = [p for p in positions
                       if p["symbol"].split("/")[0].upper() == target_sym]
            if not targets:
                await update.message.reply_text(f"Позиция {target_sym} не найдена.")
                return
        else:
            targets = positions

        from bot.jobs.main import _calc_tp_price, _calc_sl_price
        tp_sl_pcts: dict = context.bot_data.setdefault("tp_sl_pcts", {})
        results = []
        for pos in targets:
            symbol = pos["symbol"]
            coin = symbol.split("/")[0]
            entry = float(pos.get("entry_price", 0))
            lev = int(pos.get("leverage", 1))
            side = pos.get("side", "short")

            cur = tp_sl_pcts.get(symbol, {"tp_pct": 500, "sl_pct": 500})
            new_tp_pct = tp_pct if tp_pct is not None else cur.get("tp_pct", 500)
            new_sl_pct = sl_pct if sl_pct is not None else cur.get("sl_pct", 500)

            tp_price = _calc_tp_price(entry, lev, new_tp_pct, side)
            sl_price = _calc_sl_price(entry, lev, new_sl_pct, side)

            try:
                confirmed = await client.set_tp_sl(symbol, tp_price=tp_price, sl_price=sl_price,
                                       pos_data=pos)
                prices = {r["type"]: r["price"] for r in confirmed}
                tp_price, sl_price = prices["TP"], prices["SL"]
                tp_sl_pcts[symbol] = {"tp_pct": new_tp_pct, "sl_pct": new_sl_pct}
                results.append(
                    f"✅ `{coin}`: TP `{tp_price:.6g}` (+{new_tp_pct:.0f}%) "
                    f"SL `{sl_price:.6g}` (-{new_sl_pct:.0f}%)"
                )
            except Exception as e:
                results.append(f"❌ `{coin}`: {e}")

        await update.message.reply_text("\n".join(results), parse_mode="Markdown")
        return

    # ── Open symbol (short by default) ───────────────────────────
    m = _RE_OPEN_SYM.search(lo)
    if m:
        sym_raw = m.group(1).upper()
        config = context.bot_data.get("config")
        if not config or not client:
            await update.message.reply_text("❌ Бот не готов.")
            return
        symbol = client.futures_symbol(sym_raw)
        margin = float(getattr(config, "default_trade_usdt", 1.0))
        leverage = int(getattr(config, "default_leverage", 0)) or None
        tp_pct = float(getattr(config, "tp_pct", 500))
        sl_pct = float(getattr(config, "sl_pct", 500))
        await update.message.reply_text(f"⏳ Открываю шорт `{sym_raw}`...", parse_mode="Markdown")
        try:
            from bot.handlers.trading import execute_open
            result = await execute_open(client, context.application, symbol, "sell",
                                        margin, leverage, tp_pct=tp_pct, sl_pct=sl_pct)
            coin = symbol.split("/")[0]
            await update.message.reply_text(
                f"✅ *Шорт открыт* `{coin}`\n"
                f"Entry: `{result['entry_price']:.6g}` | ×{result['leverage']}\n"
                f"Маржа: `${margin:.2f}` | TP `+{tp_pct:.0f}%` SL `-{sl_pct:.0f}%`",
                parse_mode="Markdown"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка открытия: {e}")
        return

    # ── Close all / close symbol (with confirmation) ──────────────
    if _RE_CLOSE_ALL.search(lo):
        try:
            positions = await client.get_positions()
        except Exception as e:
            await update.message.reply_text(f"❌ {e}")
            return
        if not positions:
            await update.message.reply_text("Нет открытых позиций.")
            return
        coins = ", ".join(p["symbol"].split("/")[0] for p in positions)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Закрыть все", callback_data="nlp_close_ALL"),
            InlineKeyboardButton("◀ Отмена", callback_data="nlp_close_cancel"),
        ]])
        await update.message.reply_text(
            f"⚠️ Закрыть все позиции?\n`{coins}`",
            parse_mode="Markdown", reply_markup=kb
        )
        return

    m = _RE_CLOSE_SYM.search(lo)
    if m and m.group(1).upper() not in ("ТП", "СЛ", "TP", "SL", "ВСЕ", "ALL"):
        sym_raw = m.group(1).upper()
        try:
            positions = await client.get_positions()
        except Exception as e:
            await update.message.reply_text(f"❌ {e}")
            return
        targets = [p for p in positions
                   if p["symbol"].split("/")[0].upper() == sym_raw]
        if not targets:
            await update.message.reply_text(f"Позиция {sym_raw} не найдена.")
            return
        pos = targets[0]
        pnl = float(pos.get("unrealized_pnl", 0))
        pct = float(pos.get("percentage", 0))
        sign = "+" if pnl >= 0 else ""
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Закрыть {sym_raw}", callback_data=f"nlp_close_{sym_raw}"),
            InlineKeyboardButton("◀ Отмена", callback_data="nlp_close_cancel"),
        ]])
        await update.message.reply_text(
            f"⚠️ Закрыть `{sym_raw}`?\nPnL: `{sign}{pct:.1f}%` ({sign}${pnl:.2f})",
            parse_mode="Markdown", reply_markup=kb
        )
        return

    # ── Balance / positions info ──────────────────────────────────
    if any(w in lo for w in ("баланс", "balance")):
        from bot.handlers.balance import balance_handler
        await balance_handler(update, context)
        return

    if any(w in lo for w in ("позиции", "positions", "позицию", "позиция")):
        from bot.handlers.positions import _send_positions
        await _send_positions(update.message, context)
        return

    # ── Setup: apply defaults to all symbols ─────────────────────
    if any(w in lo for w in ("настроить", "сетап", "setup", "конфиг")):
        config = context.bot_data.get("config")
        if config:
            lines = [
                "*Текущий сетап*",
                f"Маржа: `${config.default_trade_usdt:.2f}`",
                f"Плечо: `{'max' if not config.default_leverage else f'×{config.default_leverage}'}`",
                f"TP: `+{config.tp_pct:.0f}%`  SL: `-{config.sl_pct:.0f}%`",
                f"Докупка: `${config.averaging_amount:.2f}` при `{config.averaging_threshold:+.0f}%`",
                f"Макс докупок: `{config.max_averaging_count}` (≈ `${config.max_averaging_count * config.averaging_amount:.2f}`)",
                f"Перезаходов: `{config.max_reentry_cycles}`",
            ]
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    # ── Stats shortcut ────────────────────────────────────────────
    if any(w in lo for w in ("стат", "stats", "статистика")):
        from bot.handlers.stats import stats_handler
        await stats_handler(update, context)
        return

    # ── Two-step setting dialogs ───────────────────────────────────
    if any(w in lo for w in ("setbet", "сетбет", "ставка", "bet")):
        context.user_data["pending_set"] = "bet"
        _, prompt, _, _ = _PENDING_PROMPTS["bet"]
        await update.message.reply_text(prompt, parse_mode="Markdown")
        return

    if any(w in lo for w in ("setstop", "setstops", "сетстоп", "стоп", "стоплосс", "sl")):
        context.user_data["pending_set"] = "sl"
        _, prompt, _, _ = _PENDING_PROMPTS["sl"]
        await update.message.reply_text(prompt, parse_mode="Markdown")
        return

    if any(w in lo for w in ("settp", "settakes", "сеттп", "тейк", "тейкпрофит", "tp")):
        context.user_data["pending_set"] = "tp"
        _, prompt, _, _ = _PENDING_PROMPTS["tp"]
        await update.message.reply_text(prompt, parse_mode="Markdown")
        return


async def nlp_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirmation callback for NLP close commands."""
    q = update.callback_query
    await q.answer()

    if q.data == "nlp_close_cancel":
        await q.edit_message_text("◀ Отменено.")
        return

    client = context.bot_data.get("exchange")
    if not client:
        await q.edit_message_text("❌ Биржа недоступна.")
        return

    from bot import db as db_mod

    if q.data == "nlp_close_ALL":
        await q.edit_message_text("⏳ Закрываю все позиции...")
        try:
            positions = await client.get_positions()
        except Exception as e:
            await q.edit_message_text(f"❌ {e}")
            return
        results = []
        for pos in positions:
            symbol = pos["symbol"]
            coin = symbol.split("/")[0]
            try:
                from bot.handlers.trading import _do_close
                await _do_close(client, context, symbol, keep_reentry=False)
                results.append(f"{coin}: ордер отправлен, PnL ожидает исполнения")
            except Exception as e:
                results.append(f"❌ `{coin}`: {e}")
        await q.edit_message_text("Результат:\n" + "\n".join(results), parse_mode="Markdown")
        return

    # nlp_close_SYMBOL
    sym_raw = q.data.removeprefix("nlp_close_").upper()
    try:
        positions = await client.get_positions()
    except Exception as e:
        await q.edit_message_text(f"❌ {e}")
        return

    targets = [p for p in positions
               if p["symbol"].split("/")[0].upper() == sym_raw]
    if not targets:
        await q.edit_message_text(f"Позиция {sym_raw} уже закрыта или не найдена.")
        return

    symbol = targets[0]["symbol"]
    coin = symbol.split("/")[0]
    try:
        from bot.handlers.trading import _do_close
        await _do_close(client, context, symbol, keep_reentry=False)
        await q.edit_message_text(f"{coin}: ордер закрытия отправлен, PnL ожидает исполнения")
    except Exception as e:
        await q.edit_message_text(f"❌ {e}")

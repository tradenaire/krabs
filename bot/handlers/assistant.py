"""NLP text handler — free-form commands in Russian/English."""
import logging
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.handlers import wizard

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


async def _start_setting_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                prefix: str) -> None:
    wizard.start_wizard(context, prefix)
    await wizard.render_step(context.bot, update.message.chat_id, prefix, 0, context)


async def assistant_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-form text commands."""
    if not update.message or not update.message.text:
        return

    chat_type = update.message.chat.type
    if chat_type in ("group", "supergroup"):
        bot_username = (await context.bot.get_me()).username
        if f"@{bot_username}" not in update.message.text:
            return

    # Wizard приоритетнее pending_transfer/pending_mexc — если пользователь
    # сейчас в визарде (/avg, /automode, /short и т.д.), его ввод должен идти
    # туда, не в случайно повисшие state. start_wizard теперь чистит pending_X
    # при старте, но дополнительно страхуемся здесь: если wizard активен, он
    # перехватывает ввод раньше всех остальных state-машин.
    if await wizard.handle_text(update, context):
        return

    msg = update.message.text.strip()
    lo = msg.lower()

    # ── pending_transfer: ввод суммы для USDT spot↔futures перевода ─
    # Активируется кнопкой в /balance (см. handlers/balance.py transfer_*).
    # Состояние одноразовое: получили сумму → выполнили перевод → очистили.
    pending_transfer = context.user_data.get("pending_transfer")
    if pending_transfer:
        context.user_data.pop("pending_transfer", None)
        try:
            amount = float(msg.replace(",", "."))
        except ValueError:
            await update.message.reply_text("Не похоже на число. Перевод отменён.")
            return
        direction = pending_transfer.get("dir", "s2f")
        avail = float(pending_transfer.get("avail", 0.0) or 0.0)
        if amount <= 0:
            await update.message.reply_text("Сумма должна быть больше нуля.")
            return
        if amount > avail:
            await update.message.reply_text(
                f"❌ Недостаточно средств. Доступно: `${avail:.2f}`",
                parse_mode="Markdown",
            )
            return
        client = context.bot_data.get("exchange")
        if not client:
            await update.message.reply_text("❌ Клиент биржи недоступен.")
            return
        label = "Спот → Фьючерсы" if direction == "s2f" else "Фьючерсы → Спот"
        try:
            await client.transfer_usdt(amount, direction)
            await update.message.reply_text(
                f"✅ *{label}*: `${amount:.2f}` USDT переведено.",
                parse_mode="Markdown",
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка перевода: {e}")
        return

    # ── pending_mexc: двухшаговая замена MEXC ключей с проверкой ────
    # Запускается /setmexc (handlers/trading.py:setmexc_handler).
    # Шаг 1 = secret, шаг 2 = api_key. После api_key — пробуем фьюч-баланс,
    # если ОК — записываем оба ключа в DB и заменяем live ExchangeClient.
    # Удаляем сообщение пользователя сразу после получения чтобы ключ не остался в чате.
    pending_mexc = context.user_data.get("pending_mexc")
    if pending_mexc:
        if lo in ("отмена", "cancel", "стоп", "выход"):
            context.user_data.pop("pending_mexc", None)
            context.user_data.pop("_mexc_secret", None)
            await update.message.reply_text("✖ Замена ключей отменена.")
            return

        # Удаление сообщения с ключом — best-effort.
        try:
            await update.message.delete()
        except Exception:
            pass

        chat_id = update.effective_chat.id
        step = pending_mexc.get("step")

        if step == "secret":
            context.user_data["_mexc_secret"] = msg
            pending_mexc["step"] = "api_key"
            await context.bot.send_message(
                chat_id=chat_id,
                text="✅ Secret получен (твоё сообщение удалено).\n\n"
                     "Шаг 2/2 — теперь введи *API key*:",
                parse_mode="Markdown",
            )
            return

        if step == "api_key":
            api_key = msg
            secret = context.user_data.pop("_mexc_secret", None)
            context.user_data.pop("pending_mexc", None)

            if not secret:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="❌ Внутренняя ошибка: secret потерян. Запусти /setmexc заново.",
                )
                return

            await context.bot.send_message(chat_id=chat_id, text="⏳ Проверяю ключи на MEXC…")

            from bot.exchange.client import ExchangeClient
            test_client = ExchangeClient(api_key, secret)
            try:
                bal = await test_client.get_futures_balance()
            except Exception as e:
                # Не записываем — оставляем старые ключи рабочими.
                try:
                    await test_client.close()
                except Exception:
                    pass
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Ключи не работают: `{type(e).__name__}: {e}`\n\n"
                         "Запусти /setmexc заново.",
                    parse_mode="Markdown",
                )
                return

            from bot import db as db_mod
            db_mod.set_config("mexc_api_key", api_key)
            db_mod.set_config("mexc_secret", secret)
            config = context.bot_data.get("config")
            if config is not None:
                config.mexc_api_key = api_key
                config.mexc_secret = secret

            # Подменяем активный клиент на проверенный.
            context.bot_data["exchange"] = test_client

            free = float(bal.get("free", {}).get("USDT", 0) or 0)
            total = float(bal.get("total", {}).get("USDT", 0) or 0)
            await context.bot.send_message(
                chat_id=chat_id,
                text=("✅ *MEXC ключи обновлены и работают.*\n\n"
                      f"Баланс: free `{free:.2f}` / equity `{total:.2f}` USDT"),
                parse_mode="Markdown",
            )
            return

    # (wizard.handle_text был перенесён в начало функции — wizard приоритетнее
    # pending_transfer/pending_mexc, чтобы ввод '100' на шаге плеча не уходил
    # как сумма перевода или MEXC secret.)

    client = context.bot_data.get("exchange")

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
                await client.set_tp_sl(symbol, tp_price=tp_price, sl_price=sl_price,
                                       pos_data=pos)
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
        # Сначала проверяем существование контракта на MEXC. Без этого
        # `client.futures_symbol("FARTCOIN")` сконструирует SYM/USDT:USDT и
        # execute_open упадёт с криптической ошибкой биржи. Пользователь видит
        # бесполезное сообщение типа «А он ине это прислал» и не понимает что не так.
        from bot.ai.scanner import mexc_find_futures_symbol, mexc_suggest_tickers
        symbol = await mexc_find_futures_symbol(client, sym_raw)
        if not symbol:
            suggestions = await mexc_suggest_tickers(client, sym_raw, n=3)
            if suggestions:
                hint = ", ".join(f"`{s}`" for s in suggestions)
                await update.message.reply_text(
                    f"❌ Фьючерс `{sym_raw}` не найден на MEXC.\n"
                    f"Может имел в виду: {hint}?\n"
                    f"Попробуй: `Открой {suggestions[0]}`",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    f"❌ Фьючерс `{sym_raw}` не найден на MEXC и нет похожих тикеров.",
                    parse_mode="Markdown",
                )
            return
        margin = float(getattr(config, "default_trade_usdt", 1.0))
        leverage = int(getattr(config, "default_leverage", 0)) or None
        tp_pct = float(getattr(config, "tp_pct", 500))
        sl_pct = float(getattr(config, "sl_pct", 500))
        coin = symbol.split("/")[0]
        await update.message.reply_text(f"⏳ Открываю шорт `{coin}`...", parse_mode="Markdown")
        try:
            from bot.handlers.trading import execute_open
            result = await execute_open(client, context.application, symbol, "sell",
                                        margin, leverage, tp_pct=tp_pct, sl_pct=sl_pct)
            await update.message.reply_text(
                f"✅ *Шорт открыт* `{coin}`\n"
                f"Entry: `{result['entry_price']:.6g}` | ×{result['leverage']}\n"
                f"Маржа: `${margin:.2f}` | TP `+{tp_pct:.0f}%` SL `-{sl_pct:.0f}%`",
                parse_mode="Markdown"
            )
        except Exception as e:
            # Расшифровываем известные ошибки MEXC чтобы пользователь понимал
            # что делать. 'API Key expired' было в инциденте 02.05.2026.
            err_text = str(e).lower()
            if "api key" in err_text and ("expired" in err_text or "invalid" in err_text):
                await update.message.reply_text(
                    f"❌ Ключи MEXC не работают (`{e}`).\n"
                    f"Запусти `/setmexc` чтобы заменить ключи.",
                    parse_mode="Markdown",
                )
            else:
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

    # ── Setting shortcuts → wizards ───────────────────────────────
    if any(w in lo for w in ("setbet", "сетбет", "ставка", "bet")):
        await _start_setting_wizard(update, context, "setbet")
        return

    if any(w in lo for w in ("setstop", "setstops", "сетстоп", "стоп", "стоплосс", "sl")):
        await _start_setting_wizard(update, context, "setstop")
        return

    if any(w in lo for w in ("settp", "settakes", "сеттп", "тейк", "тейкпрофит", "tp")):
        await _start_setting_wizard(update, context, "settp")
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
                pnl = float(pos.get("unrealized_pnl", 0))
                margin = float(pos.get("margin", 0))
                exit_price = float(pos.get("mark_price", 0))
                await client.cancel_tp_sl_orders(symbol)
                await client.close_futures_position(symbol)
                db_mod.close_position(symbol)
                db_mod.delete_reentry(symbol)
                db_mod.log_trade(symbol, "close", amount=margin, pnl=pnl, note="manual")
                db_mod.close_position_history(symbol, exit_price, pnl, "manual")
                results.append(f"✅ `{coin}`")
            except Exception as e:
                results.append(f"❌ `{coin}`: {e}")
        await q.edit_message_text("Закрыто:\n" + "\n".join(results), parse_mode="Markdown")
        return

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
        pnl = float(targets[0].get("unrealized_pnl", 0))
        margin = float(targets[0].get("margin", 0))
        exit_price = float(targets[0].get("mark_price", 0))
        await client.cancel_tp_sl_orders(symbol)
        await client.close_futures_position(symbol)
        db_mod.close_position(symbol)
        db_mod.delete_reentry(symbol)
        db_mod.log_trade(symbol, "close", amount=margin, pnl=pnl, note="manual")
        db_mod.close_position_history(symbol, exit_price, pnl, "manual")
        sign = "+" if pnl >= 0 else ""
        await q.edit_message_text(
            f"✅ `{coin}` закрыт ({sign}${pnl:.2f})", parse_mode="Markdown"
        )
    except Exception as e:
        await q.edit_message_text(f"❌ {e}")

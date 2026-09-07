"""/balance — полный баланс с деталями по каждой позиции."""
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from bot.fmt import fmt_usd

def _value_assets(balance, prices):
    amounts = {c: float(q) for c, q in balance.get("total", {}).items() if q}
    missing = [c for c in amounts if c not in prices]
    if missing:
        return "нет цены: " + ", ".join(missing)
    return f"≈ {sum(q * prices[c] for c, q in amounts.items()):.2f} USDT"


def _build_balance_text(futures_raw: dict, positions: list[dict],
                        tp_sl_pcts: dict, db_map: dict, re_map: dict,
                        config, daily_stats: dict,
                        lev_cache: dict | None = None,
                        spot_raw: dict | None = None) -> str:
    from bot.exchange.client import available_margin

    prices = futures_raw.get("_prices", {"USDT": 1.0})
    # MEXC exposes negative multi-asset equity as zero equity plus debtAmount.
    # Negative equity, if provided, already includes that debt and is not deducted twice.
    net = dict(futures_raw.get("total", {}))
    assets = futures_raw.get("_assets", {})
    for currency, asset in assets.items():
        equity = float(asset.get("equity") or 0)
        debt = float(asset.get("debtAmount") or 0)
        net[currency] = -debt if equity == 0 and debt > 0 else equity
    combined = dict(net)
    for currency, quantity in (spot_raw or {}).get("total", {}).items():
        combined[currency] = float(combined.get(currency, 0)) + float(quantity or 0)
    lines = ["💰 Баланс", 
        "Всего: " + (_value_assets({"total": combined}, prices) if spot_raw is not None else "нет данных спота"),
        "Фьючерсы: " + _value_assets({"total": net}, prices),
        "Спот: " + (_value_assets(spot_raw, prices) if spot_raw is not None else "нет данных")]
    contributions = [a.get("contributeMarginAmount") for a in assets.values()]
    if contributions and all(v is not None for v in contributions):
        lines.append(f"Обеспечение MEXC: {sum(float(v) for v in contributions):.2f} USDT")
    multi_asset = any(c != "USDT" and float(a.get("equity") or 0) for c, a in assets.items())
    if multi_asset:
        lines.append("Свободная маржа: нет подтверждённых данных")
    else:
        try:
            lines.append(f"Доступно для новых сделок: {available_margin(futures_raw):.2f} USDT")
        except ValueError:
            lines.append("Доступная маржа: нет данных")
    lines.append("≈ с учётом долга; обеспечение ≠ свободная маржа.")
    total_pnl = sum(float(p.get("unrealized_pnl", 0)) for p in positions)
    lines.append(f"Позиции: {len(positions)} · PnL: {fmt_usd(total_pnl)}")
    for pos in positions:
        symbol = pos["symbol"]
        direction = "↓" if pos.get("side") == "short" else "↑"
        status = "бот" if symbol in db_map else "вручную"
        lines.append(f"{symbol.split('/')[0]} {direction}×{int(pos.get('leverage') or 1)} · "
                     f"{float(pos.get('unrealized_pnl') or 0):+.2f} USDT · {status}")
    if daily_stats:
        unknown = int(daily_stats.get("unknown_pnl", 0) or 0)
        lines.append(f"Сегодня (бот): {float(daily_stats.get('realized_pnl') or 0):+.2f} USDT · "
                     f"{int(daily_stats.get('closes') or 0)} сд."
                     + (f" · PnL неизвестен: {unknown}" if unknown else ""))
    return "\n".join(lines)


def _build_close_kb(positions: list[dict], config=None) -> InlineKeyboardMarkup:
    rows = []
    from bot.fmt import fmt_pct, fmt_usd
    for i, pos in enumerate(positions, 1):
        coin = pos["symbol"].split("/")[0]
        pnl = float(pos.get("unrealized_pnl", 0))
        pct = float(pos.get("percentage", 0))
        icon = "✅" if pnl >= 0 else "🔻"
        rows.append([InlineKeyboardButton(
            f"{i}. {icon} {coin}  {fmt_pct(pct)}  {fmt_usd(pnl)}",
            callback_data=f"bal_close_{pos['symbol']}"
        )])
    rows.append([
        InlineKeyboardButton("🔄 Обновить", callback_data="balance_refresh"),
        InlineKeyboardButton("💱 Перевести", callback_data="transfer_start"),
    ])
    rows.append([InlineKeyboardButton("📊 Позиции", callback_data="positions_show")])
    avg_on = getattr(config, "averaging_enabled", True) if config else True
    scan_on = getattr(config, "auto_scan_enabled", False) if config else False
    paper_on = getattr(config, "paper_enabled", True) if config else True
    rows.append([
        InlineKeyboardButton(
            f"{'✅' if avg_on else '❌'} Докупки",
            callback_data="bal_toggle_avg",
        ),
        InlineKeyboardButton(
            f"{'✅' if scan_on else '❌'} Авто-поиск",
            callback_data="bal_toggle_scan",
        ),
        InlineKeyboardButton(
            f"{'✅' if paper_on else '❌'} Бумага",
            callback_data="bal_toggle_paper",
        ),
    ])
    return InlineKeyboardMarkup(rows)


async def _fetch_all(client, context):
    from bot import db as db_mod

    futures_bal, spot_bal, positions, prices = await asyncio.gather(
        client.get_futures_balance(),
        client.get_spot_balance(),
        client.get_positions(),
        client.get_asset_prices(),
        return_exceptions=True,
    )
    if isinstance(futures_bal, Exception):
        raise futures_bal
    if isinstance(positions, Exception):
        raise positions
    if isinstance(spot_bal, Exception):
        spot_bal = None

    # Keep balance text and numbered buttons in descending return (%) order.
    positions = sorted(positions, key=lambda p: float(p.get("percentage") or 0), reverse=True)

    futures_bal["_prices"] = {"USDT": 1.0} if isinstance(prices, Exception) else prices

    db_recs = {p["symbol"]: r for p in positions if (r := db_mod.get_managed_position(p))}
    re_recs = {r["symbol"]: r for r in db_mod.get_all_reentry()}
    config = context.bot_data.get("config")
    tp_sl_pcts = context.bot_data.get("tp_sl_pcts", {})
    daily_stats = db_mod.get_daily_stats()
    lev_cache = {}

    return futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache, spot_bal


async def _fetch_with_typing(client, context, message):
    async def typing():
        from telegram.error import TelegramError
        while True:
            try:
                await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")
            except TelegramError:
                return
            await asyncio.sleep(4)

    task = asyncio.create_task(typing())
    try:
        return await _fetch_all(client, context)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def balance_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.bot_data["exchange"]
    try:
        futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache, spot_bal = \
            await _fetch_with_typing(client, context, update.message)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return

    text = _build_balance_text(futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache, spot_bal)
    kb = _build_close_kb(positions, config)
    # Split if Telegram limit exceeded (4096 chars)
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for i, chunk in enumerate(chunks):
        try:
            await update.message.reply_text(
                chunk, parse_mode="Markdown",
                reply_markup=kb if i == len(chunks) - 1 else None
            )
        except Exception:
            await update.message.reply_text(
                chunk,
                reply_markup=kb if i == len(chunks) - 1 else None
            )


async def balance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "bal_close_cancel":
        await q.answer("Отменено")
        await q.delete_message()
        return

    if q.data.startswith("bal_close_"):
        symbol = q.data[len("bal_close_"):]
        coin = symbol.split("/")[0]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 С перезаходом", callback_data=f"close_reentry_{symbol}"),
             InlineKeyboardButton("❌ Насовсем", callback_data=f"close_final_{symbol}")],
            [InlineKeyboardButton("◀ Отмена", callback_data="bal_close_cancel")],
        ])
        await q.message.reply_text(
            f"Закрыть `{coin}`?", parse_mode="Markdown", reply_markup=kb,
        )
        return

    if q.data in ("balance_refresh", "balance_futures"):
        client = context.bot_data["exchange"]
        try:
            futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache, spot_bal = \
                await _fetch_with_typing(client, context, q.message)
        except Exception as e:
            await q.answer(f"Ошибка: {e}", show_alert=True)
            return
        text = _build_balance_text(futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache, spot_bal)
        kb = _build_close_kb(positions, config)
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        try:
            await q.edit_message_text(chunks[0], parse_mode="Markdown",
                                      reply_markup=kb if len(chunks) == 1 else None)
        except Exception:
            try:
                await q.edit_message_text(chunks[0],
                                          reply_markup=kb if len(chunks) == 1 else None)
            except Exception:
                pass
        for chunk in chunks[1:]:
            try:
                await q.message.reply_text(chunk, parse_mode="Markdown",
                                           reply_markup=kb)
            except Exception:
                await q.message.reply_text(chunk, reply_markup=kb)
        return

    if q.data == "positions_show":
        from bot.handlers.positions import _send_positions
        await _send_positions(q.message, context, edit=False)

    if q.data == "transfer_start":
        client = context.bot_data["exchange"]
        try:
            futures_bal, spot_bal = await asyncio.gather(
                client.get_futures_balance(),
                client.get_spot_balance(),
                return_exceptions=True,
            )
            fut_free = float(futures_bal.get("free", {}).get("USDT", 0) or 0) if not isinstance(futures_bal, Exception) else 0.0
            spot_free = float((spot_bal.get("free") or {}).get("USDT", 0) or 0) if not isinstance(spot_bal, Exception) else 0.0
        except Exception:
            fut_free = spot_free = 0.0
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"Спот→Фьючи (${spot_free:.2f})", callback_data="transfer_dir_s2f"),
            InlineKeyboardButton(f"Фьючи→Спот (${fut_free:.2f})", callback_data="transfer_dir_f2s"),
        ], [
            InlineKeyboardButton("✖ Отмена", callback_data="balance_refresh"),
        ]])
        await q.edit_message_text("💱 *Перевод USDT*\nВыбери направление:", parse_mode="Markdown", reply_markup=kb)
        return

    if q.data in ("transfer_dir_s2f", "transfer_dir_f2s"):
        direction = q.data.replace("transfer_dir_", "")
        client = context.bot_data["exchange"]
        try:
            if direction == "s2f":
                bal = await client.get_spot_balance()
                avail = float((bal.get("free") or {}).get("USDT", 0) or 0)
                label = "Спот → Фьючерсы"
            else:
                bal = await client.get_futures_balance()
                raw = bal.get("_raw", {})
                # transferable = min(availableBalance, cashBalance) — MEXC reserves maintenance margin
                cash = float(raw.get("cashBalance", raw.get("availableBalance", 0)) or 0)
                avail_raw = float(bal.get("free", {}).get("USDT", 0) or 0)
                avail = min(avail_raw, cash)
                # floor to 2 decimals to avoid MEXC exact-boundary rejection
                import math
                avail = math.floor(avail * 100) / 100
                label = "Фьючерсы → Спот"
        except Exception:
            avail = 0.0
            label = "Спот → Фьючерсы" if direction == "s2f" else "Фьючерсы → Спот"
        context.user_data["pending_transfer"] = {"dir": direction, "avail": avail}
        await q.edit_message_text(
            f"💱 *{label}*\nДоступно: `${avail:.2f}`\n\nВведи сумму USDT:",
            parse_mode="Markdown",
        )
        return

    if q.data in ("bal_toggle_avg", "bal_toggle_scan", "bal_toggle_paper"):
        from bot import db as db_mod
        config = context.bot_data.get("config")
        if not config:
            await q.answer("Конфиг недоступен", show_alert=True)
            return
        if q.data == "bal_toggle_avg":
            new_val = not getattr(config, "averaging_enabled", True)
            config.averaging_enabled = new_val
            db_mod.set_config("averaging_enabled", "true" if new_val else "false")
            state = "включены ✅" if new_val else "отключены ❌"
            await q.answer(f"Докупки {state}", show_alert=False)
        elif q.data == "bal_toggle_scan":
            new_val = not getattr(config, "auto_scan_enabled", False)
            config.auto_scan_enabled = new_val
            db_mod.set_config("auto_scan_enabled", "true" if new_val else "false")
            state = "включён ✅" if new_val else "отключён ❌"
            await q.answer(f"Авто-поиск {state}", show_alert=False)
        else:
            new_val = not getattr(config, "paper_enabled", True)
            config.paper_enabled = new_val
            db_mod.set_config("paper_enabled", "true" if new_val else "false")
            state = "включена ✅" if new_val else "выключена ❌"
            await q.answer(f"Бумага {state}", show_alert=False)
        # Refresh balance message with updated buttons
        client = context.bot_data["exchange"]
        try:
            futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache, spot_bal = \
                await _fetch_with_typing(client, context, q.message)
        except Exception as e:
            await q.answer(f"Ошибка: {e}", show_alert=True)
            return
        text = _build_balance_text(futures_bal, positions, tp_sl_pcts, db_recs, re_recs, config, daily_stats, lev_cache, spot_bal)
        kb = _build_close_kb(positions, config)
        try:
            await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            try:
                await q.edit_message_text(text, reply_markup=kb)
            except Exception:
                pass
        return

"""/positions — список с эмодзи-кнопками, детальный вид, закрытие."""
import json
import logging
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import ContextTypes
from bot.fmt import fmt_pct, fmt_usd

logger = logging.getLogger(__name__)

_PLOCK_PATH = Path(__file__).parent.parent.parent / "data" / "profit_lock_disabled.json"


def _load_plock() -> set:
    try:
        return set(json.loads(_PLOCK_PATH.read_text()))
    except Exception:
        return set()


def _save_plock(s: set) -> None:
    try:
        _PLOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PLOCK_PATH.write_text(json.dumps(list(s)))
    except Exception as e:
        logger.warning("_save_plock: %s", e)

# Numbered emoji 1️⃣–9️⃣
_NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def _pos_emoji(pos: dict) -> str:
    """Main emoji: side + health."""
    side = pos.get("side", "")
    pct = float(pos.get("percentage", 0))
    liq = float(pos.get("liquidation_price", 0))
    mark = float(pos.get("mark_price", 0))

    # Liquidation proximity
    if liq > 0 and mark > 0:
        dist_pct = abs(mark - liq) / mark * 100
        if dist_pct < 3:
            return "💀"
        if dist_pct < 10:
            return "⚠️"

    if side == "short":
        if pct >= 200:
            return "🔥"
        if pct > 0:
            return "✅"
        return "🔻"
    else:
        if pct >= 200:
            return "🔥"
        if pct > 0:
            return "🟩"
        return "🔺"


def _format_pos_line(pos: dict, num: int) -> str:
    """One-line position summary for the list."""
    num_e = _NUM_EMOJI[num - 1] if num <= len(_NUM_EMOJI) else f"{num}."
    emoji = _pos_emoji(pos)
    coin = pos["symbol"].split("/")[0]
    lev = int(pos.get("leverage", 1))
    pct = float(pos.get("percentage", 0))
    pnl = float(pos.get("unrealized_pnl", 0))
    return f"{num_e} {emoji}{coin}×{lev} ({fmt_pct(pct)}) {fmt_usd(pnl)}"


def _format_pos_detail(pos: dict, extra: dict | None = None) -> str:
    """Full detail card for a single position."""
    extra = extra or {}
    coin = pos["symbol"].split("/")[0]
    emoji = _pos_emoji(pos)
    side = pos.get("side", "")
    side_ru = "SHORT 🔻" if side == "short" else "LONG 🟩"
    lev = int(pos.get("leverage", 1))
    pct = float(pos.get("percentage", 0))
    pnl = float(pos.get("unrealized_pnl", 0))
    entry = float(pos.get("entry_price", 0))
    mark = float(pos.get("mark_price", 0))
    liq = float(pos.get("liquidation_price", 0))
    margin = float(pos.get("margin", 0))
    margin_mode = pos.get("margin_mode", "")

    lines = [
        f"*{emoji} {coin}* — {side_ru} ×{lev}",
        f"PnL: `{fmt_pct(pct)}` ({fmt_usd(pnl)})",
        f"Entry: `{entry:.6g}` | Mark: `{mark:.6g}`",
    ]
    if liq > 0:
        dist = abs(mark - liq) / mark * 100 if mark > 0 else 0
        liq_emoji = "💀" if dist < 3 else ("⚠️" if dist < 10 else "📍")
        lines.append(f"{liq_emoji} Liq: `{liq:.6g}` ({dist:.1f}% до ликв.)")
    lines.append(f"Маржа: `${margin:.4f}` ({margin_mode})")

    # TP/SL
    tp_pct = extra.get("tp_pct")
    sl_pct = extra.get("sl_pct")
    if tp_pct or sl_pct:
        tp_s = f"+{tp_pct:.0f}%" if tp_pct else "—"
        sl_s = f"-{sl_pct:.0f}%" if sl_pct else "—"
        lines.append(f"TP: `{tp_s}` | SL: `{sl_s}`")

    # Max leverage / max position
    max_lev = extra.get("max_lev")
    max_usdt = extra.get("max_usdt")
    if max_lev:
        lev_info = f"Макс плечо: `×{max_lev}`"
        if max_usdt:
            lev_info += f" | Лимит позиции: `~${max_usdt:,.0f}`"
        lines.append(lev_info)

    # Averaging progress
    avg_count = extra.get("avg_count")
    max_avg = extra.get("max_avg")
    total_inv = extra.get("total_invested")
    budget = extra.get("budget")
    if avg_count is not None and max_avg:
        avg_s = f"Докупок: `{avg_count}/{max_avg}`"
        if total_inv is not None and budget:
            avg_s += f" | `${total_inv:.2f}/${budget:.2f}`"
        lines.append(avg_s)

    # Re-entry progress
    reentry_count = extra.get("reentry_count")
    max_reentry = extra.get("max_reentry")
    if reentry_count is not None and max_reentry:
        lines.append(f"Перезаходов: `{reentry_count}/{max_reentry}`")

    return "\n".join(lines)


async def positions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_positions(update.message, context, edit=False)


_SEP = "─" * 20


async def _send_positions(message: Message, context: ContextTypes.DEFAULT_TYPE,
                          edit: bool = False):
    from bot import db as db_mod
    from bot.pos_format import format_position_block

    client = context.bot_data["exchange"]
    try:
        positions = await client.get_positions()
    except Exception as e:
        text = f"❌ Ошибка получения позиций: {e}"
        if edit:
            await message.edit_text(text)
        else:
            await message.reply_text(text)
        return

    if not positions:
        text = "📭 Нет открытых позиций."
        if edit:
            await message.edit_text(text)
        else:
            await message.reply_text(text)
        return

    db_map = {r["symbol"]: r for r in db_mod.get_open_positions()}
    re_map = {r["symbol"]: r for r in db_mod.get_all_reentry()}
    config = context.bot_data.get("config")
    tp_sl_pcts = context.bot_data.get("tp_sl_pcts", {})

    # Fetch max_lev / pos limit / funding per symbol in parallel (best-effort)
    import asyncio as _asyncio
    lev_cache: dict = {}
    funding_cache: dict = {}

    async def _fetch_sym_data(pos):
        sym = pos["symbol"]
        lev = int(pos.get("leverage", 1))
        try:
            ml, mp = await _asyncio.gather(
                client.get_max_leverage(sym),
                client.get_position_limit_usdt(sym, lev),
            )
            lev_cache[sym] = {"max_lev": ml, "max_pos_usdt": mp}
        except Exception:
            pass
        try:
            fr = await client.get_funding_rate(sym)
            funding_cache[sym] = fr
        except Exception:
            pass

    await _asyncio.gather(*[_fetch_sym_data(p) for p in positions])

    total_pnl = sum(float(p.get("unrealized_pnl", 0)) for p in positions)
    word = "зарабатываем" if total_pnl >= 0 else "теряем"
    lines = [f"*📊 Позиции ({len(positions)}) — {word} `{fmt_usd(total_pnl)}`*"]

    for pos in positions:
        lines.append(_SEP)
        symbol = pos["symbol"]
        cached = lev_cache.get(symbol, {})
        block = format_position_block(
            pos,
            db_rec=db_map.get(symbol),
            re_rec=re_map.get(symbol),
            config=config,
            tp_sl_pcts=tp_sl_pcts,
            max_lev=cached.get("max_lev", 0),
            max_pos_usdt=cached.get("max_pos_usdt", 0),
            funding_rate=funding_cache.get(symbol, {}).get("rate", 0.0),
            funding_next_ts=funding_cache.get(symbol, {}).get("next_funding_time"),
        )
        lines.append(block)

    # Inline buttons: close + profit-lock toggle per position
    plock_disabled: set = context.bot_data.setdefault("_profit_lock_disabled", _load_plock())
    plock_step: dict = context.bot_data.get("_profit_lock_step", {})
    btn_rows = []
    for i, pos in enumerate(positions, 1):
        sym = pos["symbol"]
        coin = sym.split("/")[0]
        pnl = float(pos.get("unrealized_pnl", 0))
        pct = float(pos.get("percentage", 0))
        icon = "✅" if pnl >= 0 else "🔻"
        label = f"{i}. {icon} {coin}  {fmt_pct(pct)}  {fmt_usd(pnl)}"
        if sym in plock_disabled:
            lock_label = "🔓 лок выкл"
        elif sym in plock_step:
            lock_sl = plock_step[sym] - 50
            lock_label = f"🔒 SL+{lock_sl}%"
        else:
            lock_label = "🔒 лок"
        btn_rows.append([
            InlineKeyboardButton(label, callback_data=f"pos_close_{sym}"),
            InlineKeyboardButton(lock_label, callback_data=f"pos_plock_{sym}"),
        ])
    btn_rows.append([InlineKeyboardButton("🔄 Обновить", callback_data="positions_refresh")])

    kb = InlineKeyboardMarkup(btn_rows)
    text = "\n".join(lines)
    try:
        if edit:
            await message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
        else:
            await message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        if edit:
            await message.edit_text(text, reply_markup=kb)
        else:
            await message.reply_text(text, reply_markup=kb)


async def positions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "positions_refresh":
        await _send_positions(q.message, context, edit=True)
        return

    if data.startswith("pos_plock_"):
        symbol = data[len("pos_plock_"):]
        plock_disabled: set = context.bot_data.setdefault("_profit_lock_disabled", _load_plock())
        plock_step_map: dict = context.bot_data.setdefault("_profit_lock_step", {})
        coin = symbol.split("/")[0]
        client = context.bot_data.get("exchange")
        config = context.bot_data.get("config")

        from bot.jobs.main import _calc_tp_price, _calc_sl_price

        def _lev(pos):
            cfg_lev = int(getattr(config, "default_leverage", 0) or 0) if config else 0
            return cfg_lev or int(pos.get("leverage", 1) or 1)

        if symbol in plock_disabled:
            # ── Включить профит-лок ──────────────────────────────────
            plock_disabled.discard(symbol)
            _save_plock(plock_disabled)
            # Если уже в профите — сразу выставить нужный шаг
            try:
                pos = await client.get_position(symbol)
                pnl_pct = float(pos.get("percentage", 0)) if pos else 0.0
                plt = float(getattr(config, "averaging_profit_lock_trigger", 0)) if config else 0
                if pos and plt > 0 and pnl_pct >= 100:
                    _PL_STEP = 50
                    new_step = (int(pnl_pct) // _PL_STEP) * _PL_STEP
                    lock_sl_pct = new_step - _PL_STEP
                    entry = float(pos.get("entry_price", 0) or 0)
                    side = pos.get("side", "short")
                    lev = _lev(pos)
                    tp_stored = context.bot_data.get("tp_sl_pcts", {}).get(symbol, {})
                    tp_pct_v = tp_stored.get("tp_pct") or float(getattr(config, "tp_pct", 500))
                    new_tp = _calc_tp_price(entry, lev, tp_pct_v, side)
                    new_sl = _calc_tp_price(entry, lev, lock_sl_pct, side)
                    sl_lim = new_sl * 1.005 if side == "short" else new_sl * 0.995
                    await client.set_tp_sl(symbol, tp_price=new_tp, sl_price=new_sl,
                                           pos_data=pos, sl_limit_price=sl_lim)
                    plock_step_map[symbol] = new_step
                    await q.answer(f"🔒 Лок ВКЛ → SL выставлен +{lock_sl_pct}%", show_alert=True)
                else:
                    await q.answer(f"🔒 Лок {coin}: включён (сработает при +100%)", show_alert=False)
            except Exception as e:
                await q.answer(f"🔒 Лок вкл, SL: {e}", show_alert=True)
        else:
            # ── Выключить профит-лок → вернуть обычный SL ───────────
            plock_disabled.add(symbol)
            plock_step_map.pop(symbol, None)
            _save_plock(plock_disabled)
            try:
                pos = await client.get_position(symbol)
                if pos and config:
                    entry = float(pos.get("entry_price", 0) or 0)
                    side = pos.get("side", "short")
                    lev = _lev(pos)
                    tp_stored = context.bot_data.get("tp_sl_pcts", {}).get(symbol, {})
                    tp_pct_v = tp_stored.get("tp_pct") or float(getattr(config, "tp_pct", 500))
                    sl_pct_v = tp_stored.get("sl_pct") or float(getattr(config, "sl_pct", 500))
                    new_tp = _calc_tp_price(entry, lev, tp_pct_v, side)
                    new_sl = _calc_sl_price(entry, lev, sl_pct_v, side)
                    await client.set_tp_sl(symbol, tp_price=new_tp, sl_price=new_sl, pos_data=pos)
                    await q.answer(f"🔓 Лок ВЫКЛ → SL возвращён -{sl_pct_v:.0f}%", show_alert=True)
                else:
                    await q.answer(f"🔓 Лок {coin}: отключён", show_alert=False)
            except Exception as e:
                await q.answer(f"🔓 Лок выкл, SL: {e}", show_alert=True)

        await _send_positions(q.message, context, edit=True)
        return

    if data.startswith("pos_detail_"):
        symbol = data.replace("pos_detail_", "")
        client = context.bot_data["exchange"]
        try:
            pos = await client.get_position(symbol)
        except Exception as e:
            await q.answer(f"Ошибка: {e}", show_alert=True)
            return
        if not pos:
            await q.answer("Позиция не найдена.", show_alert=True)
            return

        # Collect extra info
        from bot import db as db_mod
        lev = int(pos.get("leverage", 1))
        extra: dict = {}
        tp_sl_pcts: dict = context.bot_data.get("tp_sl_pcts", {})
        stored = tp_sl_pcts.get(symbol, {})
        db_rec = db_mod.get_open_position(symbol)
        extra["tp_pct"] = stored.get("tp_pct") or (db_rec.get("tp_pct") if db_rec else None)
        extra["sl_pct"] = stored.get("sl_pct") or (db_rec.get("sl_pct") if db_rec else None)
        try:
            extra["max_lev"] = await client.get_max_leverage(symbol)
            extra["max_usdt"] = await client.get_position_limit_usdt(symbol, lev)
        except Exception:
            pass
        if db_rec:
            extra["avg_count"] = db_rec.get("averaging_count", 0)
            extra["total_invested"] = db_rec.get("total_invested", 0)
            extra["budget"] = db_rec.get("averaging_budget", 0)
        config = context.bot_data.get("config")
        if config:
            extra["max_avg"] = config.max_averaging_count
        re_rec = db_mod.get_reentry(symbol)
        if re_rec:
            extra["reentry_count"] = re_rec.get("cycle_count", 0)
            extra["max_reentry"] = re_rec.get("max_cycles", 3)

        detail = _format_pos_detail(pos, extra)
        coin = symbol.split("/")[0]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Закрыть", callback_data=f"pos_close_{symbol}")],
            [InlineKeyboardButton("◀ Назад", callback_data="positions_refresh")],
        ])
        try:
            await q.edit_message_text(detail, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            await q.edit_message_text(detail, reply_markup=kb)
        return

    if data.startswith("pos_close_"):
        symbol = data.replace("pos_close_", "")
        coin = symbol.split("/")[0]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 С перезаходом", callback_data=f"close_reentry_{symbol}"),
             InlineKeyboardButton("❌ Насовсем", callback_data=f"close_final_{symbol}")],
            [InlineKeyboardButton("◀ Отмена", callback_data="positions_refresh")],
        ])
        try:
            await q.edit_message_text(
                f"Закрыть `{coin}`?", parse_mode="Markdown", reply_markup=kb
            )
        except Exception:
            pass
        return

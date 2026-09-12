"""/short, /close, /avg — торговые команды."""
import asyncio
import json
import logging
import math
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
    return entry - move if side in ("short", "sell") else entry + move


def _calc_sl_price(entry: float, leverage: int, sl_pct: float, side: str) -> float:
    """entry + leverage move that gives -sl_pct PnL on margin."""
    move = entry * sl_pct / 100 / leverage
    return entry + move if side in ("short", "sell") else entry - move


def _max_leverage_by_vol(vol_24h_usdt: float) -> int:
    """Cap leverage based on 24h quote volume as liquidity/volatility proxy."""
    if vol_24h_usdt >= 500_000_000:
        return 20
    if vol_24h_usdt >= 50_000_000:
        return 10
    return 5


class MinOrderUpgradeNeeded(Exception):
    def __init__(self, min_margin: float, leverage: int):
        self.min_margin = min_margin
        self.leverage = leverage
        super().__init__(f"min_margin={min_margin:.4f} lev={leverage}")


async def execute_open(client, app, symbol: str, side: str,
                       margin: float, leverage: int | None = None,
                       tp_pct: float = 500, sl_pct: float = 500,
                       interactive: bool = False, cycle_count: int = 0) -> dict:
    """Open a futures position with TP/SL and register re-entry.

    interactive=True: raises MinOrderUpgradeNeeded instead of silently upgrading margin.
    """
    config = app.bot_data.get("config")

    user_set = leverage is not None and leverage > 0

    # Resolve max leverage if not given
    if not user_set:
        try:
            leverage = await client.get_max_leverage(symbol)
        except Exception:
            leverage = 25

    # Cap leverage by 24h volume only when leverage was NOT explicitly set by user
    if not user_set:
        try:
            ticker = await client.get_ticker(symbol)
            vol_24h = float(ticker.get("quoteVolume") or ticker.get("baseVolume") or 0)
            vol_cap = _max_leverage_by_vol(vol_24h)
            if leverage > vol_cap:
                logger.info("Leverage capped %s: %d→%d (vol_24h=$%.0f)", symbol, leverage, vol_cap, vol_24h)
                leverage = vol_cap
        except Exception:
            pass

    # BTC trend warning for manual shorts (non-blocking)
    if side in ("sell", "short"):
        try:
            from bot.jobs.main import _get_btc_rsi_4h
            btc_rsi = await _get_btc_rsi_4h(client)
            btc_threshold = float(getattr(config, "btc_rsi_filter", 65.0)) if config else 65.0
            if btc_rsi is not None and btc_rsi > btc_threshold:
                from bot.jobs.main import _notify_all
                await _notify_all(app,
                    f"⚠️ BTC RSI 4h = `{btc_rsi:.0f}` > `{btc_threshold:.0f}` — бычий рынок\n"
                    f"Шорт открывается, но осторожно")
        except Exception:
            pass

    # Enforce minimum order notional AFTER leverage caps (MEXC error 7008).
    # MEXC enforces $5 minimum notional; many symbols lack limits.cost.min in market data,
    # so get_min_order_usdt falls back to 1-contract (too small). Floor at $5.
    _MEXC_MIN_NOTIONAL = 5.0
    _min_cache: dict = app.bot_data.setdefault("_min_order_cache", {})
    _cached_notional = _min_cache.get(symbol, 0)
    if _cached_notional > 0:
        effective_notional = max(_cached_notional, _MEXC_MIN_NOTIONAL)
        _min_margin = effective_notional / max(leverage, 1) * 1.05
    else:
        try:
            _min_margin_api = await client.get_min_order_usdt(symbol, leverage)
            raw_notional = _min_margin_api * leverage if _min_margin_api > 0 else 0.0
        except Exception:
            raw_notional = 0.0
        effective_notional = max(raw_notional, _MEXC_MIN_NOTIONAL)
        _min_cache[symbol] = effective_notional
        _min_margin = effective_notional / max(leverage, 1) * 1.05

    if _min_margin > 0 and margin < _min_margin - 0.0001:
        if interactive:
            raise MinOrderUpgradeNeeded(_min_margin, leverage)
        logger.info("execute_open %s: margin upgraded $%.4f→$%.4f (×%d)", symbol, margin, _min_margin, leverage)
        margin = _min_margin

    order = await client.place_futures_order(symbol, side, margin, leverage)
    actual_lev = order.get("leverage", leverage) or leverage

    from bot import db as db_mod
    from bot.lifecycle import register_position, reset_runtime
    pos = None
    for _ in range(5):
        await asyncio.sleep(0.4)
        pos = await client.get_position(symbol)
        if pos and str(pos["position_id"]) == str(order["position_id"]):
            break
    if not pos or str(pos["position_id"]) != str(order["position_id"]):
        raise RuntimeError(f"Order {order['id']} accepted; matching position not confirmed. Do not repeat opening.")
    entry, actual_lev = pos["entry_price"], pos["leverage"]
    liq = pos.get("liquidation_price", 0)
    fsym = pos["symbol"]
    record = register_position(pos, config, tp_pct=tp_pct, sl_pct=sl_pct)
    db_mod.set_config(f"order_uncertain_{fsym}", "")
    reset_runtime(app, fsym)
    db_mod.log_trade(fsym, "open", amount=pos["margin"], note=f"lev={actual_lev}")
    max_cycles = int(config.max_reentry_cycles)
    if max_cycles > 0:
        db_mod.upsert_reentry(fsym, pos["side"], margin, actual_lev, tp_pct, sl_pct,
                             max_cycles=max_cycles, cycle_count=cycle_count, position_key=record["id"])
    else:
        db_mod.delete_reentry(fsym)
    tp_price = sl_price = 0.0
    protection_status = "не подтверждена"
    try:
        protections = await client.set_tp_sl(symbol,
            tp_price=_calc_tp_price(entry, actual_lev, tp_pct, pos["side"]),
            sl_price=_calc_sl_price(entry, actual_lev, sl_pct, pos["side"]), pos_data=pos)
        prices = {r["type"]: r["price"] for r in protections}
        tp_price, sl_price = prices["TP"], prices["SL"]
        protection_status = "TP и SL подтверждены"
    except Exception as error:
        from bot.jobs.main import _notify_all
        await _notify_all(app, f"⚠️ {fsym}: позиция открыта, защита НЕ подтверждена: {error}")
        logger.error("Opened position %s without confirmed protection: %s", fsym, error)

    # Store tp_sl_pcts for averaging recalc
    tp_sl_pcts = app.bot_data.setdefault("tp_sl_pcts", {})
    tp_sl_pcts[fsym] = {"tp_pct": tp_pct, "sl_pct": sl_pct}

    return {
        "symbol": symbol,
        "side": side,
        "margin": margin,
        "leverage": actual_lev,
        "entry_price": entry,
        "protection_status": protection_status,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "liquidation_price": liq,
        "order_id": order.get("id"),
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
    }


async def short_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/short SYMBOL [amount] [xLEV] — открыть шорт. Плечо: x100 или х100."""
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /short SYMBOL [amount_usdt] [xLEV]")
        return

    symbol_raw = args[0].upper()
    config = context.bot_data["config"]
    client = context.bot_data["exchange"]
    default_margin = float(getattr(config, "default_trade_usdt", 0.20))

    # Parse remaining args: xNN = leverage, float = margin
    inline_lev: int | None = None
    margin = default_margin
    import re as _re
    for a in args[1:]:
        m = _re.match(r'^[xхXХ](\d+)$', a, _re.IGNORECASE)
        if m:
            inline_lev = int(m.group(1))
        else:
            try:
                margin = float(a)
            except ValueError:
                pass

    cfg_lev = int(getattr(config, "default_leverage", 0) or 0)
    leverage = inline_lev or cfg_lev or None

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

    lev_str = f"×{leverage}" if leverage else "×макс"
    await update.message.reply_text(f"🔻 Открываю SHORT `{coin}` ${margin:g} {lev_str}...",
                                     parse_mode="Markdown")
    try:
        result = await execute_open(
            client, context.application, sym, "sell", margin,
            leverage=leverage, tp_pct=tp_pct, sl_pct=sl_pct,
        )
        actual_margin = result.get("margin", margin)
        lines = [
            f"*{coin}* 🔻×{result['leverage']} `${actual_margin:.2f}`",
            f"▶ Entry: `{result['entry_price']:.6g}`",
        ]
        if actual_margin > margin + 0.001:
            lines.append(f"⚠️ Маржа поднята до мин MEXC: `${margin:.2f}` → `${actual_margin:.2f}`")
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
    """/close SYMBOL — показывает выбор: с перезаходом или насовсем."""
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
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 С перезаходом", callback_data=f"close_reentry_{sym}"),
         InlineKeyboardButton("❌ Насовсем", callback_data=f"close_final_{sym}")],
        [InlineKeyboardButton("◀ Отмена", callback_data=f"close_cancel_{sym}")],
    ])
    await update.message.reply_text(
        f"Закрыть `{coin}`?", parse_mode="Markdown", reply_markup=kb
    )


async def _do_close(client, context, symbol: str, keep_reentry: bool):
    from bot import db as db_mod
    pos = await client.get_position(symbol)
    record = db_mod.get_managed_position(pos) if pos else None
    if not record:
        raise ValueError("Position is unmanaged or changed; /adopt SYMBOL first")
    if not keep_reentry:
        db_mod.delete_reentry(symbol)
    elif not db_mod.get_reentry(symbol):
        config = context.bot_data["config"]
        db_mod.upsert_reentry(symbol, pos["side"], pos["margin"], pos["leverage"],
            record["tp_pct"], record["sl_pct"], max_cycles=config.max_reentry_cycles,
            position_key=record["id"])
    await client.close_futures_position(symbol, expected_position_id=pos["position_id"],
        reason="manual_reentry" if keep_reentry else "manual")
    # Submission is not execution. Reconciliation writes the result when history confirms it.
    return None, None


async def close_reentry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """close_reentry_{symbol} — закрыть с сохранением перезахода."""
    q = update.callback_query
    await q.answer()
    symbol = q.data[len("close_reentry_"):]
    client = context.bot_data["exchange"]
    coin = symbol.split("/")[0]
    try:
        pnl, cycles_left = await _do_close(client, context, symbol, keep_reentry=True)
        pnl_s = "PnL ожидает исполнения"
        await q.edit_message_text(
            f"Ордер закрытия *{coin}* отправлен{(' ' + pnl_s) if pnl_s else ''}\n"
            "Перезаход — после подтверждения исполнения и проверки циклов",
            parse_mode="Markdown",
        )
    except Exception as e:
        await q.edit_message_text(f"❌ Ошибка: {e}")


async def close_final_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """close_final_{symbol} — закрыть насовсем без перезахода."""
    q = update.callback_query
    await q.answer()
    symbol = q.data[len("close_final_"):]
    client = context.bot_data["exchange"]
    coin = symbol.split("/")[0]
    try:
        pnl, _ = await _do_close(client, context, symbol, keep_reentry=False)
        pnl_s = " PnL ожидает исполнения"
        await q.edit_message_text(f"Ордер закрытия *{coin}* отправлен{pnl_s}.", parse_mode="Markdown")
    except Exception as e:
        await q.edit_message_text(f"❌ Ошибка: {e}")


async def close_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """close_cancel_{symbol} — отмена закрытия."""
    q = update.callback_query
    await q.answer()
    try:
        await q.delete_message()
    except Exception:
        await q.edit_message_text("Отменено.")


# ── Avg wizard ────────────────────────────────────────────────────

AVG_WIZARD_STEPS = [
    (1,  "bet",       "default_trade_usdt",            float, "Маржа",           "$"),
    (2,  "leverage",  "default_leverage",              int,   "Плечо",           "#"),
    (3,  "tp",        "tp_pct",                        float, "TP",              "%"),
    (4,  "sl",        "sl_pct",                        float, "SL",              "%"),
    (5,  "threshold", "averaging_threshold",           float, "При PnL",         "%"),
    (6,  "amount",    "averaging_amount",              float, "Сумма",           "$"),
    (7,  "maxavg",    "max_averaging_count",           int,   "Макс",            "#"),
    (8,  "interval",  "averaging_interval",            int,   "Интервал",        "s"),
    (10, "scan_cap",        "auto_scan_capital_pct",            float, "Авто-капитал",    "%"),
    (15, "margin_emergency", "margin_emergency_threshold_pct", float, "Порог доступной маржи", "%"),
    (16, "margin_trim", "margin_emergency_trim_pct", float, "Размер сокращения", "%"),
]
# Legacy steps not in numbered list (kept for backward compat):
# ("reentry",      "max_reentry_cycles",              int,   "Перезаходов макс",  "#"),
# ("lock_trigger", "averaging_profit_lock_trigger",   float, "Локк SL при профите ≥", "%"),
# ("lock_sl",      "averaging_profit_lock_sl_pct",    float, "Поставить SL на PnL",  "%"),

_AVG_BY_NUM = {num: (key, attr, cast, label, unit) for num, key, attr, cast, label, unit in AVG_WIZARD_STEPS}
_AVG_BY_KEY = {key: (num, attr, cast, label, unit) for num, key, attr, cast, label, unit in AVG_WIZARD_STEPS}


def _load_avg_dynamic_rules() -> list[dict]:
    from bot import db as db_mod
    raw = db_mod.get_config("avg_dynamic_rules", "")
    if not raw:
        return []
    try:
        rules = json.loads(raw)
    except Exception:
        return []
    if not isinstance(rules, list):
        return []
    out: list[dict] = []
    for r in rules[:4]:
        try:
            out.append({
                "after": int(r["after"]),
                "pnl": float(r["pnl"]),
                "amount": float(r["amount"]),
            })
        except Exception:
            continue
    return sorted(out, key=lambda r: r["after"])


def _save_avg_dynamic_rules(rules: list[dict]) -> None:
    from bot import db as db_mod
    clean = []
    for r in rules[:4]:
        try:
            amount = float(r["amount"])
            if amount <= 0:
                continue
            clean.append({
                "after": int(r["after"]),
                "pnl": float(r["pnl"]),
                "amount": amount,
            })
        except Exception:
            continue
    clean.sort(key=lambda r: r["after"])
    db_mod.set_config("avg_dynamic_rules", json.dumps(clean) if clean else "")


def _fmt_dyn_rule(rule: dict | None) -> str:
    if not rule:
        return "выкл"
    return f"после `{int(rule['after'])}` докупок → PnL ≤ `{float(rule['pnl']):.0f}%`, сумма `${float(rule['amount']):.2f}`"


def avg_pending_for_number(num: int) -> dict | None:
    if num in _AVG_BY_NUM:
        key, attr, cast, label, unit = _AVG_BY_NUM[num]
        return {"num": num, "kind": "simple", "key": key, "attr": attr,
                "cast": cast.__name__, "label": label, "unit": unit}
    if num == 9:
        return {"num": 9, "kind": "lock", "key": "profit_lock", "label": "Профит-локк"}
    if 11 <= num <= 14:
        return {"num": num, "kind": "dyn", "key": f"dyn_{num - 10}",
                "label": f"Ступень {num - 10}", "index": num - 11}
    return None


def avg_pending_for_key(key: str) -> dict | None:
    if key in _AVG_BY_KEY:
        num, attr, cast, label, unit = _AVG_BY_KEY[key]
        return {"num": num, "kind": "simple", "key": key, "attr": attr,
                "cast": cast.__name__, "label": label, "unit": unit}
    if key == "profit_lock":
        return avg_pending_for_number(9)
    if key.startswith("dyn_"):
        try:
            dyn_num = int(key.split("_", 1)[1])
        except ValueError:
            return None
        if 1 <= dyn_num <= 4:
            return avg_pending_for_number(10 + dyn_num)
    return None


def avg_question_text(config, pending: dict) -> str:
    num = int(pending["num"])
    label = pending["label"]
    kind = pending.get("kind")
    if kind == "simple":
        cur = _avg_fmt(config, pending["attr"], pending["unit"])
        return f"*{num}. {label}*\nСейчас: `{cur}`\n\nВведи новое значение:"
    if kind == "lock":
        trigger = float(getattr(config, "averaging_profit_lock_trigger", 0))
        sl_pct = float(getattr(config, "averaging_profit_lock_sl_pct", 0))
        cur = f"при +{trigger:.0f}% → SL +{sl_pct:.0f}%" if trigger > 0 else "выкл"
        return (
            f"*9. Профит-локк*\nСейчас: `{cur}`\n\n"
            "Введи два числа: `trigger sl`, например `150 100`.\n"
            "Или `0`, чтобы выключить."
        )
    rules = _load_avg_dynamic_rules()
    idx = int(pending["index"])
    cur = _fmt_dyn_rule(rules[idx] if idx < len(rules) else None)
    return (
        f"*{num}. {label}*\nСейчас: {cur}\n\n"
        "Введи три значения: `после PnL сумма`, например `50 -150 0.10`.\n"
        "Или `0`, чтобы выключить эту ступень."
    )


def _parse_numbers(text: str) -> list[float]:
    parts = text.replace(",", ".").replace(";", " ").split()
    return [float(p) for p in parts]


def _validate_margin_setting(attr, value):
    if attr in ("margin_emergency_threshold_pct", "margin_emergency_trim_pct"):
        minimum = 0 if attr == "margin_emergency_threshold_pct" else 0.000001
        if not math.isfinite(value) or not minimum <= value <= 100:
            raise ValueError("Порог: 0–100%; размер сокращения: больше 0 и не более 100%")


async def apply_avg_pending_value(context: ContextTypes.DEFAULT_TYPE,
                                  pending: dict, text: str) -> str:
    config = context.bot_data["config"]
    kind = pending.get("kind")
    from bot import db as db_mod

    if kind == "simple":
        cast = {"float": float, "int": int}.get(pending["cast"], float)
        raw = text.replace(",", ".").strip()
        val = cast(float(raw)) if cast is int else cast(raw)
        _validate_margin_setting(pending["attr"], val)
        setattr(config, pending["attr"], val)
        db_mod.set_config(pending["attr"], str(val))

        key = pending["key"]
        protection_results = None
        if key in ("tp", "sl"):
            protection_results = await _reapply_tpsl_all(context.application, config)
        if key == "interval":
            from bot.jobs.main import reschedule_averaging
            reschedule_averaging(context.application, int(val))
        if key in ("amount", "maxavg"):
            new_budget = config.max_averaging_count * config.averaging_amount
            with db_mod._connect() as conn:
                conn.execute("UPDATE positions SET averaging_budget=? WHERE status='open'", (new_budget,))
        result = f"{pending['num']}. {pending['label']} → `{_avg_fmt(config, pending['attr'], pending['unit'])}`"
        if protection_results is not None:
            result += "\n\n" + format_tpsl_results(protection_results)
        return result

    lo = text.strip().lower()
    if kind == "lock":
        if lo in ("0", "off", "выкл", "выключить", "нет"):
            trigger = 0.0
            sl_pct = 0.0
        else:
            nums = _parse_numbers(text)
            if len(nums) < 2:
                raise ValueError("нужно два числа: trigger sl, например `150 100`, или `0`")
            trigger, sl_pct = nums[0], nums[1]
            if trigger < 0 or sl_pct < 0:
                raise ValueError("значения профит-локка должны быть ≥ 0")
        config.averaging_profit_lock_trigger = float(trigger)
        config.averaging_profit_lock_sl_pct = float(sl_pct)
        db_mod.set_config("averaging_profit_lock_trigger", str(trigger))
        db_mod.set_config("averaging_profit_lock_sl_pct", str(sl_pct))
        return "9. Профит-локк → `выкл`" if trigger <= 0 else (
            f"9. Профит-локк → `+{trigger:.0f}% → SL +{sl_pct:.0f}%`"
        )

    if kind == "dyn":
        rules = _load_avg_dynamic_rules()
        idx = int(pending["index"])
        if lo in ("0", "off", "выкл", "выключить", "нет"):
            if idx < len(rules):
                rules.pop(idx)
            _save_avg_dynamic_rules(rules)
            return f"{pending['num']}. {pending['label']} → `выкл`"

        nums = _parse_numbers(text)
        if len(nums) < 3:
            raise ValueError("нужно три значения: `после PnL сумма`, например `50 -150 0.10`, или `0`")
        after, pnl, amount = int(nums[0]), float(nums[1]), float(nums[2])
        if after < 0:
            raise ValueError("количество докупок должно быть ≥ 0")
        if pnl > 0:
            pnl = -pnl
        if amount <= 0:
            raise ValueError("сумма докупки должна быть > 0")
        while len(rules) <= idx:
            rules.append({"after": 0, "pnl": -100.0, "amount": float(getattr(config, "averaging_amount", 0.1))})
        rules[idx] = {"after": after, "pnl": pnl, "amount": amount}
        _save_avg_dynamic_rules(rules)
        return f"{pending['num']}. {pending['label']} → `{_fmt_dyn_rule(rules[idx])}`"

    raise ValueError("неизвестный пункт настройки")


def _avg_fmt(config, attr: str, unit: str) -> str:
    val = getattr(config, attr, 0)
    if unit == "$":
        return f"${float(val):.2f}"
    if unit == "#":
        return str(int(val))
    if unit == "s":
        return f"{int(val)}s"
    return f"{float(val):.0f}%"


def _build_avg_select_kb(config) -> InlineKeyboardMarkup:
    """Keyboard with one button per setting showing current value."""
    rows = []
    pair = []
    for num in range(1, 16):
        pending = avg_pending_for_number(num)
        if not pending:
            continue
        label = f"{num}. {pending['label']}"
        pair.append(InlineKeyboardButton(label, callback_data=f"avg_pick_{pending['key']}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton("✖ Готово", callback_data="avg_done")])
    return InlineKeyboardMarkup(rows)


async def send_avg_wizard_step(bot, chat_id: int, step_idx: int, config) -> None:
    _, key, attr, _, label, unit = AVG_WIZARD_STEPS[step_idx]
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


def _build_avg_text(config, free_balance: float | None = None, open_count: int = 0) -> str:
    lock_trig = float(getattr(config, "averaging_profit_lock_trigger", 0))
    lock_sl = float(getattr(config, "averaging_profit_lock_sl_pct", 0))
    lock_line = (f"9. Профит-локк: при `+{lock_trig:.0f}%` → SL в `+{lock_sl:.0f}%`"
                 if lock_trig > 0 else "9. Профит-локк: выкл")
    scan_risk = float(getattr(config, "auto_scan_capital_pct", 0))
    avg_count = int(getattr(config, "max_averaging_count", 100))
    avg_amt = float(getattr(config, "averaging_amount", 0.5))
    bet = float(getattr(config, "default_trade_usdt", 0.2))
    sl_f = float(getattr(config, "sl_pct", 500))
    lock_trig_f = float(getattr(config, "averaging_profit_lock_trigger", 0))
    base_bud = avg_count * avg_amt + bet
    full_budget = base_bud if lock_trig_f > 0 else base_bud * (sl_f / 100.0)
    min_dep = full_budget * (1 - scan_risk / 100)
    lock_note = " (lock)" if lock_trig_f > 0 else f" (×SL{sl_f:.0f}%)"
    if free_balance is not None:
        total_avail = free_balance + open_count * bet
        total_needed = full_budget * (open_count + 1) * (1.0 - scan_risk / 100.0)
        ok = "✅" if total_avail >= total_needed else "❌"
        pos_label = f"{open_count + 1} поз" if open_count else "1 поз"
        scan_risk_line = (
            f"10. Авто-капитал: `{scan_risk:.0f}%` → `${total_needed:.2f}` ({pos_label}×`${full_budget:.2f}`){lock_note}"
            f" | фьюч `${free_balance:.2f}` {ok}"
        )
    else:
        scan_risk_line = (
            f"10. Авто-капитал: `{scan_risk:.0f}%` → нужно `${min_dep:.2f}`/поз{lock_note}"
        )
    lines = [
        "*Торговые настройки*",
        "",
        "*Вход*",
        f"1. Маржа: `${config.default_trade_usdt:.2f}`",
        f"2. Плечо: `{'макс' if not config.default_leverage else f'×{config.default_leverage}'}`",
        f"3. TP: `{config.tp_pct:.0f}%`",
        f"4. SL: `{config.sl_pct:.0f}%`",
        "",
        "*Докупка*",
        f"5. При PnL: `{config.averaging_threshold:.0f}%`",
        f"6. Сумма: `${config.averaging_amount:.2f}`",
        f"7. Макс: `{config.max_averaging_count}` докупок",
        f"8. Интервал: `{config.averaging_interval}s`",
        lock_line,
        scan_risk_line,
        f"15. Порог доступной маржи: `{float(getattr(config, 'margin_emergency_threshold_pct', 0)):.0f}%`"
        + (" _(выкл)_" if not float(getattr(config, 'margin_emergency_threshold_pct', 0)) else
           " _(доступно < X% свободного или доступно ≤ 0)_"),
        f"16. Размер сокращения: `{config.margin_emergency_trim_pct:g}%` контрактов",
        "",
        "_Напиши номер `1`-`16`, чтобы изменить конкретный пункт._",
    ]
    dyn = _load_avg_dynamic_rules()
    lines.append("")
    lines.append("*Динамика докупки:*")
    for i in range(4):
        lines.append(f"{11 + i}. Ступень {i + 1}: {_fmt_dyn_rule(dyn[i] if i < len(dyn) else None)}")
    return "\n".join(lines)


async def avg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/avg [param] [value] — все торговые настройки."""
    config = context.bot_data["config"]
    args = context.args or []

    if not args:
        client = context.bot_data.get("exchange")
        free_balance: float | None = None
        open_count = 0
        if client:
            try:
                free_balance = await client.get_free_futures_balance()
            except Exception:
                pass
            pos_cache = context.bot_data.get("_pos_cache")
            if pos_cache is not None:
                open_count = len(pos_cache)
            else:
                try:
                    open_count = len(await client.get_positions())
                except Exception:
                    pass
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Изменить", callback_data="avg_edit")],
            [InlineKeyboardButton("⚙️ Динамика докупки", callback_data="dyn_setup")],
        ])
        context.user_data["avg_select_mode"] = True
        await update.message.reply_text(_build_avg_text(config, free_balance, open_count),
                                        parse_mode="Markdown", reply_markup=kb)
        return

    if args[0].isdigit():
        pending = avg_pending_for_number(int(args[0]))
        if not pending:
            await update.message.reply_text("Номер должен быть от 1 до 14.")
            return
        if len(args) < 2:
            context.user_data["avg_pending"] = pending
            context.user_data["avg_select_mode"] = True
            await update.message.reply_text(
                avg_question_text(config, pending),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Отмена", callback_data="avg_back")]]),
            )
            return
        # Value provided inline: /avg 1 0.50
        try:
            result = await apply_avg_pending_value(context, pending, " ".join(args[1:]))
        except ValueError as e:
            await update.message.reply_text(f"❌ {e}", parse_mode="Markdown")
            return
        await update.message.reply_text(f"✅ {result}", parse_mode="Markdown",
                                         reply_markup=_build_avg_select_kb(config))
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
        "bet":          ("default_trade_usdt", float),
        "leverage":     ("default_leverage", int),
        "tp":           ("tp_pct", float),
        "sl":           ("sl_pct", float),
        "threshold":    ("averaging_threshold", float),
        "amount":       ("averaging_amount", float),
        "interval":     ("averaging_interval", int),
        "maxavg":       ("max_averaging_count", int),
        "lock_trigger": ("averaging_profit_lock_trigger", float),
        "lock_sl":      ("averaging_profit_lock_sl_pct", float),
        "scan_cap":     ("auto_scan_capital_pct", float),
        "margin_emergency": ("margin_emergency_threshold_pct", float),
        "margin_trim": ("margin_emergency_trim_pct", float),
    }
    if param not in field_map:
        await update.message.reply_text(
            f"Неизвестный параметр: `{param}`\n"
            "Доступны: `bet`, `leverage`, `tp`, `sl`, `threshold`, `amount`, `interval`, `maxavg`, `lock_trigger`, `lock_sl`, `scan_cap`",
            parse_mode="Markdown"
        )
        return

    attr, cast = field_map[param]
    _validate_margin_setting(attr, cast(val))
    setattr(config, attr, cast(val))
    from bot import db as db_mod
    db_mod.set_config(attr, str(val))

    label = {"bet": f"${val:.2f}", "tp": f"{val:.0f}%", "sl": f"{val:.0f}%"}.get(param, str(val))
    await update.message.reply_text(f"✅ Настройка сохранена: `{param}` = `{label}`", parse_mode="Markdown")
    if param in ("tp", "sl"):
        results = await _reapply_tpsl_all(context.application, config)
        await update.message.reply_text(format_tpsl_results(results), parse_mode="Markdown")

    if param == "interval":
        from bot.jobs.main import reschedule_averaging
        reschedule_averaging(context.application, int(val))

    if param in ("amount", "maxavg"):
        new_budget = config.max_averaging_count * config.averaging_amount
        with db_mod._connect() as conn:
            conn.execute("UPDATE positions SET averaging_budget=? WHERE status='open'", (new_budget,))


async def _reapply_tpsl_all(app, config) -> list[dict]:
    """Re-apply TP/SL and return the confirmed/failed result per position."""
    from bot import db as db_mod
    from bot.jobs.main import _calc_tp_price, _calc_sl_price
    client = app.bot_data.get("exchange")
    if not client:
        return [{"symbol": "*", "ok": False, "error": "биржевой клиент недоступен"}]
    tp_pct = float(getattr(config, "tp_pct", 500))
    sl_pct = float(getattr(config, "sl_pct", 500))
    tp_sl_pcts: dict = app.bot_data.setdefault("tp_sl_pcts", {})
    try:
        positions = await client.get_positions()
    except Exception as e:
        return [{"symbol": "*", "ok": False, "error": f"позиции не получены: {e}"}]
    results = []
    for pos in positions:
        if not db_mod.get_managed_position(pos):
            continue
        symbol = pos["symbol"]
        entry = float(pos.get("entry_price", 0) or 0)
        lev = int(pos.get("leverage", 1) or 1)
        side = pos.get("side", "short")
        if not entry:
            results.append({"symbol": symbol, "ok": False, "error": "entry недоступен"})
            continue
        tp_price = _calc_tp_price(entry, lev, tp_pct, side)
        sl_price = _calc_sl_price(entry, lev, sl_pct, side)
        try:
            confirmed = await client.set_tp_sl(symbol, tp_price=tp_price, sl_price=sl_price, pos_data=pos)
            legs = {
                leg.get("type"): leg for leg in (confirmed or [])
                if isinstance(leg, dict)
                and leg.get("type") in ("TP", "SL")
                and leg.get("confirmed") is True
                and leg.get("id")
            }
            if set(legs) != {"TP", "SL"}:
                raise RuntimeError("биржа не подтвердила обе ноги TP/SL с order ID")
            tp_price = float(legs["TP"]["price"])
            sl_price = float(legs["SL"]["price"])
            tp_sl_pcts[symbol] = {"tp_pct": tp_pct, "sl_pct": sl_pct}
            db_mod.update_position_tpsl(symbol, tp_pct, sl_pct)
            with db_mod._connect() as conn:
                conn.execute(
                    "UPDATE reentry SET tp_pct=?, sl_pct=? WHERE symbol=?",
                    (tp_pct, sl_pct, symbol),
                )
            results.append({
                "symbol": symbol, "ok": True,
                "tp_price": tp_price, "sl_price": sl_price,
                "tp_id": str(legs["TP"]["id"]), "sl_id": str(legs["SL"]["id"]),
            })
        except Exception as e:
            logger.warning("reapply tpsl %s: %s", symbol, e)
            results.append({"symbol": symbol, "ok": False, "error": str(e)})
    return results


def format_tpsl_results(results: list[dict]) -> str:
    """Tell the user which saved settings reached confirmed exchange orders."""
    if not results:
        return "ℹ️ Настройка сохранена; управляемых открытых позиций нет."
    lines = ["🛡 Статус активной защиты:"]
    for result in results:
        symbol = result.get("symbol", "*").split("/")[0]
        if result.get("ok"):
            lines.append(
                f"✅ `{symbol}`: TP `{result['tp_price']:.6g}` (ID `{result['tp_id']}`) и "
                f"SL `{result['sl_price']:.6g}` (ID `{result['sl_id']}`) подтверждены"
            )
        else:
            lines.append(f"⚠️ `{symbol}`: защита НЕ подтверждена — {result.get('error', 'неизвестная ошибка')}")
    return "\n".join(lines)


async def avg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback for /avg inline wizard."""
    q = update.callback_query
    await q.answer()
    config = context.bot_data.get("config")
    if not config:
        await q.edit_message_text("❌ Конфиг недоступен.")
        return

    if q.data in ("avg_edit", "avg_back"):
        context.user_data.pop("avg_pending", None)
        await q.edit_message_text(
            _build_avg_text(config) + "\n\n_Выбери параметр для изменения:_",
            parse_mode="Markdown",
            reply_markup=_build_avg_select_kb(config),
        )
        return

    if q.data == "avg_done":
        context.user_data.pop("avg_pending", None)
        client = context.bot_data.get("exchange")
        free_balance: float | None = None
        open_count = 0
        if client:
            try:
                free_balance = await client.get_free_futures_balance()
            except Exception:
                pass
            pos_cache = context.bot_data.get("_pos_cache")
            open_count = len(pos_cache) if pos_cache is not None else 0
        await q.edit_message_text(
            _build_avg_text(config, free_balance, open_count),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Изменить", callback_data="avg_edit")]]),
        )
        return

    if q.data.startswith("avg_pick_"):
        key = q.data[len("avg_pick_"):]
        pending = avg_pending_for_key(key)
        if not pending:
            await q.answer("Неизвестный параметр")
            return
        context.user_data["avg_pending"] = pending
        await q.edit_message_text(
            avg_question_text(config, pending),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад", callback_data="avg_back")]]),
        )
        return

    if q.data == "avg_cancel":
        context.user_data.pop("avg_pending", None)
        context.user_data.pop("avg_wizard", None)
        await q.edit_message_text("✖ Изменение настроек отменено.")
        return

    if q.data == "dyn_setup":
        from bot import db as db_mod
        dyn_raw = db_mod.get_config("avg_dynamic_rules", "")
        cur_text = ""
        if dyn_raw:
            try:
                dyn = json.loads(dyn_raw)
                if dyn:
                    rows_txt = []
                    for i, r in enumerate(dyn, 1):
                        rows_txt.append(f"  {i}. после {r['after']} докупок → PnL≤{r['pnl']:.0f}%")
                    cur_text = "\n*Текущие правила:*\n" + "\n".join(rows_txt) + "\n\n"
            except Exception:
                pass
        context.user_data["dyn_wizard"] = {"phase": "count", "rules": []}
        await q.edit_message_text(
            f"⚙️ *Динамика докупки*{cur_text}\n"
            "Сколько ступеней правил? (0 — отключить динамику)\n"
            "Каждая ступень: порог PnL и сумма меняются после N докупок.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖ Отмена", callback_data="dyn_cancel")]]),
        )
        return

    if q.data == "dyn_cancel":
        context.user_data.pop("dyn_wizard", None)
        config = context.bot_data.get("config")
        client = context.bot_data.get("exchange")
        free_balance: float | None = None
        open_count = 0
        if client:
            try:
                free_balance = await client.get_free_futures_balance()
            except Exception:
                pass
            pos_cache = context.bot_data.get("_pos_cache")
            open_count = len(pos_cache) if pos_cache is not None else 0
        await q.edit_message_text(
            _build_avg_text(config, free_balance, open_count),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Изменить", callback_data="avg_edit")],
                [InlineKeyboardButton("⚙️ Динамика докупки", callback_data="dyn_setup")],
            ]),
        )
        return


async def _finish_wizard(chat_id: int, context, changed: dict, config) -> None:
    if not changed:
        await context.bot.send_message(chat_id=chat_id, text="Ничего не изменено.")
        return
    lines = ["✅ *Настройки обновлены:*", ""]
    unit_map = {key: unit for _, key, _, _, _, unit in AVG_WIZARD_STEPS}
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
        results = await _reapply_tpsl_all(context.application, config)
        await context.bot.send_message(chat_id=chat_id, text=format_tpsl_results(results), parse_mode="Markdown")
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
        if isinstance(attr, bool):
            setattr(config, key, value.lower() in ("1", "true", "yes", "on"))
        elif isinstance(attr, float):
            setattr(config, key, float(value))
        elif isinstance(attr, int):
            setattr(config, key, int(value))
        else:
            setattr(config, key, value)
    except Exception:
        setattr(config, key, value)
    from bot.event_logger import configure_audit, sanitize
    configure_audit(config)
    displayed = sanitize({key: value})[key]
    await update.message.reply_text(f"✅ Сохранено: {key} = {displayed}")


async def min_open_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback для подтверждения открытия с минимальным ордером."""
    query = update.callback_query
    await query.answer()

    if query.data == "min_open_no":
        context.user_data.pop("pending_min_open", None)
        await query.edit_message_text("❌ Отменено")
        return

    pending = context.user_data.pop("pending_min_open", None)
    if not pending:
        await query.edit_message_text("❌ Нет данных — попробуй снова")
        return

    client = context.bot_data["exchange"]
    coin = pending["symbol"].split("/")[0]
    margin = pending["margin"]
    await query.edit_message_text(
        f"🔻 Открываю SHORT `{coin}` ${margin:.2f}...", parse_mode="Markdown"
    )
    try:
        result = await execute_open(
            client, context.application,
            pending["symbol"], pending["side"], margin, pending["leverage"],
            tp_pct=pending["tp_pct"], sl_pct=pending["sl_pct"],
        )
        lines = [
            f"*{coin}* 🔻×{result['leverage']} `${margin:.2f}`",
            f"▶ Entry: `{result['entry_price']:.6g}`",
        ]
        if result.get("liquidation_price"):
            lines.append(f"💀 Liq: `{result['liquidation_price']:.6g}`")
        if result.get("tp_price"):
            lines.append(f"✅ TP: `{result['tp_price']:.6g}` (+{pending['tp_pct']:.0f}%)")
        if result.get("sl_price"):
            lines.append(f"🛑 SL: `{result['sl_price']:.6g}` (-{pending['sl_pct']:.0f}%)")
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка: {e}")


async def avgunlock_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разблокировать докупки для символа (убрать из avg_exhausted)."""
    query = update.callback_query
    await query.answer()
    symbol = query.data[len("avgunlock_"):]
    notified_exhausted: set = context.bot_data.setdefault("_avg_notified_exhausted", set())
    coin = symbol.split("/")[0]
    if symbol in notified_exhausted:
        notified_exhausted.discard(symbol)
        from bot.jobs.main import _save_exhausted
        _save_exhausted(notified_exhausted)
        await query.edit_message_text(
            f"🔓 *Докупки `{coin}` разблокированы*\n"
            f"Бот возобновит докупки при PnL ≤ порога",
            parse_mode="Markdown"
        )
    else:
        await query.edit_message_text(
            f"✅ `{coin}` уже не заблокирован",
            parse_mode="Markdown"
        )

"""/scan — LLM + web-search шорт-пикер."""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.ai.scanner import scan_overbought, analyze_single_coin, mexc_find_futures_symbol, format_coin_card
from bot.ai.analyst import (deep_short_analysis, parse_analyst_blocks, extract_sentiment,
                             format_usage_footer, DEFAULT_MODEL, FALLBACK_MODEL)
from bot.handlers import wizard
from bot.handlers.wizard import Step

logger = logging.getLogger(__name__)


async def _run_scan(context: ContextTypes.DEFAULT_TYPE, chat_id: int, n: int) -> None:
    config = context.bot_data["config"]
    client = context.bot_data["exchange"]

    api_key = config.openrouter_api_key
    if not api_key:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Нет OpenRouter ключа. Добавь через /setkey openrouter_api_key sk-or-...",
        )
        return

    status = await context.bot.send_message(chat_id=chat_id, text="🧠 Думаю...")

    try:
        local_results, _total = await scan_overbought(client, 65.0, 10.0)
    except Exception as e:
        logger.warning("Local scan failed: %s", e)
        local_results = []

    model = getattr(config, "openrouter_model", DEFAULT_MODEL) or DEFAULT_MODEL
    await status.edit_text(f"🔍 Анализирую через {model}...")

    ai_result = await deep_short_analysis(local_results, api_key, model=model, n=n)

    if ai_result.error and not ai_result.text:
        logger.warning("Primary model failed, trying fallback %s", FALLBACK_MODEL)
        await status.edit_text(f"🌐 Пробую {FALLBACK_MODEL}...")
        ai_result = await deep_short_analysis(local_results, api_key, model=FALLBACK_MODEL, n=n)

    if ai_result.error and not ai_result.text:
        await status.edit_text(f"❌ AI недоступен: {ai_result.error}")
        return

    picks = parse_analyst_blocks(ai_result.text, n=n)
    if not picks:
        await status.edit_text(
            f"📝 AI ответил не по формату:\n\n{ai_result.text[:3000]}\n\n"
            f"{format_usage_footer(ai_result)}",
            parse_mode="Markdown",
        )
        return

    await status.edit_text(f"✅ AI выдал {len(picks)} монет. Проверяю MEXC...")

    try:
        open_positions = await client.get_positions()
        open_coins = {p["symbol"].split("/")[0] for p in open_positions}
    except Exception:
        open_coins = set()

    validated: list[dict] = []
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()

    for pick in picks:
        ticker = pick["ticker"].upper()
        if ticker in seen:
            continue
        seen.add(ticker)
        if ticker in open_coins:
            skipped.append((ticker, "уже в позиции"))
            continue
        fut_sym = await mexc_find_futures_symbol(client, ticker)
        if not fut_sym:
            skipped.append((ticker, "нет на MEXC"))
            continue
        tech = await analyze_single_coin(client, fut_sym)
        if not tech:
            skipped.append((ticker, "нет OHLCV"))
            continue
        tech["_ai_fund"] = pick.get("fund", "")
        tech["_ai_funding"] = pick.get("funding", "")
        tech["_ai_risk"] = pick.get("risk", "")
        validated.append(tech)

    try:
        await status.delete()
    except Exception:
        pass

    if not validated:
        summary = ", ".join(f"{t} ({r})" for t, r in skipped[:5])
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Ничего не прошло проверку MEXC.\nПропущено: {summary}",
        )
        return

    header = f"*🎯 AI top-{len(validated)} шорт ({ai_result.model})*"
    if skipped:
        header += f"\n_пропущено: {', '.join(t for t, _ in skipped[:5])}_"
    await context.bot.send_message(chat_id=chat_id, text=header, parse_mode="Markdown")

    default_bet = float(getattr(config, "default_trade_usdt", 0.20))

    for i, r in enumerate(validated, 1):
        sym = r["symbol"]
        direction = r.get("direction", "short")
        side_code = "sell" if direction == "short" else "buy"

        try:
            sym_max = await client.get_max_leverage(sym)
        except Exception:
            sym_max = 100
        user_lev = int(getattr(config, "default_leverage", 0) or 0)
        lev_eff = min(user_lev, sym_max) if user_lev > 0 else sym_max

        card = format_coin_card(r, i, ai_note=r.get("_ai_fund", ""),
                                max_lev=lev_eff, margin=default_bet)
        if r.get("_ai_funding"):
            card += f"\n   Фандинг (AI): {r['_ai_funding']}"
        if r.get("_ai_risk"):
            card += f"\n   Риск (AI): {r['_ai_risk']}"

        icon = "🔻" if direction == "short" else "🔺"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"{icon} ${default_bet:g} · {lev_eff}x",
                callback_data=f"open_{side_code}_{sym}",
            )
        ]])
        try:
            await context.bot.send_message(chat_id=chat_id, text=card,
                                           parse_mode="Markdown", reply_markup=kb)
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=card, reply_markup=kb)

    tail = []
    sentiment = extract_sentiment(ai_result.text)
    if sentiment:
        tail.append(f"📝 _{sentiment}_")
    tail.append(format_usage_footer(ai_result))
    try:
        await context.bot.send_message(chat_id=chat_id, text="\n\n".join(tail),
                                       parse_mode="Markdown")
    except Exception:
        await context.bot.send_message(chat_id=chat_id, text="\n\n".join(tail))

    context.bot_data["last_scan"] = validated


SCAN_STEPS: list[Step] = [
    Step(key="count", prompt="Сколько монет искать? (1–20, SKIP = 5)",
         kind="int:1:20", optional=True),
]


async def _scan_finish(context, chat_id: int, wizard_state: dict) -> None:
    values = wizard_state.get("values", {}) or {}
    n = int(values.get("count", 5)) if "count" in values else 5
    await _run_scan(context, chat_id, n)


wizard.register("scan", SCAN_STEPS, _scan_finish)


async def scan_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if args:
        try:
            n = max(1, min(int(args[0]), 20))
        except ValueError:
            n = 5
        await _run_scan(context, update.message.chat_id, n)
        return

    wizard.start_wizard(context, "scan")
    await wizard.render_step(context.bot, update.message.chat_id, "scan", 0, context)


async def scan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await wizard.handle_callback(update, context, "scan")


async def open_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles open_{side}_{symbol} — opens short/long immediately after confirm."""
    q = update.callback_query
    await q.answer()

    parts = q.data.split("_", 2)  # open, sell/buy, symbol
    if len(parts) < 3:
        return
    _, side, symbol = parts
    config = context.bot_data["config"]
    client = context.bot_data["exchange"]

    margin = float(getattr(config, "default_trade_usdt", 0.20))
    user_lev = int(getattr(config, "default_leverage", 0) or 0)
    try:
        sym_max = await client.get_max_leverage(symbol)
    except Exception:
        sym_max = 100
    leverage = min(user_lev, sym_max) if user_lev > 0 else sym_max

    coin = symbol.split("/")[0]
    side_ru = "SHORT" if side == "sell" else "LONG"
    await q.message.reply_text(f"🚀 Открываю {side_ru} `{coin}` ${margin:g} ×{leverage}...",
                                parse_mode="Markdown")

    try:
        from bot.handlers.trading import execute_open
        result = await execute_open(client, context.application, symbol, side, margin, leverage)
        icon = "🔻" if side == "sell" else "🟩"
        lines = [
            f"*{coin}* {icon}×{result['leverage']} `${margin:.2f}`",
            f"▶ Entry: `{result['entry_price']:.6g}`",
        ]
        if result.get("liquidation_price"):
            lines.append(f"💀 Liq: `{result['liquidation_price']:.6g}`")
        if result.get("tp_price"):
            lines.append(f"✅ TP: `{result['tp_price']:.6g}`")
        if result.get("sl_price"):
            lines.append(f"🛑 SL: `{result['sl_price']:.6g}`")
        await q.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await q.message.reply_text(f"❌ Ошибка: {e}")

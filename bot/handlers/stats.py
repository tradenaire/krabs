"""/stats — дневная статистика."""
from telegram import Update
from telegram.ext import ContextTypes


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from bot import db as db_mod
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date().isoformat()
    s = db_mod.get_daily_stats(today)

    client = context.bot_data.get("exchange")

    # Live data
    bal_free = bal_total = 0.0
    unrealized = 0.0
    n_pos = 0
    try:
        if client:
            bal = await client.get_futures_balance()
            bal_free = float(bal["free"]["USDT"])
            bal_total = float(bal["total"]["USDT"])
            positions = await client.get_positions()
            n_pos = len(positions)
            unrealized = sum(float(p.get("unrealized_pnl", 0)) for p in positions)
    except Exception:
        pass

    u_sign = "+" if unrealized >= 0 else ""
    r_sign = "+" if s["realized_pnl"] >= 0 else ""

    closes = s.get("closes", 0)
    wins = s.get("wins", 0)
    losses = s.get("losses", 0)
    win_rate = f"{wins/closes*100:.0f}%" if closes > 0 else "—"

    lines = [
        f"📈 *Статистика* `{today}`",
        "",
        f"*Сделки сегодня*",
        f"Открыто: `{s['opens']}` | Закрыто: `{closes}`",
        f"Победы/Поражения: `{wins}W / {losses}L` ({win_rate})",
        f"Докупок: `{s['avg_count']}` (+`${s['avg_amount']:.2f}`)",
        f"Перезаходов: `{s['reentry_count']}`",
        "",
        f"*P&L*",
        f"Известный PnL: `{r_sign}${s['realized_pnl']:.4f}`; неизвестен: {s.get('unknown_pnl', 0)}",
        f"Нереализовано: `{u_sign}${unrealized:.4f}`",
        "",
        f"*Баланс*",
        f"Всего: `${bal_total:.4f}`",
        f"Свободно: `${bal_free:.4f}`",
        f"Позиций открыто: `{n_pos}`",
    ]

    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if msg:
        await msg.reply_text("\n".join(lines), parse_mode="Markdown")

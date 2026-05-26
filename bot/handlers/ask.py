"""/ask — задать вопрос AI прямо из бота.

Синтаксис:
  /ask <вопрос>
  /ask @jobs почему докупка не сработала?
  /ask @trading @scan объясни логику открытия

Теги для файлов: @jobs @trading @scan @balance @positions @assistant
                 @client @db @config @pos_format @automode @ask
Спецтеги: @log (последние 150 строк bot.log)
"""
import logging
import re
from pathlib import Path
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_BOT_ROOT = Path(__file__).parent.parent

_FILE_MAP = {
    "jobs":       _BOT_ROOT / "jobs" / "main.py",
    "trading":    _BOT_ROOT / "handlers" / "trading.py",
    "scan":       _BOT_ROOT / "handlers" / "scan.py",
    "balance":    _BOT_ROOT / "handlers" / "balance.py",
    "positions":  _BOT_ROOT / "handlers" / "positions.py",
    "assistant":  _BOT_ROOT / "handlers" / "assistant.py",
    "automode":   _BOT_ROOT / "handlers" / "automode.py",
    "ask":        _BOT_ROOT / "handlers" / "ask.py",
    "client":     _BOT_ROOT / "exchange" / "client.py",
    "db":         _BOT_ROOT / "db.py",
    "config":     _BOT_ROOT / "config.py",
    "pos_format": _BOT_ROOT / "pos_format.py",
}

_LOG_PATH = _BOT_ROOT.parent / "data" / "bot.log"
_LOG_LINES = 150
_FILE_MAX_CHARS = 12_000  # per file, to stay within context


def _read_file(path: Path, max_chars: int = _FILE_MAX_CHARS) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[-max_chars:]
            text = "…(начало обрезано)\n" + text
        return text
    except Exception as e:
        return f"(не удалось прочитать {path.name}: {e})"


def _read_log(n: int = _LOG_LINES) -> str:
    try:
        lines = _LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception as e:
        return f"(не удалось прочитать лог: {e})"


_SYSTEM = (
    "Ты ассистент для отладки и эксплуатации MEXC futures-бота (Python, python-telegram-bot, APScheduler, ccxt).\n"
    "Отвечай кратко и по делу. Если есть исходный код или логи — цитируй конкретные строки.\n"
    "Объясняй причины, не просто симптомы."
)


async def ask_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ask [@тег ...] <вопрос>"""
    args = context.args or []
    raw = " ".join(args).strip()
    if not raw:
        tag_list = ", ".join(f"@{t}" for t in _FILE_MAP)
        await update.message.reply_text(
            "Использование: `/ask <вопрос>`\n\n"
            f"Теги файлов: {tag_list}\n"
            "Спецтег: `@log` — последние логи бота\n\n"
            "Пример: `/ask @jobs почему докупка не сработала?`",
            parse_mode="Markdown",
        )
        return

    config = context.bot_data.get("config")
    api_key = getattr(config, "openrouter_api_key", "") if config else ""
    anthropic_key = getattr(config, "anthropic_api_key", "") if config else ""
    if not api_key and not anthropic_key:
        await update.message.reply_text(
            "❌ Нет API ключа. Добавь: `/setkey openrouter_api_key sk-or-...`",
            parse_mode="Markdown",
        )
        return

    # Extract @tags and strip them from query
    tags = re.findall(r'@(\w+)', raw)
    query = re.sub(r'@\w+\s*', '', raw).strip()
    if not query:
        query = "Объясни что здесь происходит"

    # Build extra context blocks
    ctx_blocks: list[str] = []

    # Always include last log lines (compact version unless @log explicit)
    if "log" in tags:
        tags = [t for t in tags if t != "log"]
        ctx_blocks.append(f"=== bot.log (последние {_LOG_LINES} строк) ===\n{_read_log(_LOG_LINES)}")
    else:
        # Always append short tail for context (last 30 lines)
        ctx_blocks.append(f"=== bot.log (хвост) ===\n{_read_log(30)}")

    # Source files requested via tags
    for tag in tags:
        path = _FILE_MAP.get(tag.lower())
        if path:
            ctx_blocks.append(f"=== {path.name} ===\n{_read_file(path)}")
        else:
            ctx_blocks.append(f"(неизвестный тег @{tag}; доступны: {', '.join(_FILE_MAP)})")

    # Positions context
    try:
        from bot import db as db_mod
        positions = context.bot_data.get("_pos_cache") or []
        db_recs = {p["symbol"]: p for p in db_mod.get_open_positions()}
        if positions:
            lines = []
            for p in positions:
                sym = p["symbol"]
                coin = sym.split("/")[0]
                side = p.get("side", "?")
                lev = p.get("leverage", 1)
                entry = p.get("entry_price", 0)
                mark = p.get("mark_price", 0)
                pct = p.get("percentage", 0)
                pnl = p.get("unrealized_pnl", 0)
                margin = p.get("margin", 0)
                db = db_recs.get(sym, {})
                avg_count = db.get("averaging_count", 0)
                invested = db.get("total_invested", margin)
                lines.append(
                    f"{coin} {side}×{lev}: entry={entry:.4g} mark={mark:.4g} "
                    f"PnL={pct:+.1f}% (${pnl:+.2f}) margin=${margin:.2f} "
                    f"докупок={avg_count} вложено=${invested:.2f}"
                )
            ctx_blocks.append("=== Позиции ===\n" + "\n".join(lines))
    except Exception:
        pass

    system_content = _SYSTEM
    if ctx_blocks:
        system_content += "\n\n" + "\n\n".join(ctx_blocks)

    msg = await update.message.reply_text("🤔 Анализирую...")

    try:
        from openai import AsyncOpenAI

        if anthropic_key:
            client = AsyncOpenAI(api_key=anthropic_key, base_url="https://openrouter.ai/api/v1")
            model = "anthropic/claude-sonnet-4-6"
        else:
            client = AsyncOpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
            model = getattr(config, "openrouter_model", "x-ai/grok-4-fast:online") or "x-ai/grok-4-fast:online"

        result = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": query},
            ],
            max_tokens=2000,
            temperature=0.2,
        )
        answer = (result.choices[0].message.content or "").strip()
        answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.DOTALL).strip()

        if not answer:
            answer = "❌ Пустой ответ от модели."
        elif len(answer) > 4000:
            answer = answer[:4000] + "\n…_(обрезано)_"

        used_model = model.split("/")[-1]
        await msg.edit_text(f"{answer}\n\n_— {used_model}_", parse_mode="Markdown")

    except Exception as e:
        logger.error("ask_handler: %s", e)
        await msg.edit_text(f"❌ Ошибка: {e}")

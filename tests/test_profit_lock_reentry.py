"""Tests для гарантированного re-entry после profit-lock SL.

Покрывает:
- bot/db.py: миграция reentry.profit_locked, set_reentry_profit_locked, что
  upsert_reentry НЕ сбрасывает флаг при ON CONFLICT.
- bot/jobs/main.py: _apply_profit_lock_override — все 4 кейса (loss-SL+flag → override,
  unknown+flag → no, profitable+flag → no, TP+flag → no).
"""
import sqlite3
from pathlib import Path

import pytest

from bot.jobs.main import _apply_profit_lock_override


# ── Helpers ──────────────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Подменяет bot.db.DB_PATH на временный файл, чтобы тесты не трогали prod БД."""
    test_db = tmp_path / "test.db"
    import bot.db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", test_db)
    yield test_db


# ── Migration ────────────────────────────────────────────────────


def test_db_migration_adds_profit_locked_to_old_schema(tmp_db, monkeypatch):
    """Старая БД без колонки profit_locked → init_db() мигрирует чисто."""
    # 1) Создать old schema reentry table вручную (без profit_locked).
    conn = sqlite3.connect(str(tmp_db))
    conn.execute("""
        CREATE TABLE reentry (
            symbol TEXT PRIMARY KEY,
            side TEXT NOT NULL,
            margin REAL DEFAULT 1.0,
            leverage INTEGER DEFAULT 0,
            tp_pct REAL DEFAULT 500,
            sl_pct REAL DEFAULT 500,
            cycle_count INTEGER DEFAULT 0,
            max_cycles INTEGER DEFAULT 3,
            updated_at TEXT
        );
    """)
    conn.execute(
        "INSERT INTO reentry (symbol, side) VALUES ('BTC/USDT:USDT', 'sell')"
    )
    conn.commit()
    conn.close()

    # 2) Запустить init_db — должен добавить колонку без ошибок.
    import bot.db as db_mod
    db_mod.init_db()

    # 3) Проверить что колонка теперь есть.
    conn = sqlite3.connect(str(tmp_db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reentry)").fetchall()}
    assert "profit_locked" in cols
    # И существующая запись имеет default 0.
    row = conn.execute(
        "SELECT profit_locked FROM reentry WHERE symbol='BTC/USDT:USDT'"
    ).fetchone()
    assert row[0] == 0
    conn.close()


def test_db_migration_idempotent_when_column_exists(tmp_db):
    """Повторный init_db на уже мигрированной БД не падает."""
    import bot.db as db_mod
    db_mod.init_db()
    db_mod.init_db()  # вторая попытка — должна быть no-op для миграции.
    conn = sqlite3.connect(str(tmp_db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reentry)").fetchall()}
    assert "profit_locked" in cols
    conn.close()


# ── set_reentry_profit_locked ───────────────────────────────────


def test_set_reentry_profit_locked_round_trip(tmp_db):
    """upsert_reentry → set True → get показывает 1; set False → get показывает 0."""
    import bot.db as db_mod
    db_mod.init_db()
    db_mod.upsert_reentry(
        symbol="BTC/USDT:USDT", side="sell", margin=1.0, leverage=10,
        tp_pct=500, sl_pct=500, max_cycles=3, cycle_count=0,
    )
    rec = db_mod.get_reentry("BTC/USDT:USDT")
    assert rec is not None
    assert rec.get("profit_locked") == 0  # default

    db_mod.set_reentry_profit_locked("BTC/USDT:USDT", True)
    assert db_mod.get_reentry("BTC/USDT:USDT")["profit_locked"] == 1

    db_mod.set_reentry_profit_locked("BTC/USDT:USDT", False)
    assert db_mod.get_reentry("BTC/USDT:USDT")["profit_locked"] == 0


def test_set_reentry_profit_locked_noop_for_missing_symbol(tmp_db):
    """Вызов на отсутствующий symbol — no-op (не падает, не создаёт row)."""
    import bot.db as db_mod
    db_mod.init_db()
    # Не вызываем падение, просто проверяем что не было row до и нет после.
    db_mod.set_reentry_profit_locked("NOTHING/USDT:USDT", True)
    assert db_mod.get_reentry("NOTHING/USDT:USDT") is None


def test_upsert_reentry_does_not_reset_profit_locked_flag(tmp_db):
    """Критично: повторный upsert_reentry для того же symbol не должен сбрасывать
    флаг — иначе averaging_job recalc TP/SL стирает наши гарантии re-entry."""
    import bot.db as db_mod
    db_mod.init_db()
    db_mod.upsert_reentry(
        symbol="ETH/USDT:USDT", side="sell", margin=1.0, leverage=10,
        tp_pct=500, sl_pct=500, max_cycles=3,
    )
    db_mod.set_reentry_profit_locked("ETH/USDT:USDT", True)
    assert db_mod.get_reentry("ETH/USDT:USDT")["profit_locked"] == 1

    # Симулируем второй upsert (например при перевыставлении TP/SL).
    db_mod.upsert_reentry(
        symbol="ETH/USDT:USDT", side="sell", margin=1.0, leverage=10,
        tp_pct=400, sl_pct=400, max_cycles=3,  # изменили tp_pct/sl_pct
    )
    # Флаг должен сохраниться.
    assert db_mod.get_reentry("ETH/USDT:USDT")["profit_locked"] == 1


# ── _apply_profit_lock_override ─────────────────────────────────


def test_override_loss_sl_with_flag_forces_profitable():
    """Главный кейс: was_closed_by_tp вернул (False, ...) → profitable_sl=False,
    но флаг profit_locked=1 → форсим profitable_sl=True для re-entry."""
    closed_by_tp, profitable_sl = _apply_profit_lock_override(False, False, {"profit_locked": 1})
    assert closed_by_tp is False
    assert profitable_sl is True


def test_no_override_when_resolution_unknown():
    """closed_by_tp=None означает 'позиция возможно ещё открыта на бирже' — нельзя
    re-enter преждевременно. Override не применяется."""
    closed_by_tp, profitable_sl = _apply_profit_lock_override(None, False, {"profit_locked": 1})
    assert closed_by_tp is None
    assert profitable_sl is False


def test_no_override_when_already_profitable_sl():
    """Если резолв уже определил profitable_sl=True (trigger_price есть и в плюсе) —
    override бессмысленен, оставляем как есть."""
    closed_by_tp, profitable_sl = _apply_profit_lock_override(False, True, {"profit_locked": 1})
    assert closed_by_tp is False
    assert profitable_sl is True


def test_no_override_when_closed_by_tp():
    """Если резолв определил TP — re-enter и так сработает. Не трогаем."""
    closed_by_tp, profitable_sl = _apply_profit_lock_override(True, False, {"profit_locked": 1})
    assert closed_by_tp is True
    assert profitable_sl is False


def test_no_override_when_flag_not_set():
    """Без флага override никогда не срабатывает — обычное поведение loss-SL ветки."""
    closed_by_tp, profitable_sl = _apply_profit_lock_override(False, False, {"profit_locked": 0})
    assert closed_by_tp is False
    assert profitable_sl is False
    # Также для отсутствующего ключа.
    closed_by_tp, profitable_sl = _apply_profit_lock_override(False, False, {})
    assert profitable_sl is False

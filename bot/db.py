"""SQLite store — config + positions + re-entry."""
import json
import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "bot.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_account (
                id INTEGER PRIMARY KEY DEFAULT 1,
                balance REAL NOT NULL DEFAULT 500.0,
                initial_balance REAL NOT NULL DEFAULT 500.0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS paper_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL DEFAULT 0,
                leverage INTEGER DEFAULT 1,
                margin REAL DEFAULT 0,
                total_invested REAL DEFAULT 0,
                averaging_count INTEGER DEFAULT 0,
                averaging_budget REAL DEFAULT 50.0,
                tp_pct REAL DEFAULT 500,
                sl_pct REAL DEFAULT 500,
                status TEXT DEFAULT 'open',
                close_price REAL DEFAULT 0,
                realized_pnl REAL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                closed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_trade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                side TEXT DEFAULT '',
                entry_price REAL DEFAULT 0,
                close_price REAL DEFAULT 0,
                margin REAL DEFAULT 0,
                pnl REAL DEFAULT 0,
                note TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL DEFAULT 0,
                leverage INTEGER DEFAULT 1,
                margin REAL DEFAULT 0,
                total_invested REAL DEFAULT 0,
                averaging_count INTEGER DEFAULT 0,
                averaging_budget REAL DEFAULT 51.0,
                tp_pct REAL DEFAULT 500,
                sl_pct REAL DEFAULT 500,
                status TEXT DEFAULT 'open',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS reentry (
                symbol TEXT PRIMARY KEY,
                side TEXT NOT NULL,
                margin REAL DEFAULT 1.0,
                leverage INTEGER DEFAULT 0,
                tp_pct REAL DEFAULT 500,
                sl_pct REAL DEFAULT 500,
                cycle_count INTEGER DEFAULT 0,
                max_cycles INTEGER DEFAULT 3,
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS trade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                amount REAL DEFAULT 0,
                pnl REAL DEFAULT 0,
                note TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS position_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT,
                leverage INTEGER,
                entry_price REAL DEFAULT 0,
                exit_price REAL DEFAULT 0,
                initial_margin REAL DEFAULT 0,
                total_invested REAL DEFAULT 0,
                avg_count INTEGER DEFAULT 0,
                pnl REAL,
                close_reason TEXT,
                hold_seconds INTEGER,
                opened_at TEXT,
                closed_at TEXT,
                tp_pct REAL DEFAULT 500,
                sl_pct REAL DEFAULT 500,
                avg_threshold REAL DEFAULT -100,
                avg_amount REAL DEFAULT 0,
                avg_budget REAL DEFAULT 0,
                avg_max_count INTEGER DEFAULT 0,
                avg_interval INTEGER DEFAULT 0
            );
        """)


    # Migrate position_history — add avg settings columns if missing
    _ph_cols = [
        ("tp_pct",        "REAL DEFAULT 500"),
        ("sl_pct",        "REAL DEFAULT 500"),
        ("avg_threshold", "REAL DEFAULT -100"),
        ("avg_amount",    "REAL DEFAULT 0"),
        ("avg_budget",    "REAL DEFAULT 0"),
        ("avg_max_count", "INTEGER DEFAULT 0"),
        ("avg_interval",  "INTEGER DEFAULT 0"),
    ]
    with _connect() as conn:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(position_history)").fetchall()}
        for col, coldef in _ph_cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE position_history ADD COLUMN {col} {coldef}")


def get_all_config() -> dict[str, str]:
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_config(key: str, value: str):
    with _connect() as conn:
        conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                     (key, str(value)))


def get_config(key: str, default: str = "") -> str:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


# ── positions ─────────────────────────────────────────────────────

def upsert_position(symbol: str, side: str, entry_price: float, leverage: int,
                    margin: float, tp_pct: float = 500, sl_pct: float = 500,
                    budget: float = 5.0) -> int:
    with _connect() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO positions (symbol, side, entry_price, leverage, margin,
                total_invested, averaging_budget, tp_pct, sl_pct, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """, (symbol, side, entry_price, leverage, margin, margin, budget, tp_pct, sl_pct))
        row = conn.execute(
            "SELECT id FROM positions WHERE symbol=? AND status='open' ORDER BY id DESC LIMIT 1",
            (symbol,)
        ).fetchone()
        return row["id"] if row else 0


def get_open_positions() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='open'"
        ).fetchall()
    return [dict(r) for r in rows]


def get_open_position(symbol: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE symbol=? AND status='open' ORDER BY id DESC LIMIT 1",
            (symbol,)
        ).fetchone()
    return dict(row) if row else None


def update_averaging(pos_id: int, total_invested: float, avg_count: int,
                     new_entry: float = 0):
    with _connect() as conn:
        if new_entry:
            conn.execute(
                "UPDATE positions SET total_invested=?, averaging_count=?, entry_price=? WHERE id=?",
                (total_invested, avg_count, new_entry, pos_id)
            )
        else:
            conn.execute(
                "UPDATE positions SET total_invested=?, averaging_count=? WHERE id=?",
                (total_invested, avg_count, pos_id)
            )


def update_position_tpsl(symbol: str, tp_pct: float, sl_pct: float):
    with _connect() as conn:
        conn.execute(
            "UPDATE positions SET tp_pct=?, sl_pct=? WHERE symbol=? AND status='open'",
            (tp_pct, sl_pct, symbol)
        )


def close_position(symbol: str):
    with _connect() as conn:
        conn.execute(
            "UPDATE positions SET status='closed' WHERE symbol=? AND status='open'",
            (symbol,)
        )


# ── re-entry ──────────────────────────────────────────────────────

def get_reentry(symbol: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM reentry WHERE symbol=?", (symbol,)).fetchone()
    return dict(row) if row else None


def upsert_reentry(symbol: str, side: str, margin: float, leverage: int,
                   tp_pct: float, sl_pct: float, max_cycles: int = 3,
                   cycle_count: int = 0):
    with _connect() as conn:
        # Preserve existing cycle_count on update — only reset on fresh insert
        conn.execute("""
            INSERT INTO reentry
                (symbol, side, margin, leverage, tp_pct, sl_pct, max_cycles, cycle_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, datetime('now'))
            ON CONFLICT(symbol) DO UPDATE SET
                side=excluded.side, margin=excluded.margin, leverage=excluded.leverage,
                tp_pct=excluded.tp_pct, sl_pct=excluded.sl_pct, max_cycles=excluded.max_cycles,
                updated_at=excluded.updated_at
        """, (symbol, side, margin, leverage, tp_pct, sl_pct, max_cycles))


def increment_reentry_cycle(symbol: str) -> int:
    with _connect() as conn:
        conn.execute(
            "UPDATE reentry SET cycle_count=cycle_count+1 WHERE symbol=?", (symbol,)
        )
        row = conn.execute("SELECT cycle_count FROM reentry WHERE symbol=?", (symbol,)).fetchone()
    return row["cycle_count"] if row else 0


def delete_reentry(symbol: str):
    with _connect() as conn:
        conn.execute("DELETE FROM reentry WHERE symbol=?", (symbol,))


def get_all_reentry() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM reentry").fetchall()
    return [dict(r) for r in rows]


def dedupe_open_positions():
    """Keep only the record with the highest total_invested per symbol; close others."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, symbol, total_invested FROM positions WHERE status='open' ORDER BY symbol, total_invested DESC"
        ).fetchall()
        seen: set[str] = set()
        to_close: list[int] = []
        for row in rows:
            sym = row["symbol"]
            if sym in seen:
                to_close.append(row["id"])
            else:
                seen.add(sym)
        if to_close:
            conn.execute(
                f"UPDATE positions SET status='closed' WHERE id IN ({','.join('?' * len(to_close))})",
                to_close
            )
            logger.info("dedupe_open_positions: closed %d duplicate records", len(to_close))
    return len(to_close)


def sync_closed_positions(open_symbols: set[str]) -> list[str]:
    """Mark 'open' DB records as 'closed' for symbols not in open_symbols (exchange state)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM positions WHERE status='open'"
        ).fetchall()
        to_close = [r["symbol"] for r in rows if r["symbol"] not in open_symbols]
        for sym in to_close:
            conn.execute(
                "UPDATE positions SET status='closed' WHERE symbol=? AND status='open'", (sym,)
            )
            logger.info("sync_closed_positions: marked %s as closed (not on exchange)", sym)
    return to_close


# ── trade_log ─────────────────────────────────────────────────────

def log_trade(symbol: str, action: str, amount: float = 0,
              pnl: float = 0, note: str = ""):
    from datetime import date
    today = date.today().isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO trade_log (date, symbol, action, amount, pnl, note) VALUES (?,?,?,?,?,?)",
            (today, symbol, action, amount, pnl, note)
        )


def get_daily_stats(date_str: str | None = None) -> dict:
    from datetime import date
    d = date_str or date.today().isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT action, SUM(amount) as total_amount, SUM(pnl) as total_pnl, COUNT(*) as cnt "
            "FROM trade_log WHERE date=? GROUP BY action",
            (d,)
        ).fetchall()
    stats = {"date": d, "opens": 0, "avg_count": 0, "avg_amount": 0.0,
             "reentry_count": 0, "realized_pnl": 0.0,
             "closes": 0, "wins": 0, "losses": 0}
    for r in rows:
        action = r["action"]
        if action == "open":
            stats["opens"] = r["cnt"]
            stats["open_amount"] = r["total_amount"]
        elif action == "avg":
            stats["avg_count"] = r["cnt"]
            stats["avg_amount"] = float(r["total_amount"] or 0)
        elif action == "close":
            stats["closes"] = r["cnt"]
            stats["realized_pnl"] += float(r["total_pnl"] or 0)
        elif action == "reentry":
            stats["reentry_count"] = r["cnt"]
    with _connect() as conn:
        # Win = pnl > 0 (manual close in profit) OR note='tp' (closed by TP or profit-lock SL)
        stats["wins"] = conn.execute(
            "SELECT COUNT(*) FROM trade_log WHERE date=? AND action='close' AND (pnl > 0 OR note='tp')", (d,)
        ).fetchone()[0]
        stats["losses"] = conn.execute(
            "SELECT COUNT(*) FROM trade_log WHERE date=? AND action='close' AND pnl <= 0 AND note != 'tp'", (d,)
        ).fetchone()[0]
    return stats


# ── position_history ──────────────────────────────────────────────

def open_position_history(symbol: str, side: str, leverage: int,
                          entry_price: float, margin: float,
                          tp_pct: float = 500, sl_pct: float = 500,
                          avg_threshold: float = -100, avg_amount: float = 0,
                          avg_budget: float = 0, avg_max_count: int = 0,
                          avg_interval: int = 0):
    import datetime
    now = datetime.datetime.utcnow().isoformat()
    with _connect() as conn:
        conn.execute("""
            INSERT INTO position_history
            (symbol, side, leverage, entry_price, initial_margin, total_invested, avg_count,
             tp_pct, sl_pct, avg_threshold, avg_amount, avg_budget, avg_max_count, avg_interval,
             opened_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, side, leverage, entry_price, margin, margin,
              tp_pct, sl_pct, avg_threshold, avg_amount, avg_budget, avg_max_count, avg_interval,
              now))


def update_position_history_avg(symbol: str, total_invested: float, avg_count: int):
    with _connect() as conn:
        row = conn.execute("""
            SELECT id FROM position_history WHERE symbol=? AND closed_at IS NULL
            ORDER BY id DESC LIMIT 1
        """, (symbol,)).fetchone()
        if row:
            conn.execute("""
                UPDATE position_history SET total_invested=?, avg_count=? WHERE id=?
            """, (total_invested, avg_count, row["id"]))


def close_position_history(symbol: str, exit_price: float, pnl: float, close_reason: str):
    import datetime
    now = datetime.datetime.utcnow().isoformat()
    with _connect() as conn:
        row = conn.execute("""
            SELECT id, opened_at FROM position_history
            WHERE symbol=? AND closed_at IS NULL
            ORDER BY id DESC LIMIT 1
        """, (symbol,)).fetchone()
        if not row:
            return
        try:
            opened_dt = datetime.datetime.fromisoformat(row["opened_at"])
            hold_seconds = int((datetime.datetime.utcnow() - opened_dt).total_seconds())
        except Exception:
            hold_seconds = 0
        conn.execute("""
            UPDATE position_history
            SET exit_price=?, pnl=?, close_reason=?, closed_at=?, hold_seconds=?
            WHERE id=?
        """, (exit_price, pnl, close_reason, now, hold_seconds, row["id"]))


def get_last_position_history(symbol: str) -> dict | None:
    """Return the most recent position_history row for symbol (open or closed)."""
    with _connect() as conn:
        row = conn.execute("""
            SELECT * FROM position_history WHERE symbol=?
            ORDER BY id DESC LIMIT 1
        """, (symbol,)).fetchone()
    return dict(row) if row else None


def get_position_history(limit: int = 500, symbol: str | None = None) -> list[dict]:
    with _connect() as conn:
        if symbol:
            rows = conn.execute("""
                SELECT * FROM position_history WHERE symbol=?
                ORDER BY id DESC LIMIT ?
            """, (symbol, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM position_history ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── paper trading ─────────────────────────────────────────────────

def init_paper_account(initial_balance: float = 500.0):
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO paper_account (id, balance, initial_balance) VALUES (1, ?, ?)",
            (initial_balance, initial_balance)
        )


def get_paper_account() -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
    return dict(row) if row else {"balance": 500.0, "initial_balance": 500.0}


def update_paper_balance(delta: float):
    with _connect() as conn:
        conn.execute("UPDATE paper_account SET balance = balance + ? WHERE id=1", (delta,))


def open_paper_position(symbol: str, side: str, entry_price: float, leverage: int,
                         margin: float, avg_budget: float,
                         tp_pct: float = 500.0, sl_pct: float = 500.0) -> int:
    with _connect() as conn:
        cur = conn.execute("""
            INSERT INTO paper_positions
                (symbol, side, entry_price, leverage, margin, total_invested,
                 averaging_count, averaging_budget, tp_pct, sl_pct, status)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 'open')
        """, (symbol, side, entry_price, leverage, margin, margin, avg_budget, tp_pct, sl_pct))
        return cur.lastrowid


def get_open_paper_positions() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM paper_positions WHERE status='open' ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def get_closed_paper_positions(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM paper_positions WHERE status='closed' ORDER BY closed_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_paper_averaging(pos_id: int, total_invested: float, avg_count: int, new_entry: float):
    with _connect() as conn:
        conn.execute(
            "UPDATE paper_positions SET total_invested=?, averaging_count=?, entry_price=? WHERE id=?",
            (total_invested, avg_count, new_entry, pos_id)
        )


def close_paper_position(pos_id: int, close_price: float, realized_pnl: float):
    with _connect() as conn:
        conn.execute("""
            UPDATE paper_positions
            SET status='closed', close_price=?, realized_pnl=?, closed_at=datetime('now')
            WHERE id=?
        """, (close_price, realized_pnl, pos_id))


def log_paper_trade(symbol: str, action: str, side: str = "",
                    entry_price: float = 0, close_price: float = 0,
                    margin: float = 0, pnl: float = 0, note: str = ""):
    from datetime import date
    today = date.today().isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO paper_trade_log (date, symbol, action, side, entry_price, close_price, margin, pnl, note) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (today, symbol, action, side, entry_price, close_price, margin, pnl, note)
        )


def reset_paper_account(initial_balance: float = 500.0):
    """Close all open paper positions and reset balance."""
    with _connect() as conn:
        conn.execute(
            "UPDATE paper_positions SET status='closed', closed_at=datetime('now') WHERE status='open'"
        )
        conn.execute(
            "UPDATE paper_account SET balance=?, initial_balance=? WHERE id=1",
            (initial_balance, initial_balance)
        )


def get_paper_stats() -> dict:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT action, COUNT(*) as cnt, SUM(pnl) as total_pnl, SUM(margin) as total_margin "
            "FROM paper_trade_log GROUP BY action"
        ).fetchall()
    stats = {"opens": 0, "tp_count": 0, "sl_count": 0, "avg_count": 0,
             "tp_pnl": 0.0, "sl_pnl": 0.0}
    for r in rows:
        a = r["action"]
        if a == "open":
            stats["opens"] = r["cnt"]
        elif a == "tp":
            stats["tp_count"] = r["cnt"]
            stats["tp_pnl"] = float(r["total_pnl"] or 0)
        elif a == "sl":
            stats["sl_count"] = r["cnt"]
            stats["sl_pnl"] = float(r["total_pnl"] or 0)
        elif a == "avg":
            stats["avg_count"] = r["cnt"]
    return stats


# ── min_order_cache ───────────────────────────────────────────────

def get_min_order_cache() -> dict:
    """Load persisted MEXC minimum notional cache {symbol: min_usdt_notional}."""
    raw = get_config("_min_order_cache_json", "{}")
    try:
        return json.loads(raw)
    except Exception:
        return {}


def set_min_order_notional(symbol: str, min_notional: float):
    """Persist minimum USDT notional for symbol (survives restarts)."""
    cache = get_min_order_cache()
    cache[symbol] = min_notional
    set_config("_min_order_cache_json", json.dumps(cache))

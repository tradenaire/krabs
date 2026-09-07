"""SQLite store — config + positions + re-entry."""
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("KRABS_DATA_DIR", Path(__file__).parent.parent / "data"))
DB_PATH = DATA_DIR / "bot.db"


@contextmanager
def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


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
                closed_at TEXT,
                funding_accrued REAL DEFAULT 0,
                last_funding_ts TEXT DEFAULT NULL,
                profit_lock_step REAL DEFAULT 0,
                liquidation_price REAL DEFAULT 0,
                source TEXT DEFAULT 'scan'
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

        # Old symbol-only records do not prove ownership of a live exchange position.
        migrations = {
            "positions": [("exchange_position_id", "TEXT"), ("opened_at_ms", "INTEGER"),
                          ("locked_sl", "REAL"), ("profit_lock_step", "REAL DEFAULT 0"),
                          ("profit_lock_enabled", "INTEGER DEFAULT 1")],
            "reentry": [("position_key", "INTEGER")],
            "position_history": [("position_key", "INTEGER")],
            "trade_log": [("position_key", "INTEGER")],
        }
        for table, columns in migrations.items():
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        conn.executescript("""
            CREATE UNIQUE INDEX IF NOT EXISTS managed_exchange_position
                ON positions(exchange_position_id) WHERE exchange_position_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS close_once ON trade_log(position_key)
                WHERE action='close' AND position_key IS NOT NULL;
            CREATE TABLE IF NOT EXISTS bot_orders (
                order_id TEXT NOT NULL, order_type TEXT NOT NULL,
                position_key INTEGER NOT NULL, symbol TEXT NOT NULL,
                kind TEXT NOT NULL, price REAL, confirmed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(order_id, order_type)
            );
            CREATE TABLE IF NOT EXISTS closures (
                position_key INTEGER PRIMARY KEY, reason TEXT NOT NULL,
                pnl REAL, exit_price REAL, closed_at TEXT NOT NULL,
                order_ids TEXT NOT NULL, notified INTEGER NOT NULL DEFAULT 0
            );
        """)


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
                    budget: float = 5.0, total_invested: float = 0,
                    avg_count: int = 0, exchange_position_id: str | None = None,
                    opened_at_ms: int | None = None) -> int:
    ti = total_invested if total_invested > 0 else margin
    with _connect() as conn:
        if exchange_position_id:
            old = conn.execute("SELECT * FROM positions WHERE exchange_position_id=?",
                               (str(exchange_position_id),)).fetchone()
            if old:
                if old["status"] != "open" or old["opened_at_ms"] != opened_at_ms or old["side"] != side:
                    raise ValueError("Exchange position identity conflicts with a previous record")
                return old["id"]
        conn.execute("""
            INSERT OR IGNORE INTO positions (symbol, side, entry_price, leverage, margin,
                total_invested, averaging_count, averaging_budget, tp_pct, sl_pct, status,
                exchange_position_id, opened_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """, (symbol, side, entry_price, leverage, margin, ti, avg_count, budget, tp_pct, sl_pct,
              str(exchange_position_id) if exchange_position_id else None, opened_at_ms))
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


def get_managed_position(live: dict) -> dict | None:
    """Never infer ownership from a symbol, runtime cache, or an old DB record."""
    exchange_id = live.get("position_id")
    if not exchange_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE exchange_position_id=? AND symbol=? AND status='open'",
            (str(exchange_id), live["symbol"]),
        ).fetchone()
    if (row and row["side"] == live["side"]
            and live.get("opened_at_ms") is not None
            and int(row["opened_at_ms"] or 0) == int(live["opened_at_ms"])):
        return dict(row)
    return None


def get_position_by_id(position_key: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM positions WHERE id=?", (position_key,)).fetchone()
    return dict(row) if row else None


def save_bot_order(order_id, position_key: int, symbol: str, kind: str,
                   price: float = 0, order_type: str = "plan", confirmed: bool = False):
    if not order_id:
        raise ValueError("Exchange did not return an order ID")
    with _connect() as conn:
        conn.execute("INSERT OR REPLACE INTO bot_orders VALUES (?,?,?,?,?,?,?)",
                     (str(order_id), order_type, position_key, symbol, kind, price, int(confirmed)))


def get_bot_orders(position_key: int | None = None, order_type: str = "plan") -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM bot_orders WHERE order_type=? AND (? IS NULL OR position_key=?)",
            (order_type, position_key, position_key),
        ).fetchall()
    return [dict(row) for row in rows]


def confirm_protection(position_key: int, order_id: str, locked_sl: float | None = None,
                       profit_lock_step: float | None = None):
    with _connect() as conn:
        conn.execute("UPDATE bot_orders SET confirmed=1 WHERE order_id=? AND order_type='plan'",
                     (str(order_id),))
        if locked_sl is not None:
            conn.execute("UPDATE positions SET locked_sl=?, profit_lock_step=COALESCE(?, profit_lock_step) WHERE id=?",
                         (locked_sl, profit_lock_step, position_key))


def get_closure(position_key: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM closures WHERE position_key=?", (position_key,)).fetchone()
    return dict(row) if row else None


def record_closure(position: dict, result: dict) -> bool:
    """One transaction per position, shared by polling and every manual-close path."""
    import datetime as dt
    key = position["id"]
    closed_at = result["closed_at"]
    with _connect() as conn:
        inserted = conn.execute(
            "INSERT OR IGNORE INTO closures(position_key,reason,pnl,exit_price,closed_at,order_ids) VALUES (?,?,?,?,?,?)",
            (key, result["reason"], result.get("pnl"), result.get("exit_price"), closed_at,
             json.dumps(result.get("order_ids", []))),
        ).rowcount
        if not inserted:
            old = conn.execute("SELECT * FROM closures WHERE position_key=?", (key,)).fetchone()
            result = dict(result)
            if old["pnl"] is not None:
                result["pnl"] = old["pnl"]
            if old["reason"] != "unknown":
                result["reason"] = old["reason"]
            conn.execute("UPDATE closures SET reason=?,pnl=?,exit_price=?,order_ids=? WHERE position_key=?",
                         (result["reason"], result.get("pnl"), result.get("exit_price"),
                          json.dumps(result.get("order_ids", [])), key))
        conn.execute("UPDATE positions SET status='closed' WHERE id=?", (key,))
        conn.execute(
            "INSERT OR IGNORE INTO trade_log(date,symbol,action,amount,pnl,note,position_key) VALUES (?,?,'close',?,?,?,?)",
            (closed_at[:10], position["symbol"], position["total_invested"], result.get("pnl"), result["reason"], key),
        )
        conn.execute("UPDATE trade_log SET pnl=?,note=? WHERE position_key=? AND action='close'",
                     (result.get("pnl"), result["reason"], key))
        opened = position.get("opened_at_ms")
        held = max(0, int(dt.datetime.fromisoformat(closed_at).timestamp() - opened / 1000)) if opened else 0
        conn.execute(
            "UPDATE position_history SET exit_price=?,pnl=?,close_reason=?,closed_at=?,hold_seconds=? WHERE position_key=?",
            (result.get("exit_price"), result.get("pnl"), result["reason"], closed_at, held, key),
        )
    return bool(inserted)


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


# ── re-entry ──────────────────────────────────────────────────────

def get_reentry(symbol: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM reentry WHERE symbol=?", (symbol,)).fetchone()
    return dict(row) if row else None


def upsert_reentry(symbol: str, side: str, margin: float, leverage: int,
                   tp_pct: float, sl_pct: float, max_cycles: int = 3,
                   cycle_count: int = 0, position_key: int | None = None):
    with _connect() as conn:
        # Preserve existing cycle_count on update — only reset on fresh insert
        conn.execute("""
            INSERT INTO reentry
                (symbol, side, margin, leverage, tp_pct, sl_pct, max_cycles, cycle_count, updated_at, position_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
            ON CONFLICT(symbol) DO UPDATE SET
                side=excluded.side, margin=excluded.margin, leverage=excluded.leverage,
                tp_pct=excluded.tp_pct, sl_pct=excluded.sl_pct, max_cycles=excluded.max_cycles,
                cycle_count=excluded.cycle_count, position_key=excluded.position_key,
                updated_at=excluded.updated_at
        """, (symbol, side, margin, leverage, tp_pct, sl_pct, max_cycles, cycle_count, position_key))


def delete_reentry(symbol: str):
    with _connect() as conn:
        conn.execute("DELETE FROM reentry WHERE symbol=?", (symbol,))


def get_all_reentry() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM reentry").fetchall()
    return [dict(r) for r in rows]


# ── trade_log ─────────────────────────────────────────────────────

def log_trade(symbol: str, action: str, amount: float = 0,
              pnl: float = 0, note: str = ""):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO trade_log (date, symbol, action, amount, pnl, note) VALUES (?,?,?,?,?,?)",
            (today, symbol, action, amount, pnl, note)
        )


def get_daily_stats(date_str: str | None = None) -> dict:
    from datetime import datetime, timezone
    d = date_str or datetime.now(timezone.utc).date().isoformat()
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
        stats["wins"] = conn.execute(
            "SELECT COUNT(*) FROM trade_log WHERE date=? AND action='close' AND pnl > 0", (d,)
        ).fetchone()[0]
        stats["losses"] = conn.execute(
            "SELECT COUNT(*) FROM trade_log WHERE date=? AND action='close' AND pnl < 0", (d,)
        ).fetchone()[0]
        stats["unknown_pnl"] = conn.execute(
            "SELECT COUNT(*) FROM trade_log WHERE date=? AND action='close' AND pnl IS NULL", (d,)
        ).fetchone()[0]
    return stats


# ── position_history ──────────────────────────────────────────────

def open_position_history(symbol: str, side: str, leverage: int,
                          entry_price: float, margin: float,
                          tp_pct: float = 500, sl_pct: float = 500,
                          avg_threshold: float = -100, avg_amount: float = 0,
                          avg_budget: float = 0, avg_max_count: int = 0,
                          avg_interval: int = 0, position_key: int | None = None):
    import datetime
    now = datetime.datetime.utcnow().isoformat()
    with _connect() as conn:
        conn.execute("""
            INSERT INTO position_history
            (symbol, side, leverage, entry_price, initial_margin, total_invested, avg_count,
             tp_pct, sl_pct, avg_threshold, avg_amount, avg_budget, avg_max_count, avg_interval,
             opened_at, position_key)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, side, leverage, entry_price, margin, margin,
              tp_pct, sl_pct, avg_threshold, avg_amount, avg_budget, avg_max_count, avg_interval,
              now, position_key))


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
        for col, definition in [
            ("funding_accrued", "REAL DEFAULT 0"),
            ("last_funding_ts", "TEXT DEFAULT NULL"),
            ("profit_lock_step", "REAL DEFAULT 0"),
            ("liquidation_price", "REAL DEFAULT 0"),
            ("source", "TEXT DEFAULT 'scan'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE paper_positions ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists


def get_paper_account() -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
    return dict(row) if row else {"balance": 500.0, "initial_balance": 500.0}


def update_paper_balance(delta: float):
    with _connect() as conn:
        conn.execute("UPDATE paper_account SET balance = balance + ? WHERE id=1", (delta,))


def open_paper_position(symbol: str, side: str, entry_price: float, leverage: int,
                         margin: float, avg_budget: float,
                         tp_pct: float = 500.0, sl_pct: float = 500.0,
                         liq_price: float = 0.0, source: str = 'scan') -> int:
    with _connect() as conn:
        cur = conn.execute("""
            INSERT INTO paper_positions
                (symbol, side, entry_price, leverage, margin, total_invested,
                 averaging_count, averaging_budget, tp_pct, sl_pct, status,
                 liquidation_price, source)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 'open', ?, ?)
        """, (symbol, side, entry_price, leverage, margin, margin, avg_budget, tp_pct, sl_pct,
              liq_price, source))
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


def update_paper_funding(pos_id: int, delta_usd: float, ts_iso: str):
    with _connect() as conn:
        conn.execute(
            "UPDATE paper_positions SET funding_accrued = funding_accrued + ?, last_funding_ts = ? WHERE id = ?",
            (delta_usd, ts_iso, pos_id)
        )


def update_paper_profit_lock(pos_id: int, step: float):
    with _connect() as conn:
        conn.execute(
            "UPDATE paper_positions SET profit_lock_step = ? WHERE id = ?",
            (step, pos_id)
        )


def update_paper_liq_price(pos_id: int, liq_price: float):
    with _connect() as conn:
        conn.execute(
            "UPDATE paper_positions SET liquidation_price = ? WHERE id = ?",
            (liq_price, pos_id)
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

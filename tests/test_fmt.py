"""Tests for bot/fmt.py — close-PnL formatting (Fix #1)."""
from bot.fmt import calc_close_pnl, format_close_pnl, fmt_pct, fmt_usd


# ── calc_close_pnl ────────────────────────────────────────────────


def test_short_win_basic():
    """SHORT: цена упала с 2.20 до 2.10 (-4.55%), leverage 100, margin $0.50.
    PnL% = +4.55 * 100 = +454.5%, USDT = $0.50 * 4.545 = $2.27."""
    pnl, pct = calc_close_pnl(2.20, 2.10, "short", 100, 0.50)
    assert abs(pnl - 2.2727) < 1e-3
    assert abs(pct - 454.55) < 0.5


def test_short_loss_basic():
    """SHORT: цена выросла с 2.10 до 2.30 (+9.52%), leverage 100, margin $0.50.
    PnL% = -9.52 * 100 = -952%, USDT = -$4.76."""
    pnl, pct = calc_close_pnl(2.10, 2.30, "short", 100, 0.50)
    assert pnl < 0 and pct < 0
    assert abs(pnl - (-4.7619)) < 1e-3
    assert abs(pct - (-952.38)) < 0.5


def test_long_win_basic():
    """LONG: цена выросла с 2.0 до 2.2 (+10%), leverage 50, margin $1.0.
    PnL% = +500%, USDT = +$5.00."""
    pnl, pct = calc_close_pnl(2.0, 2.2, "long", 50, 1.0)
    assert abs(pnl - 5.0) < 1e-6
    assert abs(pct - 500.0) < 1e-6


def test_long_loss_basic():
    """LONG: цена упала — PnL отрицательный."""
    pnl, pct = calc_close_pnl(2.0, 1.8, "long", 10, 5.0)
    assert pnl < 0 and pct < 0
    # 10% drop * 10 leverage = -100% → -$5
    assert abs(pnl - (-5.0)) < 1e-6
    assert abs(pct - (-100.0)) < 1e-6


def test_calc_zero_entry_returns_zero():
    """Граничный случай: entry=0 (нет position_history) — не делим на ноль."""
    assert calc_close_pnl(0, 100, "short", 10, 1.0) == (0.0, 0.0)
    assert calc_close_pnl(100, 0, "short", 10, 1.0) == (0.0, 0.0)
    assert calc_close_pnl(100, 100, "short", 10, 0) == (0.0, 0.0)


def test_calc_no_change_zero_pnl():
    """Если exit == entry — PnL ровно 0."""
    pnl, pct = calc_close_pnl(2.0, 2.0, "short", 100, 1.0)
    assert pnl == 0.0
    assert pct == 0.0


# ── format_close_pnl ──────────────────────────────────────────────


def test_format_short_win():
    """+$X / +Y% для прибыли. Знак ПЕРЕД символом валюты."""
    s = format_close_pnl(2.20, 2.10, "short", 100, 0.50)
    assert s.startswith("`+$") and s.endswith("`")
    assert "+$2.27" in s
    assert "+455%" in s


def test_format_short_loss():
    """-$X / -Y% для убытка. Минус ПЕРЕД $, не внутри ($-X.XX было багом)."""
    s = format_close_pnl(2.10, 2.30, "short", 100, 0.50)
    assert "-$4.76" in s   # критично: минус снаружи
    assert "$-" not in s   # старый баг
    assert "-952%" in s


def test_format_empty_when_no_data():
    """Без данных — пустая строка, чтобы caller не клеил мусорный суффикс."""
    assert format_close_pnl(0, 100, "short", 100, 1.0) == ""
    assert format_close_pnl(100, 0, "short", 100, 1.0) == ""
    assert format_close_pnl(100, 100, "short", 100, 0) == ""


def test_format_long_win():
    """LONG win — тот же формат."""
    s = format_close_pnl(2.0, 2.2, "long", 50, 1.0)
    assert "+$5.00" in s
    assert "+500%" in s


# ── fmt_usd / fmt_pct (existing) ──────────────────────────────────


def test_fmt_usd_signs():
    # adaptive precision: 0 → 6 знаков (av < 0.001 ветка)
    assert fmt_usd(0) == "+$0.000000"
    assert fmt_usd(1.234).startswith("+$")
    assert fmt_usd(-1.234).startswith("-$")


def test_fmt_pct_signs():
    assert fmt_pct(0).startswith("+")
    assert fmt_pct(-5.0).startswith("-")

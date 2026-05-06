from types import SimpleNamespace

from bot.handlers import trading


def _cfg():
    return SimpleNamespace(
        default_trade_usdt=0.25,
        default_leverage=50,
        tp_pct=400,
        sl_pct=400,
        averaging_threshold=-200,
        averaging_amount=0.10,
        max_averaging_count=300,
        averaging_interval=4,
        averaging_profit_lock_trigger=150,
        averaging_profit_lock_sl_pct=100,
        auto_scan_capital_pct=90,
    )


def test_avg_text_has_numbered_items(monkeypatch):
    monkeypatch.setattr(trading, "_load_avg_dynamic_rules", lambda: [])

    text = trading._build_avg_text(_cfg(), free_balance=340.59, open_count=8)

    for n, label in [
        (1, "Маржа"), (2, "Плечо"), (3, "TP"), (4, "SL"),
        (5, "При PnL"), (6, "Сумма"), (7, "Макс"), (8, "Интервал"),
        (9, "Профит-локк"), (10, "Авто-капитал"),
        (11, "Ступень 1"), (12, "Ступень 2"),
        (13, "Ступень 3"), (14, "Ступень 4"),
    ]:
        assert f"{n}. {label}" in text
    assert "Напиши номер `1`-`14`" in text


def test_avg_pending_maps_numbers_to_questions(monkeypatch):
    monkeypatch.setattr(trading, "_load_avg_dynamic_rules", lambda: [])
    cfg = _cfg()

    simple = trading.avg_pending_for_number(7)
    assert simple["kind"] == "simple"
    assert simple["key"] == "maxavg"
    assert "7. Макс" in trading.avg_question_text(cfg, simple)

    lock = trading.avg_pending_for_number(9)
    assert lock["kind"] == "lock"
    assert "150 100" in trading.avg_question_text(cfg, lock)

    dyn = trading.avg_pending_for_number(14)
    assert dyn["kind"] == "dyn"
    assert dyn["index"] == 3
    assert "14. Ступень 4" in trading.avg_question_text(cfg, dyn)

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.services.exchange_errors import format_open_error
from bot.services.trading import format_manual_close_result

MOJIBAKE_MARKERS = ("???", "????", "Рџ", "Рќ", "Рћ", "СЃ", "СЂ", "вЂ")


@dataclass(frozen=True)
class SmokeConfig:
    chat_id: str
    endpoint_url: str
    test_update_token: str
    db_path: str = "/root/krabs/data/bot.db"
    timeout: int = 15


def current_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def read_telegram_token(db_path: str) -> str:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT value FROM config WHERE key='telegram_token'").fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"telegram_token not found in {db_path}")
    return str(row[0])


def assert_clean_text(text: str) -> None:
    bad = [marker for marker in MOJIBAKE_MARKERS if marker in text]
    if bad:
        raise AssertionError(f"smoke text contains mojibake markers: {bad}")


def build_smoke_text(head: str) -> str:
    open_text = format_open_error(
        RuntimeError(
            'TP1 place failed for HYPE/USDT:USDT: binanceusdm '
            '{"code":-2021,"msg":"Order would immediately trigger."}'
        ),
        symbol="HYPE/USDT:USDT",
        side="short",
        stage="TP1",
        entry_price=10,
        mark_price=9.8,
        trigger_price=10.1,
    )
    close_text = format_manual_close_result({
        "symbol": "HYPE/USDT:USDT",
        "side": "short",
        "pnl": 10.0,
        "margin": 20.0,
        "entry_price": 10.0,
        "exit_price": 9.5,
        "leverage": 10,
        "cycles_left": None,
        "close_reason": "manual",
    }, keep_reentry=False)
    text = "\n".join([
        f"LIVE SMOKE {head}",
        "Сделок не открывал, Binance не трогал.",
        "",
        open_text,
        "",
        close_text,
    ])
    assert_clean_text(text)
    return text


def build_test_update_request(config: SmokeConfig):
    payload = {
        "update_id": int(time.time()),
        "message": {
            "message_id": int(time.time()) % 1_000_000,
            "date": int(time.time()),
            "chat": {"id": int(config.chat_id), "type": "private", "first_name": "LiveSmoke"},
            "from": {
                "id": int(config.chat_id),
                "is_bot": False,
                "first_name": "LiveSmoke",
                "language_code": "ru",
            },
            "text": "/help",
            "entities": [{"offset": 0, "length": 5, "type": "bot_command"}],
        },
    }
    return urllib.request.Request(
        config.endpoint_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Telegram-Bot-Api-Secret-Token": config.test_update_token,
        },
        method="POST",
    )


def build_send_message_request(token: str, *, chat_id: str, text: str):
    assert_clean_text(text)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    return urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )


def json_request(request, *, timeout: int = 15) -> dict:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


def run_smoke(config: SmokeConfig) -> dict:
    token = read_telegram_token(config.db_path)
    endpoint_response = json_request(build_test_update_request(config), timeout=config.timeout)
    text = build_smoke_text(current_head())
    send_response = json_request(
        build_send_message_request(token, chat_id=config.chat_id, text=text),
        timeout=config.timeout,
    )
    return {
        "endpoint_ok": bool(endpoint_response.get("ok")),
        "endpoint_update_id": endpoint_response.get("update_id"),
        "send_message_ok": bool(send_response.get("ok")),
        "message_id": (send_response.get("result") or {}).get("message_id"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live smoke Telegram update endpoint and UTF-8 message rendering.")
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--endpoint-url", default="http://127.0.0.1:8787/telegram/update")
    parser.add_argument("--test-update-token", required=True)
    parser.add_argument("--db-path", default="/root/krabs/data/bot.db")
    parser.add_argument("--timeout", type=int, default=15)
    args = parser.parse_args(argv)

    result = run_smoke(SmokeConfig(
        chat_id=args.chat_id,
        endpoint_url=args.endpoint_url,
        test_update_token=args.test_update_token,
        db_path=args.db_path,
        timeout=args.timeout,
    ))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["endpoint_ok"] and result["send_message_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

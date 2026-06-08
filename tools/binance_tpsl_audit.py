"""Read-only Binance TP/SL audit for current bot credentials.

This script does not place, cancel, or close orders. It only reads positions and
open TP/SL orders, then prints a per-symbol comparison.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _parse_secrets_file(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    values: dict[str, str] = {}
    for key in ("exchange_provider", "binance_api_key", "binance_secret"):
        matches = []
        for line in text.splitlines():
            match = re.match(r"\s*" + re.escape(key) + r"\s*=\s*(\S+)\s*$", line)
            if match and "YOUR_" not in match.group(1):
                matches.append(match.group(1).strip().strip("`\"'"))
        if matches:
            values[key] = matches[-1]
    return values


def _list_candidates(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="ignore")
    for line_no, line in enumerate(text.splitlines(), 1):
        lower = line.lower()
        if not any(key in lower for key in ("binance_api_key", "binance_secret", "api_key", "secret")):
            continue
        for key in ("binance_api_key", "binance_secret", "api_key", "secret"):
            if key not in lower:
                continue
            match = re.search(r"\b" + re.escape(key) + r"\b\s*[=:]\s*(\S+)", line, re.IGNORECASE)
            if match:
                value = match.group(1).strip().strip("`\"'")
                print(f"{line_no}: {key} len={len(value)} placeholder={('YOUR_' in value or value.upper() == 'SET')}")
                break
        else:
            print(f"{line_no}: mention no_key_value")


def _load_config(secrets_file: Path | None) -> tuple[str, str, str, dict]:
    provider = os.environ.get("EXCHANGE_PROVIDER", "")
    key = os.environ.get("BINANCE_API_KEY", "")
    secret = os.environ.get("BINANCE_SECRET", "")
    settings = {"sl_pct": 500.0, "tp_ladder_pcts": "50,120,250"}

    if secrets_file:
        values = _parse_secrets_file(secrets_file)
        provider = provider or values.get("exchange_provider", "")
        key = key or values.get("binance_api_key", "")
        secret = secret or values.get("binance_secret", "")

    if not (key and secret):
        from bot import db as db_mod
        from bot.config import Config

        db_mod.init_db()
        cfg = Config.from_dict(db_mod.get_all_config())
        provider = provider or getattr(cfg, "exchange_provider", "")
        key = key or getattr(cfg, "binance_api_key", "")
        secret = secret or getattr(cfg, "binance_secret", "")
        settings = {
            "sl_pct": float(getattr(cfg, "sl_pct", 500.0)),
            "tp_ladder_pcts": str(getattr(cfg, "tp_ladder_pcts", "50,120,250")),
        }

    return provider or "binance_testnet", key, secret, settings


def _tp_sl_types(side: str) -> tuple[int, int]:
    return (1, 2) if side == "long" else (2, 1)


async def _audit(provider: str, key: str, secret: str, symbols: set[str], settings: dict) -> int:
    from bot.exchange.binance_client import BinanceClient
    from bot.services.tpsl import calc_sl_price

    client = BinanceClient(key, secret, testnet=(provider == "binance_testnet"))
    try:
        balance = await client.get_futures_balance()
        positions = await client.get_positions()
        orders = await client.get_tp_sl_orders()
    finally:
        await client.close()

    print(f"provider={provider}")
    print(
        "balance_usdt="
        f"free:{float(balance.get('free', {}).get('USDT', 0) or 0):.6f} "
        f"total:{float(balance.get('total', {}).get('USDT', 0) or 0):.6f}"
    )
    print(f"positions_count={len(positions)} tpsl_orders_count={len(orders)}")

    by_symbol: dict[str, list[dict]] = {}
    for order in orders:
        by_symbol.setdefault(str(order.get("symbol") or ""), []).append(order)

    seen: set[str] = set()
    for pos in positions:
        symbol = str(pos.get("symbol") or "")
        coin = symbol.split("/")[0]
        if symbols and coin not in symbols:
            continue
        seen.add(coin)
        side = str(pos.get("side") or "")
        tp_type, sl_type = _tp_sl_types(side)
        related = by_symbol.get(symbol, [])
        tps = sorted(
            {
                float(order.get("trigger_price") or 0)
                for order in related
                if int(order.get("trigger_type") or 0) == tp_type
                and float(order.get("trigger_price") or 0) > 0
            },
            reverse=(side == "short"),
        )
        sls = sorted(
            {
                float(order.get("trigger_price") or 0)
                for order in related
                if int(order.get("trigger_type") or 0) == sl_type
                and float(order.get("trigger_price") or 0) > 0
            }
        )
        computed_sl = calc_sl_price(
            float(pos.get("entry_price") or 0),
            int(pos.get("leverage") or 1),
            float(settings.get("sl_pct", 500.0)),
            side,
        )
        if len(tps) == 3 and len(sls) >= 1:
            verdict = "OK_3TP_1SL"
        elif not tps and not sls and computed_sl <= 0:
            verdict = "UNPROTECTED_INVALID_SL"
        elif not tps and not sls:
            verdict = "UNPROTECTED_NO_ORDERS"
        else:
            verdict = "MISMATCH"
        print(
            f"{coin} {side.upper()} {verdict} "
            f"entry={float(pos.get('entry_price') or 0):.8g} "
            f"mark={float(pos.get('mark_price') or 0):.8g} "
            f"margin={float(pos.get('margin') or 0):.2f} "
            f"pnl={float(pos.get('unrealized_pnl') or 0):+.6f}"
        )
        print(f"  TP_count={len(tps)} TP={','.join(f'{price:.8g}' for price in tps) or '-'}")
        print(f"  SL_count={len(sls)} SL={','.join(f'{price:.8g}' for price in sls) or '-'}")

    missing = symbols - seen if symbols else set()
    if missing:
        print("not_open=" + ",".join(sorted(missing)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Binance TP/SL audit.")
    parser.add_argument("--secrets-file", type=Path)
    parser.add_argument("--symbols", default="RENDER,HYPE,APT,HOME")
    parser.add_argument("--list-candidates", action="store_true")
    args = parser.parse_args(argv)

    if args.list_candidates:
        if not args.secrets_file:
            print("--list-candidates requires --secrets-file")
            return 2
        _list_candidates(args.secrets_file)
        return 0

    provider, key, secret, settings = _load_config(args.secrets_file)
    print(f"parsed_provider={provider}")
    print(f"binance_key_set={bool(key)} secret_set={bool(secret)}")
    if key:
        print(f"binance_key_len={len(key)} key_chars_ok={bool(re.fullmatch(r'[A-Za-z0-9_-]+', key))}")
    if not (key and secret):
        print("No Binance credentials found.")
        return 2

    symbols = {item.strip().upper() for item in args.symbols.split(",") if item.strip()}
    return asyncio.run(_audit(provider, key, secret, symbols, settings))


if __name__ == "__main__":
    raise SystemExit(main())

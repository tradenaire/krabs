from __future__ import annotations

import re


def _coin(raw: str) -> str:
    value = (raw or "").upper().strip()
    value = value.replace(":USDT", "")
    value = value.replace("/USDT", "")
    value = value.replace("_USDT", "")
    value = re.sub(r"[^A-Z0-9]", "", value)
    if value.endswith("USDT") and len(value) > 4:
        value = value[:-4]
    return value


async def resolve_signal_symbol(client, raw_symbol: str) -> str:
    coin = _coin(raw_symbol)
    if not coin:
        raise ValueError("Signal symbol is empty.")

    expected = client.futures_symbol(coin)

    exchange = getattr(client, "_exchange", None)
    if exchange is not None and hasattr(exchange, "load_markets"):
        markets = await exchange.load_markets()
        market_map = markets or getattr(exchange, "markets", {}) or {}
        if expected in market_map:
            return expected
        for symbol, market in market_map.items():
            market_id = str((market or {}).get("id") or "").upper()
            symbol_coin = _coin(str(symbol))
            if symbol_coin == coin or market_id == f"{coin}USDT":
                return symbol

    if hasattr(client, "get_contract_details"):
        details = await client.get_contract_details()
        for item in details:
            if item.get("state") not in (0, "0", None):
                continue
            if item.get("isHidden") is True:
                continue
            symbol = str(item.get("symbol") or "")
            if _coin(symbol) == coin:
                return symbol

    return expected

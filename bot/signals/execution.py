from __future__ import annotations

from bot.signals.model import ParsedSignal


async def execute_signal(client, app, signal: ParsedSignal, margin: float) -> dict:
    leverage = signal.leverage
    if not leverage and app is not None:
        config = app.bot_data.get("config")
        leverage = int(getattr(config, "default_leverage", 0) or 0) or None
    if not leverage:
        try:
            leverage = await client.get_max_leverage(signal.symbol)
        except Exception:
            leverage = 25

    if not hasattr(client, "set_multi_tp_sl"):
        raise RuntimeError("Multi-TP signal execution is supported only by the Binance client.")

    if app is not None:
        from bot.services.trading import execute_open

        open_result = await execute_open(
            client,
            app,
            signal.symbol,
            signal.order_side,
            margin,
            leverage=leverage,
            tp_pct=500,
            sl_pct=500,
        )
    else:
        open_result = await client.place_futures_order(signal.symbol, signal.order_side, margin, leverage)

    pos = await client.get_position(signal.symbol)
    if not pos:
        raise RuntimeError(f"No opened position found for {signal.symbol}.")

    orders = await client.set_multi_tp_sl(
        signal.symbol,
        signal.tps,
        signal.stop,
        pos_data=pos,
    )
    return {
        "symbol": client.futures_symbol(signal.symbol),
        "side": signal.side,
        "margin": margin,
        "leverage": leverage,
        "entry_price": pos.get("entry_price") or open_result.get("entry_price") or open_result.get("price"),
        "orders": len(orders),
        "order_id": open_result.get("order_id") or open_result.get("id"),
        "tps": signal.tps,
        "sl_price": signal.stop,
    }

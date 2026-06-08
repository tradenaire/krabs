from __future__ import annotations

from bot.signals.model import ParsedSignal
from bot.signals.symbols import resolve_signal_symbol


def _validate_exits_against_live_position(signal: ParsedSignal, pos: dict) -> None:
    side = signal.side
    entry = float(pos.get("entry_price") or 0)
    mark = float(pos.get("mark_price") or entry or 0)
    reference = mark or entry
    if reference <= 0:
        return

    for idx, tp in enumerate(signal.tps, 1):
        price = float(tp.price)
        if side == "short" and price >= reference:
            raise RuntimeError(
                f"TP{idx} сработал бы сразу: SHORT TP `{price:.8g}` должен быть ниже текущей цены `{reference:.8g}`."
            )
        if side == "long" and price <= reference:
            raise RuntimeError(
                f"TP{idx} сработал бы сразу: LONG TP `{price:.8g}` должен быть выше текущей цены `{reference:.8g}`."
            )

    stop = float(signal.stop)
    if side == "short" and stop <= reference:
        raise RuntimeError(
            f"SL сработал бы сразу: SHORT SL `{stop:.8g}` должен быть выше текущей цены `{reference:.8g}`."
        )
    if side == "long" and stop >= reference:
        raise RuntimeError(
            f"SL сработал бы сразу: LONG SL `{stop:.8g}` должен быть ниже текущей цены `{reference:.8g}`."
        )


async def execute_signal(client, app, signal: ParsedSignal, margin: float) -> dict:
    leverage = signal.leverage
    if not leverage and app is not None:
        config = app.bot_data.get("config")
        leverage = int(getattr(config, "default_leverage", 0) or 0) or None
    symbol = await resolve_signal_symbol(client, signal.symbol)

    if not leverage:
        try:
            leverage = await client.get_max_leverage(symbol)
        except Exception:
            leverage = 25

    if not hasattr(client, "set_multi_tp_sl"):
        raise RuntimeError("Multi-TP signal execution is supported only by the Binance client.")

    if app is not None:
        from bot.services.trading import execute_open

        open_result = await execute_open(
            client,
            app,
            symbol,
            signal.order_side,
            margin,
            leverage=leverage,
            tp_pct=500,
            sl_pct=500,
            setup_exits=False,
        )
    else:
        open_result = await client.place_futures_order(symbol, signal.order_side, margin, leverage)

    pos = await client.get_position(symbol)
    if not pos:
        raise RuntimeError(f"No opened position found for {symbol}.")

    try:
        _validate_exits_against_live_position(signal, pos)
        orders = await client.set_multi_tp_sl(
            symbol,
            signal.tps,
            signal.stop,
            pos_data=pos,
        )
    except Exception:
        try:
            await client.close_futures_position(symbol)
        except Exception:
            pass
        raise
    return {
        "symbol": symbol,
        "side": signal.side,
        "margin": margin,
        "leverage": leverage,
        "entry_price": pos.get("entry_price") or open_result.get("entry_price") or open_result.get("price"),
        "orders": len(orders),
        "order_id": open_result.get("order_id") or open_result.get("id"),
        "tps": signal.tps,
        "sl_price": signal.stop,
    }

"""Manual Binance Futures testnet smoke test (NOT part of the bot loop).

Opens a tiny short on BTC/USDT on the Binance USDM Futures *testnet*, verifies
positions/balance, sets TP/SL, partially closes, then fully closes. Use to
validate that real test orders reach the exchange.

Credentials are read from (in order):
  1. env BINANCE_API_KEY / BINANCE_SECRET
  2. the bot DB config (binance_api_key / binance_secret) if set via `python start.py --setup`

Get testnet keys at https://testnet.binancefuture.com (API Key management).

Run:
  .venv\\Scripts\\python.exe tools\\binance_smoke.py            # BTC/USDT, ~60 USDT margin, x5
  .venv\\Scripts\\python.exe tools\\binance_smoke.py ETH 50 3   # symbol margin leverage

Safe: testnet only. Exits early if no keys are configured.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_keys():
    key = os.environ.get("BINANCE_API_KEY", "")
    secret = os.environ.get("BINANCE_SECRET", "")
    if not (key and secret):
        try:
            from bot import db as db_mod
            db_mod.init_db()
            key = key or db_mod.get_config("binance_api_key", "")
            secret = secret or db_mod.get_config("binance_secret", "")
        except Exception:
            pass
    return key, secret


async def main():
    coin = sys.argv[1].upper() if len(sys.argv) > 1 else "BTC"
    margin = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    leverage = int(sys.argv[3]) if len(sys.argv) > 3 else 5

    key, secret = _load_keys()
    if not (key and secret):
        print("No Binance testnet keys (env BINANCE_API_KEY/BINANCE_SECRET or `start.py --setup`). Aborting.")
        return

    from bot.exchange.binance_client import BinanceClient
    client = BinanceClient(key, secret, testnet=True)
    sym = client.futures_symbol(coin)
    print(f"=== Binance Futures TESTNET smoke: {sym} margin=${margin} x{leverage} ===")
    try:
        bal = await client.get_futures_balance()
        print("balance USDT free/total:", bal["free"]["USDT"], "/", bal["total"]["USDT"])

        maxlev = await client.get_max_leverage(coin)
        print("max leverage:", maxlev)

        print("opening short...")
        order = await client.place_futures_order(coin, "sell", margin, leverage)
        print("order:", {k: order[k] for k in ("id", "side", "amount", "price", "leverage")})

        await asyncio.sleep(2)
        pos = await client.get_position(coin)
        if not pos:
            print("no position after open — abort"); return
        print("position:", {k: pos[k] for k in ("side", "contracts", "entry_price", "margin", "percentage", "leverage")})

        entry = pos["entry_price"]
        from bot.services.tpsl import calc_tp_price, calc_sl_price
        tp = calc_tp_price(entry, pos["leverage"], 500, pos["side"])
        sl = calc_sl_price(entry, pos["leverage"], 500, pos["side"])
        print(f"setting TP={tp:.2f} SL={sl:.2f} ...")
        res = await client.set_tp_sl(coin, tp_price=tp, sl_price=sl,
                                     pos_data={"side": pos["side"], "contracts": pos["contracts"],
                                               "margin_mode": pos["margin_mode"]})
        print("set_tp_sl:", res)
        tpsl = await client.get_tp_sl_orders(coin)
        print("active tp/sl orders:", tpsl)

        half = max(pos["contracts"] / 2, 0)
        if half > 0:
            print(f"partial close {half} ...")
            print("partial:", await client.partial_close_futures_position(coin, half))
            await asyncio.sleep(2)

        print("full close ...")
        # cancel leftover TP/SL then close
        await client.cancel_tp_sl_orders(coin)
        print("close:", await client.close_futures_position(coin))
        await asyncio.sleep(2)
        print("position after close:", await client.get_position(coin))
        print("=== smoke OK ===")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())

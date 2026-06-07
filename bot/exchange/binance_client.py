"""Binance USDM Futures exchange client with testnet support.

Implements the same public surface as the MEXC client (bot/exchange/client.py)
so engines/services/handlers work unchanged. Uses ccxt ``binanceusdm`` unified
methods plus ``set_sandbox_mode(True)`` so real test orders go to Binance's
futures testnet (testnet.binancefuture.com).

Key differences hidden by this adapter (see bot/exchange/base.py for the contract):
- Binance sizes orders in base-asset quantity, not integer contracts;
  ``contracts`` here = qty (float).
- TP/SL are separate conditional orders (TAKE_PROFIT_MARKET / STOP_MARKET with
  closePosition=true), which map naturally to the MEXC plan-order shape:
  ``get_tp_sl_orders`` returns real open orders with the MEXC-style trigger_type
  (long: TP=1/SL=2, short: TP=2/SL=1) so services/tpsl verification works.
"""
from __future__ import annotations

import asyncio
import functools
import logging

import ccxt.async_support as ccxt

from bot.event_logger import log_event

logger = logging.getLogger(__name__)

_TP_TYPES = ("take_profit_market", "take_profit")
_SL_TYPES = ("stop_market", "stop")


def _with_retry(tries: int = 3, base_delay: float = 0.8):
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            last_err: Exception | None = None
            for attempt in range(tries):
                try:
                    return await fn(*args, **kwargs)
                except (ccxt.NetworkError, ccxt.DDoSProtection, asyncio.TimeoutError) as e:
                    last_err = e
                except Exception as e:
                    msg = str(e).lower()
                    if "timeout" in msg or "getaddrinfo" in msg:
                        last_err = e
                    else:
                        raise
                if attempt < tries - 1:
                    await asyncio.sleep(base_delay * (2 ** attempt))
            raise last_err
        return wrapper
    return deco


def _f(v) -> float:
    try:
        if v in (None, "", "0", 0):
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class BinanceClient:
    def __init__(self, api_key: str, secret: str, testnet: bool = True):
        self.testnet = testnet
        self._exchange = ccxt.binanceusdm({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
        if testnet:
            self._exchange.set_sandbox_mode(True)
        # Binance keeps spot/futures wallets separate, but for the bot's purposes
        # the futures wallet is what matters; spot reuses the same instance.
        self._spot = self._exchange

    # ── symbol helpers ────────────────────────────────────────────────

    def futures_symbol(self, symbol: str) -> str:
        if ":USDT" in symbol:
            return symbol
        if "/" not in symbol:
            symbol = f"{symbol}/USDT"
        return f"{symbol}:USDT"

    @staticmethod
    def _normalize_side(side: str) -> str:
        if side in ("long", "buy"):
            return "buy"
        if side in ("short", "sell"):
            return "sell"
        raise ValueError(f"Invalid side: {side}")

    def _market_id(self, sym: str) -> str:
        try:
            return self._exchange.market(sym)["id"]
        except Exception:
            return sym.replace("/", "").replace(":USDT", "")

    async def close(self):
        await self._exchange.close()

    # ── balance ───────────────────────────────────────────────────────

    async def get_futures_balance(self) -> dict:
        bal = await self._exchange.fetch_balance()
        u = bal.get("USDT", {}) or {}
        free = _f(u.get("free"))
        total = _f(u.get("total"))
        used = _f(u.get("used"))
        # Binance exposes availableBalance in the raw info.
        avail = free
        try:
            info = bal.get("info", {})
            assets = info.get("assets") if isinstance(info, dict) else None
            if assets:
                for a in assets:
                    if a.get("asset") == "USDT":
                        avail = _f(a.get("availableBalance")) or free
                        break
        except Exception:
            pass
        return {
            "free": {"USDT": free},
            "total": {"USDT": total},
            "used": {"USDT": used},
            "USDT": {"free": free, "total": total, "used": used},
            "_raw": {
                "currency": "USDT",
                "equity": total,
                "availableBalance": avail,
                "availableOpen": avail,
                "cashBalance": total,
                "positionMargin": used,
                "frozenBalance": 0.0,
            },
        }

    async def get_spot_balance(self) -> dict:
        try:
            return await self._exchange.fetch_balance({"type": "spot"})
        except Exception:
            return {"free": {"USDT": 0.0}, "total": {"USDT": 0.0}}

    async def get_free_futures_balance(self) -> float:
        try:
            bal = await self.get_futures_balance()
            return float(bal["_raw"]["availableOpen"])
        except Exception:
            return 0.0

    async def transfer_usdt(self, amount: float, direction: str) -> None:
        # Binance spot<->futures transfer; on testnet this is generally a no-op.
        try:
            code = "USDT"
            if direction in ("s2f", "spot2fut"):
                await self._exchange.transfer(code, amount, "spot", "future")
            elif direction in ("f2s", "fut2spot"):
                await self._exchange.transfer(code, amount, "future", "spot")
        except Exception as e:
            logger.info("Binance transfer_usdt(%s, %s) skipped: %s", amount, direction, e)

    # ── market data ───────────────────────────────────────────────────

    async def get_ticker(self, symbol: str) -> dict:
        return await self._exchange.fetch_ticker(self.futures_symbol(symbol))

    async def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list:
        return await self._exchange.fetch_ohlcv(self.futures_symbol(symbol), timeframe, limit=limit)

    async def get_funding_rate(self, symbol: str) -> dict:
        try:
            sym = self.futures_symbol(symbol)
            data = await self._exchange.fetch_funding_rate(sym)
            rate = float(data.get("fundingRate", 0) or 0)
            next_ts = data.get("fundingDatetime") or data.get("nextFundingDatetime")
            return {"rate": rate, "next_funding_time": next_ts, "symbol": sym}
        except Exception as e:
            logger.debug("get_funding_rate %s: %s", symbol, e)
            return {"rate": 0.0, "next_funding_time": None, "symbol": symbol}

    async def get_contract_details(self) -> list:
        await self._exchange.load_markets()
        out = []
        for _sym, m in self._exchange.markets.items():
            if m.get("type") == "swap" and m.get("settle") == "USDT":
                out.append({
                    "symbol": m.get("id", ""),
                    "state": 0 if m.get("active", False) else 1,
                    "isHidden": not m.get("active", False),
                })
        return out

    async def get_max_leverage(self, symbol: str) -> int:
        try:
            await self._exchange.load_markets()
            m = self._exchange.market(self.futures_symbol(symbol))
            mx = (((m.get("limits") or {}).get("leverage") or {}).get("max"))
            return int(mx) if mx else 100
        except Exception:
            return 100

    async def get_min_order_usdt(self, symbol: str, leverage: int) -> float:
        try:
            await self._exchange.load_markets()
            sym = self.futures_symbol(symbol)
            m = self._exchange.market(sym)
            limits = m.get("limits") or {}
            min_cost = ((limits.get("cost") or {}).get("min"))
            if not min_cost:
                min_qty = ((limits.get("amount") or {}).get("min")) or 0
                ticker = await self._exchange.fetch_ticker(sym)
                price = _f(ticker.get("last"))
                min_cost = float(min_qty) * price if (min_qty and price) else 0.0
            return float(min_cost) / max(leverage, 1) if min_cost else 0.0
        except Exception as e:
            logger.debug("get_min_order_usdt %s: %s", symbol, e)
            return 0.0

    async def get_position_limit_usdt(self, symbol: str, leverage: int) -> float:
        try:
            await self._exchange.load_markets()
            sym = self.futures_symbol(symbol)
            m = self._exchange.market(sym)
            max_qty = (((m.get("limits") or {}).get("amount") or {}).get("max")) or 0
            ticker = await self._exchange.fetch_ticker(sym)
            price = _f(ticker.get("last"))
            if max_qty and price:
                return float(max_qty) * price / max(leverage, 1)
            return 0.0
        except Exception:
            return 0.0

    # ── positions ─────────────────────────────────────────────────────

    @staticmethod
    def _norm_position(p: dict) -> dict | None:
        contracts = abs(_f(p.get("contracts")))
        if contracts <= 0:
            return None
        side = p.get("side") or "long"
        entry = _f(p.get("entryPrice"))
        mark = _f(p.get("markPrice")) or entry
        liq = round(_f(p.get("liquidationPrice")), 6)
        pnl = _f(p.get("unrealizedPnl"))
        lev = int(_f(p.get("leverage")) or 1)
        margin = _f(p.get("initialMargin")) or _f(p.get("collateral"))
        if margin <= 0 and lev > 0 and entry > 0:
            margin = entry * contracts / lev
        pct = round((pnl / margin * 100), 2) if margin > 0 else 0.0
        mm = p.get("marginMode") or "cross"
        info = p.get("info") or {}
        return {
            "symbol": p.get("symbol"),
            "side": side,
            "contracts": contracts,
            "entry_price": entry,
            "mark_price": mark,
            "liquidation_price": liq,
            "unrealized_pnl": pnl,
            "margin": margin,
            "leverage": lev,
            "percentage": pct,
            "margin_mode": mm,
            "position_id": info.get("positionSide", "BOTH"),
        }

    async def get_positions(self) -> list[dict]:
        try:
            poss = await self._exchange.fetch_positions()
        except Exception as e:
            logger.warning("binance get_positions: %s", e)
            return []
        out = []
        for p in poss:
            np = self._norm_position(p)
            if np:
                out.append(np)
        return out

    async def get_position(self, symbol: str) -> dict | None:
        sym = self.futures_symbol(symbol)
        try:
            poss = await self._exchange.fetch_positions([sym])
        except Exception:
            poss = await self._exchange.fetch_positions()
        for p in poss:
            if p.get("symbol") == sym:
                np = self._norm_position(p)
                if np:
                    return np
        return None

    # ── orders ────────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int, side: str = "buy",
                           open_type: int = 2) -> dict:
        try:
            return await self._exchange.set_leverage(leverage, self.futures_symbol(symbol))
        except Exception as e:
            msg = str(e).lower()
            # -4046: no need to change leverage
            if "no need to change" in msg or "-4046" in msg:
                return {"info": "leverage unchanged"}
            logger.debug("binance set_leverage %s: %s", symbol, e)
            return {"error": str(e)}

    async def _qty_from_margin(self, sym: str, amount_usdt: float, leverage: int) -> tuple[float, float]:
        ticker = await self._exchange.fetch_ticker(sym)
        price = _f(ticker.get("last"))
        if price <= 0:
            raise RuntimeError(f"no price for {sym}")
        qty_raw = amount_usdt * leverage / price
        qty = float(self._exchange.amount_to_precision(sym, qty_raw))
        m = self._exchange.market(sym)
        min_qty = ((m.get("limits") or {}).get("amount") or {}).get("min") or 0
        if min_qty and qty < float(min_qty):
            qty = float(min_qty)
        return qty, price

    async def place_futures_order(self, symbol: str, side: str, amount_usdt: float,
                                  leverage: int, margin_mode: str | None = None) -> dict:
        sym = self.futures_symbol(symbol)
        await self._exchange.load_markets()
        order_side = self._normalize_side(side)
        await self.set_leverage(symbol, leverage)
        qty, price = await self._qty_from_margin(sym, amount_usdt, leverage)
        order = await self._exchange.create_order(
            sym, "market", order_side, qty, None, {"reduceOnly": False},
        )
        log_event("exchange", "binance_raw_response", operation="place_order", raw=order.get("info"))
        return {
            "id": order.get("id"),
            "symbol": sym,
            "side": order_side,
            "amount": qty,
            "price": _f(order.get("average")) or price,
            "leverage": leverage,
            "margin_mode": margin_mode or "cross",
            "info": order.get("info", {}),
        }

    async def partial_close_futures_position(self, symbol: str, contracts) -> dict:
        sym = self.futures_symbol(symbol)
        pos = await self.get_position(symbol)
        if not pos:
            raise ValueError(f"No position for {sym}")
        close_side = "sell" if pos["side"] == "long" else "buy"
        await self._exchange.load_markets()
        qty = float(self._exchange.amount_to_precision(sym, float(contracts)))
        order = await self._exchange.create_order(
            sym, "market", close_side, qty, None, {"reduceOnly": True},
        )
        return {"id": order.get("id"), "symbol": sym, "contracts_closed": qty,
                "info": order.get("info", {})}

    async def close_futures_position(self, symbol: str) -> dict:
        sym = self.futures_symbol(symbol)
        pos = await self.get_position(symbol)
        if not pos:
            return {"id": None, "symbol": sym, "status": "closed", "info": {}}
        close_side = "sell" if pos["side"] == "long" else "buy"
        await self._exchange.load_markets()
        qty = float(self._exchange.amount_to_precision(sym, pos["contracts"]))
        order = await self._exchange.create_order(
            sym, "market", close_side, qty, None, {"reduceOnly": True},
        )
        return {"id": order.get("id"), "symbol": sym, "status": "closed",
                "info": order.get("info", {})}

    # ── TP/SL (conditional close orders) ──────────────────────────────

    async def set_tp_sl(self, symbol: str, tp_price: float | None = None,
                        sl_price: float | None = None,
                        pos_data: dict | None = None,
                        sl_limit_price: float | None = None) -> list[dict]:
        sym = self.futures_symbol(symbol)
        await self._exchange.load_markets()
        log_event("decisions", "set_tp_sl_start", symbol=sym,
                  tp_price=tp_price, sl_price=sl_price, pos_data=pos_data)

        if pos_data:
            side = pos_data["side"]
        else:
            pos = await self.get_position(symbol)
            if not pos:
                raise ValueError(f"No position for {sym}")
            side = pos["side"]
        close_side = "sell" if side == "long" else "buy"

        # Replace existing TP/SL for this symbol.
        await self.cancel_tp_sl_orders(symbol)

        results = []

        async def _place(kind: str, price: float, order_type: str):
            stop = float(self._exchange.price_to_precision(sym, price))
            params = {"stopPrice": stop, "closePosition": True,
                      "workingType": "MARK_PRICE"}
            try:
                r = await self._exchange.create_order(sym, order_type, close_side, None, None, params)
                log_event("exchange", "binance_raw_response",
                          operation=f"set_tp_sl_{kind.lower()}", raw=r.get("info"))
                results.append({"type": kind, "price": price, "result": {"success": True}})
            except Exception as e:
                results.append({"type": kind, "price": price,
                                "result": {"success": False, "message": str(e)}, "error": str(e)})
                raise RuntimeError(f"{kind} place failed for {sym}: {e}")

        if tp_price:
            await _place("TP", tp_price, "TAKE_PROFIT_MARKET")
        if sl_price:
            await _place("SL", sl_price, "STOP_MARKET")
        if not results:
            results.append({"type": "skip", "result": {"success": True}})
        log_event("decisions", "set_tp_sl_result", symbol=sym, results=results)
        return results

    @staticmethod
    def _order_kind(o: dict) -> str | None:
        t = str(o.get("type") or (o.get("info") or {}).get("type") or "").lower()
        if any(x in t for x in _TP_TYPES):
            return "TP"
        if any(x in t for x in _SL_TYPES):
            return "SL"
        return None

    async def _open_tpsl_orders(self, symbol: str | None):
        if symbol:
            return await self._exchange.fetch_open_orders(self.futures_symbol(symbol))
        return await self._exchange.fetch_open_orders()

    async def get_tp_sl_orders(self, symbol: str | None = None) -> list[dict]:
        try:
            orders = await self._open_tpsl_orders(symbol)
        except Exception as e:
            logger.debug("binance get_tp_sl_orders: %s", e)
            return []
        out = []
        for o in orders:
            kind = self._order_kind(o)
            if not kind:
                continue
            stop = _f(o.get("stopPrice")) or _f((o.get("info") or {}).get("stopPrice"))
            if stop <= 0:
                continue
            o_side = (o.get("side") or "").lower()
            # closePosition order side is opposite the position: sell -> long pos.
            pos_side = "long" if o_side == "sell" else "short"
            close_side = 4 if pos_side == "long" else 2
            is_tp = kind == "TP"
            if pos_side == "long":
                trigger_type = 1 if is_tp else 2
            else:
                trigger_type = 2 if is_tp else 1
            out.append({
                "id": o.get("id"),
                "symbol": o.get("symbol"),
                "trigger_price": stop,
                "side": close_side,
                "trigger_type": trigger_type,
                "state": 1,
            })
        return out

    async def cancel_tp_sl_orders(self, symbol: str) -> int:
        sym = self.futures_symbol(symbol)
        try:
            orders = await self._exchange.fetch_open_orders(sym)
        except Exception:
            return 0
        targets = [o for o in orders if self._order_kind(o)]
        for o in targets:
            try:
                await self._exchange.cancel_order(o.get("id"), sym)
            except Exception as e:
                logger.debug("binance cancel tp/sl %s: %s", o.get("id"), e)
        return len(targets)

    async def cancel_plan_orders(self, symbol: str) -> None:
        sym = self.futures_symbol(symbol)
        try:
            await self._exchange.cancel_all_orders(sym)
        except Exception as e:
            logger.debug("binance cancel_plan_orders %s: %s", sym, e)

    async def get_limit_close_orders(self, symbol: str) -> list[dict]:
        # Not consumed by engines/services; return empty for parity.
        return []

    @_with_retry()
    async def was_closed_by_tp(self, symbol: str, pos_side: str,
                               opened_at_ms: int | None = None):
        """Infer whether the position was closed by TP or SL from order history.
        Returns (is_tp: bool|None, trigger_price: float|None)."""
        sym = self.futures_symbol(symbol)
        try:
            orders = await self._exchange.fetch_closed_orders(sym, opened_at_ms, 30)
        except Exception as e:
            logger.debug("binance was_closed_by_tp %s: %s", sym, e)
            return None, None
        for o in sorted(orders, key=lambda x: x.get("timestamp") or 0, reverse=True):
            status = str(o.get("status") or "").lower()
            if status not in ("closed", "filled"):
                continue
            kind = self._order_kind(o)
            if kind is None:
                continue
            trig = _f(o.get("stopPrice")) or _f((o.get("info") or {}).get("stopPrice"))
            return (kind == "TP"), (trig or None)
        return None, None

import asyncio
import functools
import logging
import time
import uuid
import aiohttp
import ccxt.async_support as ccxt

logger = logging.getLogger(__name__)


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
                    delay = base_delay * (2 ** attempt)
                    logger.warning("%s transient error (try %d/%d): %s — retry in %.1fs",
                                   fn.__name__, attempt + 1, tries, last_err, delay)
                    await asyncio.sleep(delay)
            raise last_err
        return wrapper
    return deco


class _MexcThreadedDNS(ccxt.mexc):
    """MEXC exchange with ThreadedResolver to avoid aiodns DNS failures on Windows.
    Session is created lazily inside the running async loop (not in sync context).
    """
    def __init__(self, config=None):
        self._session = None  # must exist before parent __init__ calls self.session
        super().__init__(config or {})

    @property
    def session(self):
        if self._session is None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return None  # sync context, defer creation
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), ssl=True)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    @session.setter
    def session(self, value):
        self._session = value


class ExchangeClient:
    def __init__(self, api_key: str, secret: str):
        self._exchange = _MexcThreadedDNS({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        # Prevent load_markets() from calling spotPrivateGetCapitalConfigGetall
        # (spot private auth endpoint that fails when only futures key is configured).
        async def _no_currencies(*a, **kw):
            return {}
        self._exchange.fetch_currencies = _no_currencies

        self._spot = _MexcThreadedDNS({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })
        self._mark_ticker_cache: dict[str, tuple[float, float]] = {}
        self._mark_ticker_errts: dict[str, float] = {}

    def futures_symbol(self, symbol: str) -> str:
        if ":USDT" in symbol:
            return symbol
        if "/" not in symbol:
            symbol = f"{symbol}/USDT"
        return f"{symbol}:USDT"

    @staticmethod
    def _mexc_contract_symbol(market: dict, fallback_sym: str) -> str:
        return market.get("id", fallback_sym.replace("/", "_").replace(":USDT", ""))

    @staticmethod
    def _normalize_side(side: str) -> str:
        if side in ("long", "buy"):
            return "buy"
        if side in ("short", "sell"):
            return "sell"
        raise ValueError(f"Invalid side: {side}")

    async def close(self):
        await self._exchange.close()
        await self._spot.close()

    # ── Balance ──────────────────────────────────────────────────────

    async def get_futures_balance(self) -> dict:
        """Return futures balance using contract API directly (avoids spot auth)."""
        raw = await self._exchange.contractPrivateGetAccountAssets()
        assets = raw.get("data") or []
        usdt = next((a for a in assets if a.get("currency") == "USDT"), {})
        free = float(usdt.get("availableBalance", 0) or 0)
        total = float(usdt.get("equity", 0) or 0)
        margin = float(usdt.get("positionMargin", 0) or 0)
        frozen = float(usdt.get("frozenBalance", 0) or 0)
        return {
            "free": {"USDT": free},
            "total": {"USDT": total},
            "used": {"USDT": margin + frozen},
            "USDT": {"free": free, "total": total, "used": margin + frozen},
            "_raw": usdt,
        }

    async def get_spot_balance(self) -> dict:
        return await self._spot.fetch_balance()

    async def transfer_usdt(self, amount: float, direction: str) -> None:
        """Transfer USDT between spot and futures. direction: 's2f' or 'f2s'."""
        if direction in ("s2f", "spot2fut"):
            await self._exchange.transfer("USDT", amount, "spot", "swap")
        elif direction in ("f2s", "fut2spot"):
            await self._exchange.transfer("USDT", amount, "swap", "spot")
        else:
            raise ValueError(f"Unknown transfer direction: {direction}")

    # ── Market data ──────────────────────────────────────────────────

    async def get_ticker(self, symbol: str) -> dict:
        return await self._exchange.fetch_ticker(symbol)

    async def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list:
        return await self._exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

    # ── Positions ────────────────────────────────────────────────────

    async def get_positions(self) -> list[dict]:
        """Fetch open futures positions via MEXC contract API directly.

        Uses contractPrivateGetPositionOpenPositions instead of CCXT's
        fetch_positions() — the latter internally calls the spot endpoint
        capital/config/getall which triggers ExchangeNotAvailable when
        the API key doesn't have spot permissions.
        """
        try:
            raw = await self._exchange.contractPrivateGetPositionOpenPositions()
        except Exception as e:
            logger.error("contractPrivateGetPositionOpenPositions failed: %s", e)
            raise

        positions = raw.get("data") or []
        if not isinstance(positions, list):
            positions = []

        _now_ts = time.time()
        result = []

        # Need markets loaded to convert MEXC symbol (BTC_USDT) → ccxt symbol (BTC/USDT:USDT)
        try:
            await self._exchange.load_markets()
            markets_by_id = {m.get("id"): sym for sym, m in self._exchange.markets.items()}
        except Exception:
            markets_by_id = {}

        for p in positions:
            vol = float(p.get("holdVol", 0) or 0)
            if vol == 0:
                continue

            logger.debug("RAW pos fields: %s", {k: p[k] for k in (
                "symbol", "holdVol", "holdAvgPrice", "leverage", "im",
                "unrealisedPnl", "markPrice", "liquidatePrice",
                "positionType", "marginType", "contractSize",
            ) if k in p})

            mexc_sym = p.get("symbol", "")  # e.g. "BTC_USDT"
            ccxt_sym = markets_by_id.get(mexc_sym, f"{mexc_sym.replace('_', '/')}/USDT:USDT" if "_" in mexc_sym else mexc_sym)

            # side: 1=long, 2=short
            side_raw = int(p.get("positionType", 1) or 1)
            side = "long" if side_raw == 1 else "short"

            entry = float(p.get("holdAvgPrice", 0) or 0)
            lev = int(p.get("leverage", 1) or 1)
            margin = float(p.get("im", 0) or p.get("margin", 0) or 0)
            raw_liq = float(p.get("liquidatePrice", 0) or 0)
            contracts = vol

            # Mark price with cache
            mark = float(p.get("markPrice", 0) or 0)
            if mark == 0 and ccxt_sym:
                cached = self._mark_ticker_cache.get(ccxt_sym)
                if cached and cached[1] > _now_ts:
                    mark = cached[0]
                elif self._mark_ticker_errts.get(ccxt_sym, 0) <= _now_ts:
                    try:
                        ticker = await self.get_ticker(ccxt_sym)
                        mark = float(ticker["last"])
                        self._mark_ticker_cache[ccxt_sym] = (mark, _now_ts + 60)
                    except Exception as e:
                        self._mark_ticker_errts[ccxt_sym] = _now_ts + 30
                        logger.warning("mark price for %s: %s", ccxt_sym, e)

            # Contract size from market data (raw pos doesn't include it)
            try:
                market = self._exchange.markets.get(ccxt_sym, {})
                contract_size = float(market.get("contractSize") or 0)
            except Exception:
                contract_size = 0
            if not contract_size:
                contract_size = float(p.get("contractSize", 0.0001) or 0.0001)

            pos_size = contracts * contract_size  # in base currency

            # PnL via mark price (unrealisedPnl not in MEXC position response)
            if mark > 0 and entry > 0:
                pnl = (mark - entry) * pos_size if side == "long" else (entry - mark) * pos_size
            else:
                pnl = 0.0

            # Margin: use im from MEXC; recalculate if missing
            if margin == 0 and entry > 0 and lev > 0:
                margin = entry * pos_size / lev

            pct = (pnl / margin * 100) if margin > 0 else 0.0

            open_type = int(p.get("marginType", 2) or 2)
            margin_mode = "isolated" if open_type == 1 else "cross"

            result.append({
                "symbol": ccxt_sym,
                "side": side,
                "contracts": contracts,
                "entry_price": entry,
                "mark_price": mark,
                "liquidation_price": round(raw_liq, 4),
                "unrealized_pnl": round(pnl, 6),
                "margin": margin,
                "leverage": lev,
                "percentage": round(pct, 2),
                "margin_mode": margin_mode,
                "position_id": p.get("positionId"),
                "hold_fee": float(p.get("holdFee", 0) or 0),
                "hold_avg_price": entry,
            })

        return result

    async def get_position(self, symbol: str) -> dict | None:
        sym = self.futures_symbol(symbol)
        for p in await self.get_positions():
            if p["symbol"] == sym:
                return p
        return None

    # ── Futures orders ────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int, side: str = "buy",
                           open_type: int = 2) -> dict:
        sym = self.futures_symbol(symbol)
        side = self._normalize_side(side)
        pos_type = 1 if side == "buy" else 2
        result = await self._exchange.set_leverage(leverage, sym, params={
            "openType": open_type,
            "positionType": pos_type,
        })
        if isinstance(result, dict):
            info = result.get("info") if "info" in result else result
            if isinstance(info, dict) and info.get("success") is False:
                raise RuntimeError(f"MEXC set_leverage rejected: {info.get('message', info)}")
        return result

    async def place_futures_order(self, symbol: str, side: str, amount_usdt: float,
                                  leverage: int, margin_mode: str | None = None) -> dict:
        side = self._normalize_side(side)
        sym = self.futures_symbol(symbol)

        ticker = await self.get_ticker(sym)
        price = float(ticker["last"])

        await self._exchange.load_markets()
        market = self._exchange.market(sym)
        contract_size = float(market.get("contractSize", 0.0001))

        max_lev = int(market.get("limits", {}).get("leverage", {}).get("max", 200) or 200)
        if leverage > max_lev:
            logger.warning("Leverage %dx > max %dx for %s, clamping", leverage, max_lev, sym)
            leverage = max_lev

        amount_base = (amount_usdt * leverage) / price
        contracts = max(1, round(amount_base / contract_size))

        logger.info("Futures order: %s %s contracts=%d lev=%dx margin=$%.2f",
                    side.upper(), sym, contracts, leverage, amount_usdt)

        # Auto-transfer from spot if needed
        try:
            fut_bal = await self.get_futures_balance()
            free = float(fut_bal.get("free", {}).get("USDT", 0) or 0)
            needed = amount_usdt * 1.1
            if free < needed:
                shortfall = needed - free + 0.5
                logger.info("Auto-transfer $%.2f spot→futures", shortfall)
                await self._exchange.transfer("USDT", shortfall, "spot", "swap")
        except Exception as e:
            logger.warning("Auto-transfer failed: %s", e)

        if margin_mode is None:
            try:
                existing = await self.get_position(symbol)
                margin_mode = existing.get("margin_mode") or "cross" if existing else "cross"
            except Exception:
                margin_mode = "cross"
        if margin_mode not in ("cross", "isolated"):
            margin_mode = "cross"
        open_type = 1 if margin_mode == "isolated" else 2

        try:
            await self.set_leverage(symbol, leverage, side, open_type=open_type)
        except Exception as e:
            logger.warning("set_leverage: %s", e)

        mexc_side = 1 if side == "buy" else 3
        mexc_symbol = self._mexc_contract_symbol(market, sym)

        result = await self._exchange.contractPrivatePostOrderSubmit({
            "symbol": mexc_symbol,
            "price": 0,
            "vol": contracts,
            "side": mexc_side,
            "type": 5,
            "openType": open_type,
            "leverage": leverage,
        })

        if not result.get("success", False):
            raise RuntimeError(f"MEXC order rejected: {result.get('message', result)}")

        order_id = str(result.get("data", ""))
        logger.info("MEXC futures order placed: %s (id=%s)", mexc_symbol, order_id)
        return {"id": order_id, "symbol": sym, "side": side,
                "amount": contracts, "price": price, "leverage": leverage,
                "margin_mode": margin_mode, "info": result}

    @_with_retry(tries=2, base_delay=1.0)
    async def close_futures_position(self, symbol: str) -> dict:
        sym = self.futures_symbol(symbol)
        pos = await self.get_position(symbol)
        if not pos:
            raise ValueError(f"No open position for {sym}")

        side = pos["side"]
        contracts = int(round(pos["contracts"]))
        mexc_side = 4 if side == "long" else 2
        margin_mode = pos.get("margin_mode") or "cross"
        open_type = 1 if margin_mode == "isolated" else 2

        await self._exchange.load_markets()
        market = self._exchange.market(sym)
        mexc_symbol = self._mexc_contract_symbol(market, sym)

        params = {
            "symbol": mexc_symbol,
            "price": 0,
            "vol": contracts,
            "side": mexc_side,
            "type": 5,
            "openType": open_type,
        }
        pos_id = pos.get("position_id")
        if pos_id:
            params["positionId"] = pos_id

        result = await self._exchange.contractPrivatePostOrderSubmit(params)
        if not result.get("success", False):
            raise RuntimeError(f"MEXC close failed: {result.get('message', result)}")

        order_id = str(result.get("data", ""))
        logger.info("Position closed: %s %s (%d contracts)", sym, side, contracts)
        return {"id": order_id, "symbol": sym, "status": "closed", "info": result}

    # ── TP/SL ────────────────────────────────────────────────────────

    async def set_tp_sl(self, symbol: str, tp_price: float | None = None,
                        sl_price: float | None = None,
                        pos_data: dict | None = None) -> list[dict]:
        sym = self.futures_symbol(symbol)

        if pos_data:
            side = pos_data["side"]
            contracts = int(round(pos_data["contracts"]))
            margin_mode = pos_data.get("margin_mode", "isolated")
        else:
            pos = await self.get_position(symbol)
            if not pos:
                raise ValueError(f"No position for {sym}")
            side = pos["side"]
            contracts = int(round(pos["contracts"]))
            margin_mode = pos.get("margin_mode", "isolated")

        open_type = 2 if margin_mode == "cross" else 1
        await self._exchange.load_markets()
        market = self._exchange.market(sym)
        mexc_sym = self._mexc_contract_symbol(market, sym)
        close_side = 4 if side == "long" else 2

        # Idempotency check
        try:
            existing = await self.get_tp_sl_orders(symbol)
        except Exception:
            existing = []
        if existing:
            tp_type, sl_type = (1, 2) if side == "long" else (2, 1)
            existing_tp = {round(float(t.get("trigger_price", 0)), 6)
                           for t in existing if t.get("trigger_type") == tp_type}
            existing_sl = {round(float(t.get("trigger_price", 0)), 6)
                           for t in existing if t.get("trigger_type") == sl_type}
            want_tp = None if tp_price is None else {round(tp_price, 6)}
            want_sl = None if sl_price is None else {round(sl_price, 6)}
            if (want_tp is None or existing_tp == want_tp) and \
               (want_sl is None or existing_sl == want_sl):
                logger.info("set_tp_sl(%s): triggers exact match, skipping", sym)
                return [{"type": "skip", "result": {"success": True}}]

        # Cancel all then re-place
        try:
            await self._exchange.contractPrivatePostPlanorderCancelAll({"symbol": mexc_sym})
            for _ in range(8):
                await asyncio.sleep(0.25)
                try:
                    still = await self.get_tp_sl_orders(symbol)
                except Exception:
                    still = []
                if not still:
                    break
        except Exception as e:
            logger.warning("cancelAll for %s: %s", mexc_sym, e)

        results = []

        async def _place(kind: str, price: float, trigger_type: int) -> dict:
            last_err: Exception | None = None
            for attempt in range(3):
                try:
                    r = await self._exchange.contractPrivatePostPlanorderPlace({
                        "symbol": mexc_sym, "price": 0, "vol": contracts,
                        "side": close_side, "orderType": 5, "openType": open_type,
                        "triggerPrice": str(price), "triggerType": trigger_type,
                        "trend": 1, "executeCycle": 2,
                    })
                    return {"type": kind, "price": price, "result": r}
                except Exception as e:
                    last_err = e
                    msg = str(e).lower()
                    if not any(x in msg for x in ("510", "too frequent", "timeout", "network")):
                        break
                    logger.warning("%s place attempt %d/3 for %s: %s", kind, attempt+1, sym, e)
                    await asyncio.sleep(0.8 * (attempt + 1))
            logger.error("%s place FAILED for %s: %s", kind, sym, last_err)
            return {"type": kind, "price": price, "error": str(last_err)}

        if tp_price:
            tt = 1 if side == "long" else 2
            results.append(await _place("TP", tp_price, tt))

        if sl_price:
            tt = 2 if side == "long" else 1
            results.append(await _place("SL", sl_price, tt))

        return results

    async def get_limit_close_orders(self, symbol: str) -> list[dict]:
        """Return open limit close orders (TP limit orders) for a symbol."""
        sym = self.futures_symbol(symbol)
        await self._exchange.load_markets()
        market = self._exchange.market(sym)
        mexc_sym = self._mexc_contract_symbol(market, sym)
        close_sides = {2, 4}
        try:
            result = await self._exchange.contractPrivateGetOrderListOpenOrdersSymbol(
                {"symbol": mexc_sym}
            )
            orders = result.get("data") or []
            return [
                o for o in orders
                if int(o.get("side", 0) or 0) in close_sides
                and int(o.get("type", 0) or 0) == 1
            ]
        except Exception as e:
            logger.warning("get_limit_close_orders(%s): %s", symbol, e)
            return []

    async def _cancel_limit_close_orders(self, symbol: str, mexc_sym: str) -> int:
        """Cancel open regular limit close orders for a symbol (used as TP limit orders)."""
        close_sides = {2, 4}
        try:
            result = await self._exchange.contractPrivateGetOrderListOpenOrdersSymbol(
                {"symbol": mexc_sym}
            )
            orders = result.get("data") or []
            cancelled = 0
            for o in orders:
                side_val = int(o.get("side", 0) or 0)
                order_type = int(o.get("type", 0) or 0)
                if side_val not in close_sides or order_type != 1:
                    continue
                order_id = o.get("orderId") or o.get("id")
                if not order_id:
                    continue
                try:
                    await self._exchange.contractPrivatePostOrderCancel({"orderId": str(order_id)})
                    cancelled += 1
                    logger.info("Cancelled TP limit order %s for %s", order_id, symbol)
                except Exception as e:
                    logger.warning("cancel TP limit %s for %s: %s", order_id, symbol, e)
            return cancelled
        except Exception as e:
            logger.warning("_cancel_limit_close_orders(%s): %s", symbol, e)
            return 0

    async def was_closed_by_tp(self, symbol: str, pos_side: str,
                               opened_at_ms: int | None = None) -> tuple[bool | None, float | None]:
        """Check recent executed plan orders to determine if position closed by TP.
        Returns (is_tp, trigger_price): True/False/None, and the price that fired."""
        try:
            sym = self.futures_symbol(symbol)
            await self._exchange.load_markets()
            market = self._exchange.market(sym)
            mexc_sym = self._mexc_contract_symbol(market, sym)
            result = await self._exchange.contractPrivateGetPlanorderListOrders(
                {"symbol": mexc_sym, "page_size": 10, "page_num": 1}
            )
            data = result.get("data") or {}
            orders = (data.get("resultList") or data.get("result_list") or []) \
                if isinstance(data, dict) else (data or [])
            executed = [
                o for o in orders
                if int(o.get("state", 0) or 0) == 3
                and (opened_at_ms is None
                     or int(o.get("createTime", 0) or 0) >= opened_at_ms)
            ]
            if not executed:
                return None, None
            latest = max(executed, key=lambda o: int(o.get("createTime", 0) or 0))
            trigger_type = int(latest.get("triggerType", 0) or 0)
            trigger_price = float(latest.get("triggerPrice", 0) or 0) or None
            # For short: TP=triggerType 2 (price ≤), SL=triggerType 1 (price ≥)
            # For long:  TP=triggerType 1 (price ≥), SL=triggerType 2 (price ≤)
            tp_type = 1 if pos_side == "long" else 2
            return trigger_type == tp_type, trigger_price
        except Exception as e:
            logger.warning("was_closed_by_tp(%s): %s", symbol, e)
            return None, None

    @_with_retry()
    async def get_tp_sl_orders(self, symbol: str | None = None) -> list[dict]:
        # state=1 → not yet triggered (active). No states filter = all statuses.
        # We fetch without state filter and return only active (state==1) orders.
        base_params = {"page_size": 100, "page_num": 1}
        if symbol:
            sym = self.futures_symbol(symbol)
            await self._exchange.load_markets()
            market = self._exchange.market(sym)
            base_params["symbol"] = self._mexc_contract_symbol(market, sym)

        parsed: list[dict] = []
        page_num = 1
        while True:
            params = dict(base_params)
            params["page_num"] = page_num
            result = await self._exchange.contractPrivateGetPlanorderListOrders(params)
            data = result.get("data")
            orders = (data.get("resultList") or data.get("result_list") or []) \
                if isinstance(data, dict) else (data or [])
            if not orders:
                break
            for o in orders:
                state = o.get("state")
                if state not in (1, "1"):  # only active (not-yet-triggered) orders
                    continue
                parsed.append({
                    "id": o.get("id"),
                    "symbol": o.get("symbol", ""),
                    "trigger_price": float(o.get("triggerPrice", 0) or 0),
                    "side": int(o.get("side", 0) or 0),
                    "trigger_type": int(o.get("triggerType", 0) or 0),
                    "state": state,
                })
            if len(orders) < 100:
                break
            page_num += 1
            if page_num > 20:
                break
        return parsed

    async def cancel_tp_sl_orders(self, symbol: str) -> int:
        """Cancel all active plan (TP/SL trigger) orders for a symbol. Returns count cancelled."""
        sym = self.futures_symbol(symbol)
        try:
            await self._exchange.load_markets()
            market = self._exchange.market(sym)
            mexc_sym = self._mexc_contract_symbol(market, sym)
        except Exception:
            mexc_sym = sym.replace("/", "_").replace(":USDT", "")

        before = await self.get_tp_sl_orders()
        before_count = sum(1 for o in before if o.get("symbol", "") == mexc_sym)

        try:
            await self._exchange.contractPrivatePostPlanorderCancelAll({"symbol": mexc_sym})
            logger.info("cancel_tp_sl_orders %s: CancelAll sent (had %d orders)", symbol, before_count)
        except Exception as e:
            logger.warning("cancel_tp_sl_orders %s: CancelAll failed: %s", symbol, e)
            return 0

        return before_count

    # ── Helpers ───────────────────────────────────────────────────────

    async def get_contract_details(self) -> list:
        try:
            result = await self._exchange.contractPublicGetDetail()
            return result.get("data", [])
        except Exception as e:
            logger.error("Contract details: %s", e)
            return []

    async def get_max_leverage(self, symbol: str) -> int:
        sym = self.futures_symbol(symbol)
        try:
            await self._exchange.load_markets()
            m = self._exchange.market(sym)
            lev = int(m.get("limits", {}).get("leverage", {}).get("max", 0) or 0)
            return lev if lev > 0 else 100
        except Exception:
            return 100

    async def get_min_order_usdt(self, symbol: str, leverage: int) -> float:
        """Return actual USDT margin for 1 contract at given leverage."""
        sym = self.futures_symbol(symbol)
        try:
            await self._exchange.load_markets()
            market = self._exchange.market(sym)
            contract_size = float(market.get("contractSize", 0.0001))
            ticker = await self.get_ticker(sym)
            price = float(ticker["last"])
            return contract_size * price / leverage
        except Exception:
            return 0.0

    async def get_position_limit_usdt(self, symbol: str, leverage: int) -> float:
        """Max position size in USDT at given leverage from MEXC risk limit tiers."""
        sym = self.futures_symbol(symbol)
        mexc_sym = sym.split("/")[0] + "_USDT"
        try:
            result = await self._exchange.contractPublicGetRiskLimitSymbol({"symbol": mexc_sym})
            tiers = result.get("data") or []
            # Find tightest tier where maxLeverage >= our leverage
            applicable = [t for t in tiers if int(t.get("maxLeverage", 0)) >= leverage]
            if not applicable:
                return 0.0
            tier = min(applicable, key=lambda t: int(t.get("maxLeverage", 999)))
            max_vol = int(tier.get("maxVol", 0))
            await self._exchange.load_markets()
            market = self._exchange.market(sym)
            contract_size = float(market.get("contractSize", 0.0001))
            ticker = await self.get_ticker(sym)
            price = float(ticker["last"])
            return max_vol * contract_size * price / leverage
        except Exception:
            return 0.0

    async def get_free_futures_balance(self) -> float:
        try:
            bal = await self.get_futures_balance()
            return float(bal["free"]["USDT"])
        except Exception:
            return 0.0

    async def get_funding_rate(self, symbol: str) -> dict:
        """Return current funding rate for symbol.
        rate > 0: longs pay shorts (good for short)
        rate < 0: shorts pay longs (costs us money)
        """
        try:
            sym = self.futures_symbol(symbol)
            data = await self._exchange.fetch_funding_rate(sym)
            rate = float(data.get("fundingRate", 0) or 0)
            next_ts = data.get("fundingDatetime") or data.get("nextFundingDatetime")
            return {"rate": rate, "next_funding_time": next_ts, "symbol": sym}
        except Exception as e:
            logger.debug("get_funding_rate %s: %s", symbol, e)
            return {"rate": 0.0, "next_funding_time": None, "symbol": symbol}

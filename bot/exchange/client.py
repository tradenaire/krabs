import asyncio
import functools
import logging
import math
import time
import uuid
from bot import db
from bot.event_logger import log_event, correlation_id
import aiohttp
import ccxt.async_support as ccxt

logger = logging.getLogger(__name__)


def _mutation(fn):
    @functools.wraps(fn)
    async def wrapped(self, *args, **kwargs):
        # ponytail: one lock per client; one account/replica is the supported deployment.
        async with self._mutation_lock:
            token = correlation_id.set(correlation_id.get() or uuid.uuid4().hex)
            log_event("mutation_started", operation=fn.__name__, args=args, kwargs=kwargs)
            try:
                result = await fn(self, *args, **kwargs)
                log_event("mutation_returned", operation=fn.__name__, result=result)
                return result
            except BaseException as error:
                log_event("mutation_failed", operation=fn.__name__, error_type=type(error).__name__)
                raise
            finally:
                correlation_id.reset(token)
    return wrapped


def _checked(response: dict) -> dict:
    if not isinstance(response, dict) or response.get("success") is not True:
        raise RuntimeError(f"MEXC rejected request: {response.get('code')} {response.get('message', '')}"
                           if isinstance(response, dict) else "Invalid MEXC response")
    return response


def _protection_matches(order, *, side, trigger, contracts, open_type, price):
    return (order["side"] == side and order["trigger_type"] == trigger
            and order["vol"] == contracts and order["open_type"] == open_type
            and order["order_type"] == 5 and order["trend"] == 1
            and math.isclose(order["trigger_price"], price, rel_tol=1e-10, abs_tol=1e-12))


def _protection_snapshot(pos, record):
    return {**{k: pos[k] for k in ("position_id", "opened_at_ms", "contracts", "entry_price", "leverage", "side", "margin_mode")},
            **{k: record[k] for k in ("id", "tp_pct", "sl_pct", "locked_sl")}}


def available_margin(balance: dict, currency: str = "USDT") -> float:
    raw = balance.get("_assets", {}).get(currency, balance.get("_raw", {}) if currency == "USDT" else {})
    for field in ("availableOpen", "availableBalance"):
        if raw.get(field) is not None:
            return float(raw[field])
    raise ValueError(f"Available futures collateral is unknown for {currency}")


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
    """MEXC with aiohttp ThreadedResolver to avoid aiodns getaddrinfo
    failures on Windows (same workaround as the MEXC client). Session is created
    lazily inside the running loop.
    """
    def __init__(self, config=None):
        self._session = None
        self._closing = False
        self._closed = False
        super().__init__(config or {})

    @property
    def session(self):
        if self._session is None:
            if self._closing or self._closed:
                return None
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return None
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), ssl=True)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    @session.setter
    def session(self, value):
        self._session = value
        if value is not None:
            self._closed = False

    async def close(self):
        session = self._session
        self._closing = True
        try:
            await super().close()
        finally:
            if session and not session.closed:
                await session.close()
            if self._session and self._session is not session and not self._session.closed:
                await self._session.close()
            self._session = None
            self._closing = False
            self._closed = True

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

        # CCXT charges contract/detail 100 * 50 ms on the shared queue. Keep that
        # public metadata budget separate from position/balance/order requests.
        self._metadata = _MexcThreadedDNS({
            "enableRateLimit": True, "options": {"defaultType": "swap"},
        })
        self._exchange.fetch_markets = self._metadata.fetch_swap_markets

        self._spot = _MexcThreadedDNS({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })
        self._mutation_lock = asyncio.Lock()

    def futures_symbol(self, symbol: str) -> str:
        symbol = symbol.upper().replace("_", "/")
        if ":" in symbol:
            return symbol
        if "/" not in symbol:
            symbol = f"{symbol}/USDT"
        return f"{symbol}:{symbol.split('/')[1]}"

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
        try:
            await self._exchange.close()
        finally:
            try:
                await self._spot.close()
            finally:
                await self._metadata.close()

    # ── Balance ──────────────────────────────────────────────────────

    async def get_futures_balance(self) -> dict:
        """Return futures balance using contract API directly (avoids spot auth)."""
        raw = _checked(await self._exchange.contractPrivateGetAccountAssets())
        assets = raw.get("data") or []
        usdt = next((a for a in assets if a.get("currency") == "USDT"), {})
        result = {"free": {}, "total": {}, "used": {}, "_raw": usdt, "_assets": {}}
        for asset in assets:
            currency = asset["currency"]
            values = {"free": float(asset.get("availableBalance") or 0),
                      "total": float(asset.get("equity") or 0),
                      "used": float(asset.get("positionMargin") or 0) + float(asset.get("frozenBalance") or 0)}
            result[currency] = values
            result["_assets"][currency] = asset
            for key, value in values.items():
                result[key][currency] = value
        return result

    async def get_spot_balance(self) -> dict:
        # Spot account balances need no market/currency metadata bootstrap.
        raw = await self._spot.spotPrivateGetAccount()
        if not isinstance(raw, dict) or not isinstance(raw.get("balances"), list):
            raise ValueError("Invalid spot account balance response")
        return self._spot.custom_parse_balance(raw, "spot")

    async def get_futures_margin_summary(self) -> dict:
        """Display-only margin from the endpoints used by MEXC's futures wallet.

        Per-currency availableOpen is not the shared multi-asset buying power.
        Keep this separate from the collateral checks used by trading jobs.
        """
        mode = _checked(await self._exchange.request(
            "multiAssets/getMultiAssetMode", ["contract", "private"], "GET")).get("data")
        if mode in ("NOT_OPEN", "FUNCTION_NOT_ALLOWED"):
            return {"mode": "single"}
        if mode != "OPEN":
            raise ValueError("Unknown MEXC asset mode")
        data = _checked(await self._exchange.request(
            "multiAssets/getMultiAssets", ["contract", "private"], "GET")).get("data")
        if not isinstance(data, dict) or data.get("currency") not in ("USDT", "USDC", "USD"):
            raise ValueError("Invalid MEXC margin summary")
        result = {"mode": "multi", "currency": data["currency"]}
        for source, target in (("availableBalance", "available"), ("adjEquity", "collateral")):
            try:
                value = float(data[source])
            except (KeyError, TypeError, ValueError):
                raise ValueError("Missing MEXC margin amount") from None
            if not math.isfinite(value):
                raise ValueError("Invalid MEXC margin amount")
            result[target] = value
        return result

    async def get_asset_prices(self) -> dict:
        rows = await self._spot.spotPublicGetTickerPrice()
        return {r["symbol"][:-4]: float(r["price"]) for r in rows
                if r["symbol"].endswith("USDT") and float(r["price"]) > 0} | {"USDT": 1.0}

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
            raw = _checked(await self._exchange.contractPrivateGetPositionOpenPositions())
        except Exception as e:
            logger.error("contractPrivateGetPositionOpenPositions failed: %s", e)
            raise

        positions = raw.get("data") or []
        if not isinstance(positions, list):
            raise RuntimeError("Invalid MEXC positions payload")

        received_at_ms = int(time.time() * 1000)
        def position_mark(p):
            try:
                value = float(p.get("markPrice") or 0)
                return value if math.isfinite(value) and value > 0 else 0
            except (TypeError, ValueError):
                return 0

        # One fresh bulk read instead of N sequential last-price reads per caller.
        # Keep position reads uncached: mutation guards need the current identity/size.
        tickers = {}
        if any(float(p.get("holdVol") or 0) != 0 and not position_mark(p) for p in positions):
            rows = _checked(await self._exchange.contractPublicGetTicker()).get("data")
            if not isinstance(rows, list):
                raise RuntimeError("Invalid MEXC bulk ticker payload")
            tickers = {r["symbol"]: r for r in rows}
        mark_received_at_ms = int(time.time() * 1000)
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
            ccxt_sym = markets_by_id.get(mexc_sym, self.futures_symbol(mexc_sym))

            # side: 1=long, 2=short
            side_raw = int(p.get("positionType", 1) or 1)
            side = "long" if side_raw == 1 else "short"

            entry = float(p.get("holdAvgPrice", 0) or 0)
            lev = int(p.get("leverage", 1) or 1)
            margin = float(p.get("im", 0) or p.get("margin", 0) or 0)
            raw_liq = float(p.get("liquidatePrice", 0) or 0)
            contracts = vol

            # A last trade is not a mark price; never present missing prices as zero PnL.
            mark = position_mark(p)
            ticker = tickers.get(mexc_sym, {})
            funding_fields = {}
            try:
                funding_rate = float(ticker.get("fundingRate"))
                if math.isfinite(funding_rate):
                    funding_fields = {
                        "funding_rate": funding_rate,
                    }
            except (TypeError, ValueError):
                pass
            mark_source = "position.markPrice" if mark else "ticker.fairPrice"
            if not mark:
                mark = float(ticker.get("fairPrice") or 0)
            if not math.isfinite(mark) or mark <= 0:
                raise RuntimeError(f"MEXC mark price unavailable for {mexc_sym}")

            # Contract size from market data (raw pos doesn't include it)
            try:
                market = self._exchange.markets.get(ccxt_sym, {})
                contract_size = float(market.get("contractSize") or 0)
            except Exception:
                contract_size = 0
            if not contract_size:
                contract_size = float(p.get("contractSize") or 0)
            if not math.isfinite(contract_size) or contract_size <= 0:
                raise RuntimeError(f"MEXC contract size unavailable for {mexc_sym}")

            pos_size = contracts * contract_size  # in base currency

            # Preserve the exchange's PnL from this position snapshot, including zero.
            if p.get("unRealizedPnl") is not None:
                pnl = float(p["unRealizedPnl"])
                pnl_source = "position.unRealizedPnl"
            elif entry > 0:
                pnl = (mark - entry) * pos_size if side == "long" else (entry - mark) * pos_size
                pnl_source = "calculated.fairPrice"
            else:
                raise RuntimeError(f"MEXC entry price unavailable for {mexc_sym}")
            if not math.isfinite(pnl):
                raise RuntimeError(f"Invalid MEXC PnL for {mexc_sym}")

            # Margin: use im from MEXC; recalculate if missing
            if margin == 0 and entry > 0 and lev > 0:
                margin = entry * pos_size / lev

            pct = (pnl / margin * 100) if margin > 0 else 0.0

            open_type = int(p.get("openType", p.get("marginType", 2)) or 2)
            margin_mode = "isolated" if open_type == 1 else "cross"

            result.append({
                "symbol": ccxt_sym,
                "side": side,
                "contracts": contracts,
                "entry_price": entry,
                "mark_price": mark,
                "mark_price_source": mark_source,
                "mark_price_timestamp_ms": ticker.get("timestamp") if mark_source == "ticker.fairPrice" else None,
                "mark_received_at_ms": mark_received_at_ms if mark_source == "ticker.fairPrice" else received_at_ms,
                "snapshot_received_at_ms": received_at_ms,
                "pnl_source": pnl_source,
                "liquidation_price": round(raw_liq, 4),
                "unrealized_pnl": round(pnl, 6),
                "margin": margin,
                "leverage": lev,
                "percentage": round(pct, 2),
                "margin_mode": margin_mode,
                "position_id": p.get("positionId"),
                "opened_at_ms": p.get("createTime"),
                "settle_currency": self._exchange.markets.get(ccxt_sym, {}).get("settle") or ccxt_sym.split(":")[-1],
                "hold_fee": float(p.get("holdFee", 0) or 0),
                "hold_avg_price": entry,
                **funding_fields,
            })

        return result

    async def get_position(self, symbol: str) -> dict | None:
        sym = self.futures_symbol(symbol)
        matches = [p for p in await self.get_positions() if p["symbol"] == sym]
        if len(matches) > 1:
            raise ValueError("Multiple hedge positions for this symbol; symbol-only commands refused")
        return matches[0] if matches else None

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

    @_mutation
    async def place_futures_order(self, symbol: str, side: str, amount_usdt: float,
                                  leverage: int, margin_mode: str | None = None,
                                  expected_position_id: str | None = None) -> dict:
        side = self._normalize_side(side)
        sym = self.futures_symbol(symbol)
        if not math.isfinite(amount_usdt) or amount_usdt <= 0 or leverage <= 0:
            raise ValueError("Margin and leverage must be positive")
        existing = await self.get_position(sym)
        if expected_position_id is not None:
            if not existing or str(existing["position_id"]) != str(expected_position_id) or not db.get_managed_position(existing):
                raise ValueError("Averaging refused: managed position changed")
            if self._normalize_side(existing["side"]) != side:
                raise ValueError("Averaging side mismatch")
        elif existing:
            raise ValueError("Position already exists; opening must not adopt or average it")
        pending_key = f"order_uncertain_{sym}"
        if db.get_config(pending_key):
            raise RuntimeError("Previous opening outcome unknown; reconcile before retry")

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
        contracts = max(1, math.ceil(amount_base / contract_size))

        logger.info("Futures order: %s %s contracts=%d lev=%dx margin=$%.2f",
                    side.upper(), sym, contracts, leverage, amount_usdt)

        fut_bal = await self.get_futures_balance()
        currency = market.get("settle") or sym.split(":")[-1]
        if currency != "USDT":
            raise ValueError("USDT sizing is not supported for non-USDT settled orders")
        if available_margin(fut_bal, currency) < contracts * contract_size * price / leverage:
            raise ValueError("Insufficient available futures collateral")

        if margin_mode is None:
            try:
                existing = await self.get_position(symbol)
                margin_mode = existing.get("margin_mode") or "cross" if existing else "cross"
            except Exception:
                margin_mode = "cross"
        if margin_mode not in ("cross", "isolated"):
            margin_mode = "cross"
        open_type = 1 if margin_mode == "isolated" else 2

        await self.set_leverage(symbol, leverage, side, open_type=open_type)

        mexc_side = 1 if side == "buy" else 3
        mexc_symbol = self._mexc_contract_symbol(market, sym)

        db.set_config(pending_key, "pending")
        result = await self._exchange.contractPrivatePostOrderSubmit({
            "symbol": mexc_symbol,
            "price": 0,
            "vol": contracts,
            "side": mexc_side,
            "type": 5,
            "openType": open_type,
            "leverage": leverage,
            "externalOid": "krabs-" + uuid.uuid4().hex,
        })

        if not result.get("success", False):
            db.set_config(pending_key, "")
            raise RuntimeError(f"MEXC order rejected: {result.get('message', result)}")

        order_id = str(result.get("data", ""))
        details = _checked(await self._exchange.contractPrivateGetOrderGetOrderId({"order_id": order_id}))["data"]
        position_id = details.get("positionId")
        if not position_id:
            raise RuntimeError(f"Order {order_id} accepted; position ID pending, do not repeat opening")
        if expected_position_id is not None:
            if str(position_id) != str(expected_position_id):
                raise RuntimeError("Averaging order attached to an unexpected position; reconciliation required")
            db.set_config(pending_key, "")
        else:
            db.set_config(pending_key, order_id)
        logger.info("MEXC futures order placed: %s (id=%s)", mexc_symbol, order_id)
        return {"id": order_id, "symbol": sym, "side": side,
                "amount": contracts, "price": price, "leverage": leverage,
                "position_id": str(position_id),
                "margin_mode": margin_mode, "info": result}

    @_mutation
    async def partial_close_futures_position(self, symbol: str, contracts: int,
                                             expected_position_id: str | None = None) -> dict:
        """Close `contracts` contracts of an open position (partial close)."""
        sym = self.futures_symbol(symbol)
        pos = await self.get_position(symbol)
        if not pos:
            raise ValueError(f"No open position for {sym}")
        record = db.get_managed_position(pos)
        if not record or str(pos["position_id"]) != str(expected_position_id):
            raise ValueError("Partial close refused: managed position changed")

        side = pos["side"]
        total_contracts = int(round(pos["contracts"]))
        contracts = max(1, min(contracts, total_contracts))
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
            raise RuntimeError(f"MEXC partial close failed: {result.get('message', result)}")
        db.save_bot_order(result.get("data"), record["id"], sym, "emergency", order_type="regular", confirmed=True)

        logger.info("Partial close: %s %s %d/%d contracts", sym, side, contracts, total_contracts)
        return {"id": str(result.get("data", "")), "symbol": sym,
                "contracts_closed": contracts, "info": result}

    @_mutation
    async def close_futures_position(self, symbol: str, expected_position_id: str | None = None,
                                     reason: str = "manual") -> dict:
        sym = self.futures_symbol(symbol)
        pos = await self.get_position(symbol)
        if not pos:
            raise ValueError(f"No open position for {sym}")
        record = db.get_managed_position(pos)
        if not record or str(pos["position_id"]) != str(expected_position_id):
            raise ValueError("Close refused: managed position changed")

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

        with db._connect() as conn:
            conn.execute("UPDATE positions SET status='closing' WHERE id=?", (record["id"],))
        result = await self._exchange.contractPrivatePostOrderSubmit(params)
        if not result.get("success", False):
            with db._connect() as conn:
                conn.execute("UPDATE positions SET status='open' WHERE id=?", (record["id"],))
            raise RuntimeError(f"MEXC close failed: {result.get('message', result)}")

        order_id = str(result.get("data", ""))
        db.save_bot_order(order_id, record["id"], sym, reason, order_type="regular", confirmed=True)
        with db._connect() as conn:
            conn.execute("UPDATE positions SET status='closing' WHERE id=?", (record["id"],))
        logger.info("Close submitted: %s %s (%d contracts)", sym, side, contracts)
        return {"id": order_id, "symbol": sym, "status": "submitted", "info": result}

    # ── TP/SL ────────────────────────────────────────────────────────

    async def cancel_plan_orders(self, symbol: str) -> int:
        return await self.cancel_tp_sl_orders(symbol)

    async def _plan_orders(self, symbol: str | None = None, **filters) -> list[dict]:
        params = {"page_size": 100, **filters}
        if symbol:
            params["symbol"] = self.futures_symbol(symbol).split(":")[0].replace("/", "_")
        orders = []
        for page in range(1, 101):
            response = _checked(await self._exchange.contractPrivateGetPlanorderListOrders(
                {**params, "page_num": page}))
            data = response.get("data")
            batch = data.get("resultList", data.get("result_list", [])) if isinstance(data, dict) else data
            if not isinstance(batch, list):
                raise RuntimeError("Invalid plan-order list")
            orders.extend(batch)
            if len(batch) < 100:
                return orders
        raise RuntimeError("Plan-order pagination limit reached; cleanup aborted")

    async def get_tp_sl_orders(self, symbol: str | None = None) -> list[dict]:
        return [{"id": str(o["id"]), "symbol": self.futures_symbol(o["symbol"]),
                 "trigger_price": float(o.get("triggerPrice") or 0),
                 "side": int(o.get("side") or 0), "trigger_type": int(o.get("triggerType") or 0),
                 "vol": float(o.get("vol") or 0), "open_type": int(o.get("openType") or 0),
                 "order_type": int(o.get("orderType") or 0), "trend": int(o.get("trend") or 0)}
                for o in await self._plan_orders(symbol, states="1") if str(o.get("state")) == "1"]

    async def get_native_stop_orders(self, symbol: str | None = None) -> list[dict]:
        """Read-only native stop records in the API's maximum 90-day window.

        These records never enter plan-order ownership, confirmation or cancellation.
        vol=0/volType=2 is retained; it does not prove full-position coverage.
        """
        end = int(time.time() * 1000)
        params = {"is_finished": 0, "page_size": 100,
                  "start_time": end - 90 * 86400_000, "end_time": end}
        if symbol:
            params["symbol"] = self.futures_symbol(symbol).split(":")[0].replace("/", "_")
        orders, seen = [], set()
        for page in range(1, 101):
            batch = _checked(await self._exchange.contractPrivateGetStoporderListOrders(
                {**params, "page_num": page})).get("data")
            if not isinstance(batch, list):
                raise RuntimeError("Invalid native stop-order list")
            for order in batch:
                if not isinstance(order, dict) or order.get("id") in (None, "", 0, "0"):
                    raise RuntimeError("Invalid native stop-order identity")
                oid = str(order["id"])
                if oid in seen:
                    raise RuntimeError("Native stop-order pages overlap; snapshot incomplete")
                seen.add(oid)
                if str(order.get("state")) == "1" and str(order.get("isFinished")) == "0":
                    orders.append(order)
            if len(batch) < 100:
                return orders
        raise RuntimeError("Native stop-order pagination limit reached")

    async def _cancel_owned(self, orders: list[dict]) -> int:
        for order in orders:
            _checked(await self._exchange.contractPrivatePostPlanorderCancel([
                {"symbol": order["symbol"].split(":")[0].replace("/", "_"), "orderId": order["id"]}]))
        return len(orders)

    @_mutation
    async def cancel_tp_sl_orders(self, symbol: str, position_key: int | None = None) -> int:
        sym = self.futures_symbol(symbol)
        owned = {o["order_id"] for o in db.get_bot_orders(position_key) if o["symbol"] == sym}
        active = await self.get_tp_sl_orders(sym)
        return await self._cancel_owned([o for o in active if o["id"] in owned])

    async def audit_protection(self, symbol: str) -> dict:
        """Read-only audit using the same order checks as protection placement."""
        pos = await self.get_position(symbol)
        if not pos:
            raise ValueError("Open position not found")
        record = db.get_managed_position(pos, allow_closing=True)
        if not record:
            return {"status": "UNMANAGED", "symbol": pos["symbol"], "position_id": pos["position_id"]}
        sym, side = pos["symbol"], pos["side"]
        sign = 1 if side == "long" else -1
        entry, lev = pos["entry_price"], pos["leverage"]
        prices = {"TP": entry * (1 + sign * record["tp_pct"] / 100 / lev),
                  "SL": entry * (1 - sign * record["sl_pct"] / 100 / lev)}
        if record.get("locked_sl") is not None:
            prices["SL"] = (max if side == "long" else min)(prices["SL"], record["locked_sl"])
        await self._exchange.load_markets()
        prices = {k: float(self._exchange.price_to_precision(sym, v)) for k, v in prices.items()}
        active = await self.get_tp_sl_orders(sym)
        saved = {o["order_id"]: o for o in db.get_bot_orders(record["id"])}
        legs = {}
        for kind, trigger in (("TP", 1 if side == "long" else 2), ("SL", 2 if side == "long" else 1)):
            kinds = ("SL", "profit_lock") if kind == "SL" else ("TP",)
            legs[kind] = [o["id"] for o in active if o["id"] in saved and saved[o["id"]]["kind"] in kinds
                          and _protection_matches(o, side=4 if side == "long" else 2,
                              trigger=trigger, contracts=float(pos["contracts"]),
                              open_type=2 if pos.get("margin_mode") == "cross" else 1, price=prices[kind])]
        result = {"status": "CONFIRMED" if all(legs.values()) else "INCOMPLETE", "symbol": sym,
                  "position_id": pos["position_id"], "prices": prices, "legs": legs,
                  "snapshot": _protection_snapshot(pos, record)}
        log_event("protection_audit", **result)
        return result

    @_mutation
    async def set_tp_sl(self, symbol: str, tp_price: float | None = None,
                        sl_price: float | None = None, pos_data: dict | None = None,
                        sl_limit_price: float | None = None,
                        profit_lock_step: float | None = None,
                        expected_snapshot: dict | None = None) -> list[dict]:
        sym = self.futures_symbol(symbol)
        pos = await self.get_position(sym)
        if not pos or (pos_data and str(pos["position_id"]) != str(pos_data.get("position_id"))):
            raise ValueError("Position changed before protection update")
        record = db.get_managed_position(pos, allow_closing=True)
        if not record:
            raise ValueError("Position is unmanaged; use /adopt SYMBOL confirm first")
        if expected_snapshot is not None and expected_snapshot != _protection_snapshot(pos, record):
            raise ValueError("Position or protection settings changed; request a new repair preview")
        initial_snapshot = _protection_snapshot(pos, record)
        side = pos["side"]
        close_side = 4 if side == "long" else 2
        open_type = 2 if pos.get("margin_mode") == "cross" else 1
        contracts = float(pos["contracts"])
        if contracts <= 0 or not math.isfinite(contracts):
            raise ValueError("Invalid position volume")
        if sl_price is not None and record.get("locked_sl") is not None:
            sl_price = max(sl_price, record["locked_sl"]) if side == "long" else min(sl_price, record["locked_sl"])
        await self._exchange.load_markets()
        mexc_sym = self._mexc_contract_symbol(self._exchange.market(sym), sym)
        active = await self.get_tp_sl_orders(sym)
        saved = {o["order_id"]: o for o in db.get_bot_orders(record["id"])}
        results, errors = [], []
        # Protect downside first. A TP failure must not undo a confirmed SL.
        for kind, price, trigger in (("SL", sl_price, 2 if side == "long" else 1),
                                      ("TP", tp_price, 1 if side == "long" else 2)):
            if price is None:
                continue
            pending_key = f"plan_uncertain_{record['id']}_{kind}"
            try:
                if not math.isfinite(price) or price <= 0:
                    raise ValueError(f"Invalid {kind} price")
                price = float(self._exchange.price_to_precision(sym, price))
                if price <= 0:
                    raise ValueError(f"{kind} price rounds to zero")
                old = [o for o in active if o["id"] in saved and saved[o["id"]]["kind"] in
                       (("SL", "profit_lock") if kind == "SL" else ("TP",))]
                def matches(order):
                    return _protection_matches(order, side=close_side, trigger=trigger,
                                               contracts=contracts, open_type=open_type, price=price)
                found = next((o for o in old if matches(o)), None)
                if not found:
                    pending = db.get_config(pending_key)
                    visible_ids = {o["id"] for o in active}
                    unresolved = [o for o in saved.values() if not o["confirmed"]
                                  and o["kind"] in (("SL", "profit_lock") if kind == "SL" else ("TP",))]
                    if unresolved or (pending and pending not in visible_ids):
                        raise RuntimeError("Previous placement outcome is unknown; reconcile order ID before retry")
                    db.set_config(pending_key, "pending")
                    response = await self._exchange.contractPrivatePostPlanorderPlace({
                        "symbol": mexc_sym, "price": 0, "vol": contracts, "side": close_side,
                        "orderType": 5, "openType": open_type, "leverage": pos["leverage"],
                        "triggerPrice": str(price), "triggerType": trigger, "trend": 1, "executeCycle": 2,
                    })
                    if response.get("success") is False:
                        db.set_config(pending_key, "")
                    _checked(response)
                    order_id = response.get("data")
                    if isinstance(order_id, dict):
                        order_id = order_id.get("orderId") or order_id.get("id")
                    is_lock = kind == "SL" and (profit_lock_step is not None or record.get("locked_sl") is not None)
                    db.save_bot_order(order_id, record["id"], sym, "profit_lock" if is_lock else kind, price)
                    db.set_config(pending_key, str(order_id))
                    for attempt in range(4):
                        fresh = await self.get_tp_sl_orders(sym)
                        found = next((o for o in fresh if o["id"] == str(order_id) and matches(o)), None)
                        if found:
                            break
                        await asyncio.sleep(0.25)
                    if not found:
                        raise RuntimeError(f"{kind} accepted but active protection not confirmed")
                current = await self.get_position(sym)
                if not current or _protection_snapshot(current, record) != initial_snapshot:
                    errors.append(f"{kind}: Position changed during protection update; confirmation refused")
                    break
                lock_price = price if kind == "SL" and (profit_lock_step is not None or record.get("locked_sl") is not None) else None
                db.confirm_protection(record["id"], found["id"], lock_price, profit_lock_step if kind == "SL" else None)
                if db.get_config(pending_key) == found["id"]:
                    db.set_config(pending_key, "")
                # Replacement is confirmed before cancelling only our obsolete leg(s).
                await self._cancel_owned([o for o in old if o["id"] != found["id"]])
                results.append({"type": kind, "price": price, "id": found["id"], "confirmed": True})
            except Exception as error:
                errors.append(f"{kind}: {error}")
        if errors:
            confirmed = ", ".join(r["type"] for r in results) or "none"
            raise RuntimeError(f"Protection incomplete (confirmed: {confirmed}); " + "; ".join(errors))
        if results:
            current = await self.get_position(sym)
            if not current or _protection_snapshot(current, record) != initial_snapshot:
                raise RuntimeError("Position changed before final protection confirmation")
            active = await self.get_tp_sl_orders(sym)
            for leg in results:
                trigger = (1 if side == "long" else 2) if leg["type"] == "TP" else (2 if side == "long" else 1)
                if not any(o["id"] == leg["id"] and _protection_matches(o,
                           side=close_side, trigger=trigger, contracts=contracts,
                           open_type=open_type, price=leg["price"]) for o in active):
                    raise RuntimeError(f"{leg['type']} not active at final protection confirmation")
        return results

    async def _history(self, method, symbol: str, opened_at_ms: int) -> list[dict]:
        rows = []
        for page in range(1, 101):
            response = _checked(await method({"symbol": self.futures_symbol(symbol).split(":")[0].replace("/", "_"),
                "start_time": int(opened_at_ms), "end_time": min(int(time.time()*1000), int(opened_at_ms)+90*86400000),
                "page_num": page, "page_size": 100}))
            data = response.get("data")
            batch = data.get("resultList", data.get("result_list", [])) if isinstance(data, dict) else data
            if not isinstance(batch, list):
                raise RuntimeError("Invalid history response")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise RuntimeError("History pagination incomplete")

    async def get_closed_position_result(self, record: dict) -> dict | None:
        import datetime as dt
        pid, opened = record.get("exchange_position_id"), record.get("opened_at_ms")
        if not pid or not opened:
            return None
        positions = await self._history(self._exchange.contractPrivateGetPositionListHistoryPositions, record["symbol"], opened)
        closed = next((p for p in positions if str(p.get("positionId")) == pid
                       and self.futures_symbol(p["symbol"]) == record["symbol"]
                       and int(p.get("positionType") or 0) == (1 if record["side"] == "long" else 2)
                       and str(p.get("state")) == "3" and int(p.get("createTime") or 0) == int(opened)), None)
        if closed is None:
            return None
        closed_ms = int(closed.get("updateTime") or 0)
        if closed_ms < int(opened):
            return None
        orders = await self._history(self._exchange.contractPrivateGetOrderListHistoryOrders, record["symbol"], opened)
        fills = [o for o in orders if str(o.get("positionId")) == pid
                 and self.futures_symbol(o["symbol"]) == record["symbol"]
                 and int(o.get("side") or 0) == (4 if record["side"] == "long" else 2)
                 and float(o.get("dealVol") or 0) > 0 and float(o.get("dealAvgPrice") or 0) > 0
                 and int(opened) <= int(o.get("updateTime") or 0) <= closed_ms]
        reason = "unknown"
        last_time = max((int(o.get("updateTime") or 0) for o in fills), default=0)
        latest = [o for o in fills if int(o.get("updateTime") or 0) == last_time]
        if len(latest) == 1:
            last = latest[0]
            category = int(last.get("category") or 0)
            if category == 2:
                reason = "liquidation"
            elif category == 4:
                reason = "adl"
            else:
                regular = {o["order_id"]: o for o in db.get_bot_orders(record["id"], "regular")}
                if str(last["orderId"]) in regular:
                    reason = regular[str(last["orderId"])]["kind"]
                else:
                    plans = await self._plan_orders(record["symbol"], start_time=int(opened),
                                                  end_time=closed_ms)
                    saved = {o["order_id"]: o for o in db.get_bot_orders(record["id"])}
                    plan = next((o for o in plans if str(o.get("orderId")) == str(last["orderId"])
                                 and str(o.get("state")) == "3" and str(o.get("id")) in saved), None)
                    if plan:
                        reason = saved[str(plan["id"])]["kind"].lower()
        # Exchange position realised is the metric; never add fees/funding to it again.
        pnl = float(closed["realised"]) if closed.get("realised") is not None else None
        price = float(closed["closeAvgPrice"]) if closed.get("closeAvgPrice") else None
        return {"reason": reason, "pnl": pnl, "exit_price": price,
                "closed_at": dt.datetime.fromtimestamp(closed_ms / 1000, dt.timezone.utc).isoformat(),
                "order_ids": [str(o["orderId"]) for o in fills]}

    # ── Helpers ───────────────────────────────────────────────────────

    async def get_contract_details(self) -> list:
        try:
            result = await self._metadata.contractPublicGetDetail()
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
        """Return minimum USDT margin needed for an order at given leverage.

        Uses exchange minimum notional (limits.cost.min) when available,
        which is what MEXC actually enforces (e.g. 5 USDT for DASH).
        Falls back to 1-contract margin calculation.
        """
        sym = self.futures_symbol(symbol)
        try:
            await self._exchange.load_markets()
            market = self._exchange.market(sym)
            # MEXC enforces minimum notional (position value), not margin
            min_notional = float((market.get("limits") or {}).get("cost", {}).get("min", 0) or 0)
            if min_notional > 0:
                return min_notional / max(leverage, 1)
            # Fallback: margin for 1 contract
            contract_size = float(market.get("contractSize", 0.0001))
            ticker = await self.get_ticker(sym)
            price = float(ticker["last"])
            return contract_size * price / max(leverage, 1)
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
        return available_margin(await self.get_futures_balance())

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

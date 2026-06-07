"""Exchange client factory — selects the provider from config.

- "mexc"             -> MEXC live client (bot/exchange/client.py)
- "binance_testnet"  -> Binance USDM Futures with ccxt sandbox/testnet enabled
- "binance"          -> Binance USDM Futures live (real money; use with care)
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _provider(config) -> str:
    return (getattr(config, "exchange_provider", "mexc") or "mexc").lower()


def create_exchange_client(config):
    """Build the exchange client for the configured provider."""
    provider = _provider(config)

    if provider in ("binance", "binance_testnet"):
        from bot.exchange.binance_client import BinanceClient
        testnet = provider == "binance_testnet" or bool(getattr(config, "binance_testnet", False))
        client = BinanceClient(
            getattr(config, "binance_api_key", ""),
            getattr(config, "binance_secret", ""),
            testnet=testnet,
        )
        logger.info("Exchange provider: Binance (testnet=%s)", testnet)
        return client

    from bot.exchange.client import ExchangeClient
    logger.info("Exchange provider: MEXC")
    return ExchangeClient(
        getattr(config, "mexc_api_key", ""),
        getattr(config, "mexc_secret", ""),
    )


def provider_credentials(config) -> tuple[str, str, bool]:
    """Return (api_key, secret, testnet) for the active provider — used to spawn
    the scanner worker process with matching credentials."""
    provider = _provider(config)
    if provider in ("binance", "binance_testnet"):
        return (
            getattr(config, "binance_api_key", ""),
            getattr(config, "binance_secret", ""),
            provider == "binance_testnet" or bool(getattr(config, "binance_testnet", False)),
        )
    return getattr(config, "mexc_api_key", ""), getattr(config, "mexc_secret", ""), False

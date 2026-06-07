"""Emergency close module.

Watches available margin and, when it drops below a configured fraction of free
balance, trims a small slice of every open position to free margin and pauses
averaging for 5 minutes. Previously this lived inline inside ``averaging_job``;
it now runs as its own engine so it keeps protecting the account even if the
averaging loop is busy or paused.

Coordination with averaging is via the shared ``_avg_disabled_until`` flag in
bot_data, which AveragingEngine already honors.
"""
from __future__ import annotations

import logging
import math
import time

from bot.engines.base import Engine

logger = logging.getLogger(__name__)


async def run_margin_emergency(app) -> bool:
    """Returns True if an emergency trim was performed."""
    config = app.bot_data.get("config")
    if not config:
        return False
    emerg_pct = float(getattr(config, "margin_emergency_threshold_pct", 0))
    if emerg_pct <= 0:
        return False

    client = app.bot_data.get("exchange")
    if not client:
        return False

    positions = app.bot_data.get("_pos_cache")
    if positions is None:
        try:
            positions = await client.get_positions()
        except Exception as e:
            logger.warning("emergency: get_positions failed: %s", e)
            return False
    if not positions:
        return False

    from bot.jobs.main import _notify_all

    try:
        full_bal = await client.get_futures_balance()
        raw_bal = full_bal.get("_raw", {})
        free = float(full_bal.get("free", {}).get("USDT", 0) or 0)
        avail = float(raw_bal.get("availableOpen", raw_bal.get("availableBalance", free)) or free)
    except Exception as e:
        logger.warning("emergency balance check: %s", e)
        return False

    if not (free > 0 and avail < (emerg_pct / 100.0) * free):
        return False

    _TRIM_PCT = 0.05
    logger.warning(
        "Margin emergency: avail=$%.2f < %.0f%% of free=$%.2f, trimming %.0f%% of all positions",
        avail, emerg_pct, free, _TRIM_PCT * 100,
    )
    trimmed = []
    for ep in positions:
        sym = ep["symbol"]
        coin = sym.split("/")[0]
        total_c = int(round(float(ep.get("contracts", 0))))
        close_c = max(1, math.floor(total_c * _TRIM_PCT))
        try:
            await client.partial_close_futures_position(sym, close_c)
            margin_ep = float(ep.get("margin", 0))
            freed_est = margin_ep * _TRIM_PCT
            trimmed.append(f"`{coin}` -{close_c}к (~`${freed_est:.2f}`)")
            logger.info("margin_emergency: trimmed %s by %d contracts", sym, close_c)
        except Exception as ce:
            logger.error("margin_emergency trim %s: %s", sym, ce)

    if trimmed:
        await _notify_all(
            app,
            f"✂️ *Сократил позиции* (avail `${avail:.2f}` < `{emerg_pct:.0f}%` от `${free:.2f}`)\n"
            + "\n".join(trimmed),
        )
    app.bot_data["_avg_disabled_until"] = time.time() + 300
    await _notify_all(app, "⏸ *Докупки приостановлены на 5 минут* (аварийное закрытие)")
    return True


class EmergencyEngine(Engine):
    name = "emergency"
    interval = 10.0

    async def tick(self) -> None:
        await run_margin_emergency(self.app)

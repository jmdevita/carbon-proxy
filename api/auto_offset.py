"""Auto-offset background loop.

Periodically checks the user's carbon balance and, when enabled, purchases offsets
up to a per-day spending cap. Above the cap, purchasing stops for the day and the
dashboard banner surfaces remaining debt for manual intervention.
"""
import asyncio
import logging
import math

from config import settings
import db
from api.offsets import (
    get_quote,
    is_auto_offset_enabled,
    purchase_offset,
)

logger = logging.getLogger("carbon-proxy.auto_offset")

# Serializes ticks so the loop and toggle-immediate path can never double-purchase.
_tick_lock = asyncio.Lock()


async def tick():
    """Single auto-offset evaluation. No-ops unless conditions are met."""
    async with _tick_lock:
        await _tick_inner()


async def fire_and_forget():
    """Run tick safely as a background task, swallowing exceptions."""
    try:
        await tick()
    except Exception as e:
        logger.warning("Auto-offset fire-and-forget failed: %s", e)


async def _tick_inner():
    enabled = await asyncio.to_thread(is_auto_offset_enabled)
    if not enabled:
        return

    balance = await asyncio.to_thread(db.get_balance)
    debt_grams = balance.get("balance_grams", 0)
    if debt_grams <= 0:
        return

    today_spent = await asyncio.to_thread(db.get_today_auto_spend_cents)
    cap = settings.auto_offset_daily_cap_cents
    remaining_cents = cap - today_spent
    if remaining_cents <= 0:
        return  # banner will surface this

    # Quote the full debt to learn the per-gram rate.
    try:
        quotes = await get_quote(debt_grams)
    except Exception as e:
        logger.warning("Auto-offset quote failed: %s", e)
        return

    paid_quote = next((q for q in quotes if q.cost_cents > 0), None)
    if paid_quote is None:
        # All providers free/pre-funded. Just purchase the full amount.
        affordable_grams = debt_grams
    else:
        # Linear scale: (remaining_cents / quote_cents) * quote_grams
        affordable_grams = min(
            debt_grams,
            (remaining_cents / paid_quote.cost_cents) * paid_quote.co2_grams,
        )

    if affordable_grams < settings.auto_offset_min_purchase_grams:
        logger.debug(
            "Auto-offset: affordable=%dg below min=%dg, skipping",
            int(affordable_grams), settings.auto_offset_min_purchase_grams,
        )
        return

    # Round down to a whole kg to align with CNaught minimum purchase units.
    affordable_grams = math.floor(affordable_grams / 1000) * 1000
    if affordable_grams < settings.auto_offset_min_purchase_grams:
        return

    logger.info(
        "Auto-offset firing: debt=%.0fg, today_spent=%dc, cap=%dc, buying=%.0fg",
        debt_grams, today_spent, cap, affordable_grams,
    )
    try:
        await purchase_offset(affordable_grams, is_auto=True)
    except Exception as e:
        logger.error("Auto-offset purchase failed: %s", e)


async def push_loop():
    """Background loop. Mirror of trmnl.push_loop."""
    interval = settings.auto_offset_check_interval_s
    logger.info("Auto-offset loop started (every %ds)", interval)
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            logger.info("Auto-offset loop cancelled")
            raise
        except Exception as e:
            logger.warning("Auto-offset tick failed: %s", e)
        await asyncio.sleep(interval)

import asyncio
import logging
import math
import time
from dataclasses import dataclass

import httpx

from config import settings
import db

logger = logging.getLogger("carbon-proxy.offsets")

# Cached CNaught per-kg rate (cents)
_cached_rate_cents: int | None = None
_cache_time: float = 0
_QUOTE_CACHE_TTL = 1800  # 30 minutes


@dataclass
class OffsetResult:
    provider: str
    co2_grams_offset: float
    cost_cents: int
    currency: str
    certificate_url: str
    order_id: str
    tree_count: int = 0


async def purchase_cnaught(co2_grams: float) -> OffsetResult:
    """Purchase carbon credits via CNaught API (1 kg minimum)."""
    if not settings.cnaught_api_key:
        raise ValueError("CNAUGHT_API_KEY not configured")

    # Convert grams to kg, round up to 1 kg minimum
    amount_kg = max(1, math.ceil(co2_grams / 1000))

    payload = {"amount_kg": amount_kg}
    if settings.cnaught_portfolio_id:
        payload["portfolio_id"] = settings.cnaught_portfolio_id

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.cnaught_base_url}/orders",
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.cnaught_api_key}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

    return OffsetResult(
        provider="cnaught",
        co2_grams_offset=amount_kg * 1000,
        cost_cents=data.get("price_usd_cents", 0),
        currency="USD",
        certificate_url=data.get("certificate_public_url", "") or "",
        order_id=data.get("id", ""),
    )


async def purchase_tree_nation(co2_grams: float) -> OffsetResult:
    """Plant a tree via Tree-Nation API."""
    if not settings.tree_nation_api_key:
        raise ValueError("TREE_NATION_API_KEY not configured")
    if not settings.tree_nation_planter_id:
        raise ValueError("TREE_NATION_PLANTER_ID not configured")

    payload = {
        "planter_id": settings.tree_nation_planter_id,
        "quantity": 1,
    }
    if settings.tree_nation_species_id:
        payload["species_id"] = settings.tree_nation_species_id

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.tree_nation_base_url}/plant",
            json=payload,
            headers={"Authorization": f"Bearer {settings.tree_nation_api_key}"},
            timeout=30,
        )
        if resp.status_code in (402, 403):
            raise ValueError("Tree-Nation insufficient credits -- top up at tree-nation.com")
        resp.raise_for_status()
        data = resp.json()

    trees = data.get("trees", [])
    certificate_url = trees[0].get("certificate_url", "") if trees else ""
    tree_id = str(trees[0].get("id", "")) if trees else ""

    return OffsetResult(
        provider="tree-nation",
        co2_grams_offset=co2_grams,
        cost_cents=0,  # Tree-Nation doesn't return cost in API
        currency="EUR",
        certificate_url=certificate_url,
        order_id=tree_id,
        tree_count=len(trees),
    )


@dataclass
class QuoteResult:
    provider: str
    co2_grams: float
    amount_kg: int
    cost_cents: int
    currency: str


async def _fetch_cnaught_rate() -> int:
    """Fetch the per-kg rate from CNaught (cents), with 30-min cache."""
    global _cached_rate_cents, _cache_time

    now = time.monotonic()
    if _cached_rate_cents is not None and (now - _cache_time) < _QUOTE_CACHE_TTL:
        return _cached_rate_cents

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.cnaught_base_url}/quotes",
                json={"amount_kg": 1},
                headers={
                    "Authorization": f"Bearer {settings.cnaught_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

        rate = data.get("price_usd_cents", 0)
        _cached_rate_cents = rate
        _cache_time = now
        logger.info("Updated CNaught rate: %d cents/kg (cached 30m)", rate)
        return rate
    except Exception as e:
        if _cached_rate_cents is not None:
            logger.warning("CNaught rate fetch failed (%s), using stale cached rate", e)
            return _cached_rate_cents
        raise


async def quote_cnaught(co2_grams: float) -> QuoteResult:
    """Get a price quote from CNaught without purchasing."""
    if not settings.cnaught_api_key:
        raise ValueError("CNAUGHT_API_KEY not configured")

    amount_kg = max(1, math.ceil(co2_grams / 1000))
    rate_cents = await _fetch_cnaught_rate()

    return QuoteResult(
        provider="cnaught",
        co2_grams=amount_kg * 1000,
        amount_kg=amount_kg,
        cost_cents=rate_cents * amount_kg,
        currency="USD",
    )


async def get_quote(co2_grams: float) -> list[QuoteResult]:
    """Get price quotes from configured provider(s)."""
    quotes = []
    provider = settings.offset_provider

    if provider in ("cnaught", "both"):
        try:
            quotes.append(await quote_cnaught(co2_grams))
        except Exception as e:
            logger.error("CNaught quote failed: %s", e)

    if provider in ("tree-nation", "both"):
        # Tree-Nation is pre-funded, no cost quote available
        amount_kg = max(1, math.ceil(co2_grams / 1000))
        quotes.append(QuoteResult(
            provider="tree-nation",
            co2_grams=co2_grams,
            amount_kg=amount_kg,
            cost_cents=0,
            currency="EUR",
        ))

    return quotes


async def _log_and_append(result: OffsetResult, results: list[OffsetResult], is_auto: bool = False):
    await db.log_offset_async(
        provider=result.provider,
        co2_grams_offset=result.co2_grams_offset,
        cost_cents=result.cost_cents,
        currency=result.currency,
        certificate_url=result.certificate_url,
        order_id=result.order_id,
        tree_count=result.tree_count,
        is_auto=is_auto,
    )
    results.append(result)


async def purchase_offset(co2_grams: float, is_auto: bool = False) -> list[OffsetResult]:
    """Purchase offset from configured provider(s). Returns list of results."""
    results = []
    provider = settings.offset_provider
    tag = " (auto)" if is_auto else ""

    if provider in ("cnaught", "both"):
        try:
            result = await purchase_cnaught(co2_grams)
            await _log_and_append(result, results, is_auto=is_auto)
            logger.info(
                "CNaught offset purchased%s: %d kg CO2, $%.2f, order=%s",
                tag, math.ceil(co2_grams / 1000), result.cost_cents / 100, result.order_id,
            )
        except Exception as e:
            logger.error("CNaught offset failed: %s", e)

    if provider in ("tree-nation", "both"):
        try:
            result = await purchase_tree_nation(co2_grams)
            await _log_and_append(result, results, is_auto=is_auto)
            logger.info(
                "Tree-Nation offset purchased%s: %d trees, order=%s",
                tag, result.tree_count, result.order_id,
            )
        except Exception as e:
            logger.error("Tree-Nation offset failed: %s", e)

    return results


def is_auto_offset_enabled() -> bool:
    val = db.get_kv("auto_offset_enabled")
    if val is None:
        return settings.auto_offset_default_enabled
    return val == "true"


async def get_auto_offset_status() -> dict:
    """Return current auto-offset status for the dashboard banner + settings UI."""
    enabled = await asyncio.to_thread(is_auto_offset_enabled)
    today_spent = await asyncio.to_thread(db.get_today_auto_spend_cents)
    balance = await asyncio.to_thread(db.get_balance)
    cap = settings.auto_offset_daily_cap_cents
    pending_grams = max(0.0, balance.get("balance_grams", 0))
    cap_exceeded = enabled and today_spent >= cap and pending_grams > 0

    return {
        "enabled": enabled,
        "daily_cap_cents": cap,
        "today_spent_cents": today_spent,
        "cap_exceeded": cap_exceeded,
        "pending_grams": round(pending_grams, 4),
        "min_purchase_grams": settings.auto_offset_min_purchase_grams,
        "check_interval_s": settings.auto_offset_check_interval_s,
    }

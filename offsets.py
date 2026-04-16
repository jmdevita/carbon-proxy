import logging
from dataclasses import dataclass

import httpx

from config import settings
import db

logger = logging.getLogger("carbon-proxy.offsets")


@dataclass
class OffsetResult:
    provider: str
    co2_grams_offset: float
    cost_cents: int
    currency: str
    certificate_url: str
    order_id: str
    tree_count: int = 0


async def purchase_patch(co2_grams: float) -> OffsetResult:
    """Purchase carbon offset via Patch API (two-step: create draft, then place)."""
    if not settings.patch_api_key:
        raise ValueError("PATCH_API_KEY not configured")

    payload = {
        "amount": int(co2_grams),
        "unit": "g",
    }
    if settings.patch_project_id:
        payload["project_id"] = settings.patch_project_id

    headers = {"Authorization": f"Bearer {settings.patch_api_key}"}

    async with httpx.AsyncClient() as client:
        # Step 1: Create draft order
        resp = await client.post(
            "https://api.patch.io/v1/orders",
            json=payload,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        order = data.get("data", {})
        order_id = order.get("id", "")

        # Step 2: Place (confirm) the order to actually purchase
        if order_id:
            place_resp = await client.patch(
                f"https://api.patch.io/v1/orders/{order_id}/place",
                headers=headers,
                timeout=30,
            )
            place_resp.raise_for_status()
            placed_data = place_resp.json()
            order = placed_data.get("data", order)

    return OffsetResult(
        provider="patch",
        co2_grams_offset=co2_grams,
        cost_cents=order.get("price", 0),
        currency=order.get("currency", "USD"),
        certificate_url=order.get("registry_url", ""),
        order_id=order_id,
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


async def purchase_offset(co2_grams: float) -> list[OffsetResult]:
    """Purchase offset from configured provider(s). Returns list of results."""
    results = []
    provider = settings.offset_provider

    if provider in ("patch", "both"):
        try:
            result = await purchase_patch(co2_grams)
            await db.log_offset_async(
                provider=result.provider,
                co2_grams_offset=result.co2_grams_offset,
                cost_cents=result.cost_cents,
                currency=result.currency,
                certificate_url=result.certificate_url,
                order_id=result.order_id,
                tree_count=result.tree_count,
            )
            results.append(result)
            logger.info(
                "Patch offset purchased: %.1fg CO2, order=%s",
                co2_grams, result.order_id,
            )
        except Exception as e:
            logger.error("Patch offset failed: %s", e)

    if provider in ("tree-nation", "both"):
        try:
            result = await purchase_tree_nation(co2_grams)
            await db.log_offset_async(
                provider=result.provider,
                co2_grams_offset=result.co2_grams_offset,
                cost_cents=result.cost_cents,
                currency=result.currency,
                certificate_url=result.certificate_url,
                order_id=result.order_id,
                tree_count=result.tree_count,
            )
            results.append(result)
            logger.info(
                "Tree-Nation offset purchased: %d trees, order=%s",
                result.tree_count, result.order_id,
            )
        except Exception as e:
            logger.error("Tree-Nation offset failed: %s", e)

    return results

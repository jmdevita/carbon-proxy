import asyncio

from fastapi import APIRouter, Body, Query, HTTPException, Request

import db
from energy.carbon import equivalents
from config import settings
from energy.power import monitor as power_monitor
from api.offsets import purchase_offset, get_quote, get_auto_offset_status
from api import auto_offset

router = APIRouter(prefix="/carbon", tags=["carbon"])


@router.get("/summary")
async def summary(
    source: str | None = Query(None),
    model: str | None = Query(None),
    since: str | None = Query(None, description="ISO timestamp"),
    until: str | None = Query(None, description="ISO timestamp"),
):
    data = await asyncio.to_thread(
        db.get_summary, source=source, model=model, since=since, until=until,
    )
    data["equivalents"] = equivalents(data["total_co2_grams"])
    return data


@router.get("/daily")
async def daily(
    source: str | None = Query(None),
    model: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
):
    return await asyncio.to_thread(
        db.get_daily_breakdown, source=source, model=model, since=since, until=until,
    )


@router.get("/requests")
async def requests(
    source: str | None = Query(None),
    model: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    return await asyncio.to_thread(
        db.get_requests,
        source=source, model=model, since=since, until=until,
        limit=limit, offset=offset,
    )


@router.get("/equivalents")
async def get_equivalents(
    source: str | None = Query(None),
    model: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
):
    data = await asyncio.to_thread(
        db.get_summary, source=source, model=model, since=since, until=until,
    )
    return equivalents(data["total_co2_grams"])


@router.get("/sources")
async def sources():
    return await asyncio.to_thread(db.get_sources)


@router.get("/live")
async def live():
    data = power_monitor.get_current_power()
    data["dynamic_carbon_intensity"] = bool(
        settings.electricitymap_api_key and settings.electricitymap_zone
    )
    return data


@router.get("/balance")
async def balance():
    data = await asyncio.to_thread(db.get_balance)
    data["offset_provider"] = settings.offset_provider
    return data


@router.get("/offsets")
async def offset_history(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    return await asyncio.to_thread(db.get_offsets, limit, offset)


@router.get("/quote")
async def offset_quote(
    co2_grams: float | None = Query(None, description="CO2 in grams. If omitted, quotes current balance."),
):
    """Get a price quote for offsetting without purchasing."""
    if co2_grams is None:
        bal = await asyncio.to_thread(db.get_balance)
        co2_grams = bal["balance_grams"]

    if co2_grams <= 0:
        return {"co2_grams": 0, "quotes": []}

    quotes = await get_quote(co2_grams)
    return {
        "co2_grams": co2_grams,
        "quotes": [
            {
                "provider": q.provider,
                "co2_grams": q.co2_grams,
                "amount_kg": q.amount_kg,
                "cost_cents": q.cost_cents,
                "currency": q.currency,
            }
            for q in quotes
        ],
    }


@router.post("/offset")
async def manual_offset(
    request: Request,
    co2_grams: float | None = Query(None, description="CO2 to offset in grams. If omitted, offsets current balance."),
):
    """Manually trigger an offset purchase."""
    # Auth check
    if not settings.offset_api_key:
        raise HTTPException(403, "Offset endpoint disabled -- set OFFSET_API_KEY to enable")
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:].strip() != settings.offset_api_key:
        raise HTTPException(401, "Invalid or missing offset API key")

    provider = settings.offset_provider
    if provider not in ("cnaught", "tree-nation", "both"):
        raise HTTPException(400, "No offset provider configured")

    # Check that the selected provider(s) have API keys
    missing = []
    if provider in ("cnaught", "both") and not settings.cnaught_api_key:
        missing.append("CNAUGHT_API_KEY")
    if provider in ("tree-nation", "both") and not settings.tree_nation_api_key:
        missing.append("TREE_NATION_API_KEY")
    if missing:
        raise HTTPException(
            400,
            f"Offset provider configured but missing API keys: {', '.join(missing)}",
        )

    if co2_grams is None:
        bal = await asyncio.to_thread(db.get_balance)
        co2_grams = bal["balance_grams"]

    if co2_grams <= 0:
        return {"message": "Nothing to offset", "balance_grams": 0}

    results = await purchase_offset(co2_grams)

    if not results:
        raise HTTPException(502, "All offset providers failed")

    return {
        "message": f"Offset purchased: {co2_grams:.1f}g CO2",
        "results": [
            {
                "provider": r.provider,
                "co2_grams": r.co2_grams_offset,
                "cost_cents": r.cost_cents,
                "certificate_url": r.certificate_url,
                "order_id": r.order_id,
                "tree_count": r.tree_count,
            }
            for r in results
        ],
    }


@router.get("/auto_offset")
async def auto_offset_get():
    """Current auto-offset status (read-only, no auth)."""
    return await get_auto_offset_status()


@router.post("/auto_offset/toggle")
async def auto_offset_toggle(
    request: Request,
    enabled: bool = Body(..., embed=True),
):
    """Enable/disable auto-offset. Same Bearer auth as /carbon/offset."""
    if not settings.offset_api_key:
        raise HTTPException(403, "Auto-offset toggle disabled -- set OFFSET_API_KEY to enable")
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:].strip() != settings.offset_api_key:
        raise HTTPException(401, "Invalid or missing offset API key")

    await asyncio.to_thread(db.set_kv, "auto_offset_enabled", "true" if enabled else "false")
    if enabled:
        # Fire an immediate tick so the user doesn't wait until the next daily check.
        # Lock-protected and exception-safe to avoid races with the background loop.
        asyncio.create_task(auto_offset.fire_and_forget())
    return await get_auto_offset_status()

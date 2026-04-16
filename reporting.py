import asyncio

from fastapi import APIRouter, Query, HTTPException, Request

import db
from carbon import equivalents
from config import settings
from power import monitor as power_monitor
from offsets import purchase_offset

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
    return power_monitor.get_current_power()


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
    if provider not in ("patch", "tree-nation", "both"):
        raise HTTPException(400, "No offset provider configured")

    # Check that the selected provider(s) have API keys
    missing = []
    if provider in ("patch", "both") and not settings.patch_api_key:
        missing.append("PATCH_API_KEY")
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

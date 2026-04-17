import asyncio
import logging
from datetime import datetime, timezone

import httpx

from config import settings
import db
from energy.carbon import equivalents
from energy.power import monitor as power_monitor

logger = logging.getLogger("carbon-proxy.trmnl")

TRMNL_URL = "https://trmnl.com/api/custom_plugins"


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _build_payload(summary: dict, balance: dict, power: dict) -> dict:
    eq = equivalents(summary.get("total_co2_grams", 0))

    emitted = balance.get("total_co2_grams", 0)
    neutralized = balance.get("total_offset_grams", 0)
    debt = balance.get("balance_grams", 0)
    pct = min(round((neutralized / emitted) * 100) if emitted > 0 else 0, 999)

    return {
        "merge_variables": {
            "total_requests": summary.get("total_requests", 0),
            "total_tokens": _format_tokens(summary.get("total_tokens", 0)),
            "energy_kwh": round(summary.get("total_energy_kwh", 0), 3),
            "co2_grams": round(emitted, 1),
            "neutralized_grams": round(neutralized, 1),
            "debt_grams": round(debt, 1),
            "neutralized_pct": pct,
            "surplus": round(abs(debt), 1) if debt < 0 else 0,
            "is_surplus": debt < 0,
            "is_net_zero": debt == 0 and emitted > 0,
            "trees_planted": balance.get("trees_planted", 0),
            "total_cost": f"${balance.get('total_cost_cents', 0) / 100:.2f}",
            "google_searches": eq.get("google_searches", 0),
            "phone_charges": eq.get("smartphone_charges", 0),
            "trees_to_neutralize": eq.get("trees_to_offset_yearly", 0),
            "power_watts": power.get("total_watts", 0),
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    }


async def _post_to_trmnl(payload: dict):
    url = f"{TRMNL_URL}/{settings.trmnl_plugin_uuid}"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
    logger.info("TRMNL push successful (%d bytes)", len(str(payload)))


async def push_loop():
    logger.info("TRMNL push loop started (every %ds)", settings.trmnl_push_interval)
    while True:
        try:
            summary = await asyncio.to_thread(db.get_summary)
            balance = await asyncio.to_thread(db.get_balance)
            power = power_monitor.get_current_power()
            payload = _build_payload(summary, balance, power)
            await _post_to_trmnl(payload)
        except asyncio.CancelledError:
            logger.info("TRMNL push loop cancelled")
            raise
        except Exception as e:
            logger.warning("TRMNL push failed: %s", e)
        await asyncio.sleep(settings.trmnl_push_interval)

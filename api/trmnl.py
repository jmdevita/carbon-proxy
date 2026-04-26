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

# Rotating equivalency sets — alternates each push cycle
_eq_cycle = 0
_EQ_SETS = [
    lambda eq, green: [
        {"label": "Cars Off Road*" if green else "Cars On Road*", "value": eq.get("cars_per_year", 0)},
        {"label": "Homes Offset*" if green else "Homes Energy*", "value": eq.get("homes_energy_per_year", 0)},
        {"label": "Flights Offset" if green else "Flights LA-NYC", "value": eq.get("flights_la_nyc", 0)},
    ],
    lambda eq, green: [
        {"label": "Trees/Yr Offset" if green else "Trees/Yr Needed", "value": eq.get("trees_to_offset_yearly", 0)},
        {"label": "Charges Offset" if green else "Phone Charges", "value": eq.get("smartphone_charges", 0)},
        {"label": "Searches Offset" if green else "Google Searches", "value": eq.get("google_searches", 0)},
    ],
    lambda eq, green: [
        {"label": "Km Not Driven" if green else "Km Driven", "value": eq.get("km_driven", 0)},
        {"label": "Streaming Offset" if green else "Streaming Hrs", "value": eq.get("streaming_hours", 0)},
        {"label": "Emails Offset" if green else "Emails Sent", "value": eq.get("emails_sent", 0)},
    ],
    lambda eq, green: [
        {"label": "Coffees Offset" if green else "Coffees", "value": eq.get("coffee_cups", 0)},
        {"label": "Kettles Offset" if green else "Kettle Boils", "value": eq.get("kettle_boils", 0)},
        {"label": "Burgers Offset" if green else "Beef Burgers", "value": eq.get("beef_burgers", 0)},
    ],
    lambda eq, green: [
        {"label": "ChatGPT Offset" if green else "ChatGPT Queries", "value": eq.get("chatgpt_queries", 0)},
        {"label": "Laundry Offset" if green else "Laundry Loads", "value": eq.get("laundry_loads", 0)},
        {"label": "Searches Offset" if green else "Google Searches", "value": eq.get("google_searches", 0)},
    ],
]


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _build_payload(summary: dict, balance: dict, power: dict) -> dict:
    global _eq_cycle
    emitted = balance.get("total_co2_grams", 0)
    neutralized = balance.get("total_offset_grams", 0)
    debt = balance.get("balance_grams", 0)
    pct = min(round((neutralized / emitted) * 100) if emitted > 0 else 0, 999)
    is_green = debt < 0
    # When carbon-negative, equivalents represent the surplus offset (not emissions)
    eq = equivalents(abs(debt) if is_green else emitted)

    # Rotating equivalency set
    eq_set = _EQ_SETS[_eq_cycle % len(_EQ_SETS)](eq, is_green)
    _eq_cycle += 1

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
            "cars_per_year": eq.get("cars_per_year", 0),
            "homes_energy_per_year": eq.get("homes_energy_per_year", 0),
            "flights_la_nyc": eq.get("flights_la_nyc", 0),
            "eq1_label": eq_set[0]["label"],
            "eq1_value": eq_set[0]["value"],
            "eq2_label": eq_set[1]["label"],
            "eq2_value": eq_set[1]["value"],
            "eq3_label": eq_set[2]["label"],
            "eq3_value": eq_set[2]["value"],
            "dynamic_carbon_intensity": bool(
                settings.electricitymap_api_key and settings.electricitymap_zone
            ),
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

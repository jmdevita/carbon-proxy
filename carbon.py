import time
import logging

import httpx

from config import settings

logger = logging.getLogger("carbon-proxy.carbon")

# Cached carbon intensity from electricityMap
_cached_intensity: float | None = None
_cache_time: float = 0
_CACHE_TTL = 1800  # 30 minutes


def joules_to_kwh(joules: float) -> float:
    return joules / 3_600_000


def calculate_co2(energy_kwh: float, intensity_g_per_kwh: float | None = None) -> float:
    if intensity_g_per_kwh is None:
        intensity_g_per_kwh = get_carbon_intensity()
    return energy_kwh * intensity_g_per_kwh


def get_carbon_intensity() -> float:
    """Get current carbon intensity, using cached electricityMap value if available.

    Safe to call from any thread -- the HTTP fetch runs synchronously but is
    only triggered when the cache expires (every 30 min).  When called from
    an async context, proxy.py wraps the whole _log_request chain in
    asyncio.to_thread via the BackgroundTask so this won't block the loop.
    """
    global _cached_intensity, _cache_time

    # Try electricityMap if configured
    if settings.electricitymap_api_key and settings.electricitymap_zone:
        now = time.monotonic()
        if _cached_intensity is not None and (now - _cache_time) < _CACHE_TTL:
            return _cached_intensity

        try:
            intensity = _fetch_electricitymap_intensity()
            _cached_intensity = intensity
            _cache_time = now
            logger.info("Updated carbon intensity from electricityMap: %.0f gCO2/kWh", intensity)
            return intensity
        except Exception as e:
            logger.warning("Failed to fetch electricityMap data: %s, using static value", e)

    return settings.carbon_intensity


def _fetch_electricitymap_intensity() -> float:
    """Fetch carbon intensity from electricityMap API.

    Uses synchronous httpx -- callers must run this in a thread to avoid
    blocking the event loop.
    """
    resp = httpx.get(
        "https://api.electricitymap.org/v3/carbon-intensity/latest",
        params={"zone": settings.electricitymap_zone},
        headers={"auth-token": settings.electricitymap_api_key},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return float(data["carbonIntensity"])


# Offset equivalents
# Sources: EPA, IEA
TREE_CO2_KG_PER_YEAR = 22.0     # 1 tree absorbs ~22kg CO2/year
CAR_CO2_G_PER_KM = 120.0        # average car
SMARTPHONE_CO2_G = 8.0           # per charge cycle
STREAMING_CO2_G_PER_HOUR = 36.0  # video streaming


def equivalents(co2_grams: float) -> dict:
    co2_kg = co2_grams / 1000
    return {
        "co2_grams": round(co2_grams, 4),
        "co2_kg": round(co2_kg, 6),
        "trees_to_offset_yearly": round(co2_kg / TREE_CO2_KG_PER_YEAR, 6),
        "km_driven": round(co2_grams / CAR_CO2_G_PER_KM, 2),
        "smartphone_charges": round(co2_grams / SMARTPHONE_CO2_G, 2),
        "streaming_hours": round(co2_grams / STREAMING_CO2_G_PER_HOUR, 2),
    }

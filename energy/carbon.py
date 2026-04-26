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
# Each constant is paired with a citation. Where ranges exist, midpoints are used.
TREE_CO2_KG_PER_YEAR = 22.0      # EPA: ~22 kg CO2 absorbed per mature tree per year
CAR_CO2_G_PER_KM = 120.0         # EPA: average passenger car tailpipe emissions
SMARTPHONE_CO2_G = 8.0           # EPA: ~8 g CO2 per smartphone charge cycle
STREAMING_CO2_G_PER_HOUR = 36.0  # IEA / Carbon Trust: HD video streaming
GOOGLE_SEARCH_CO2_G = 0.2        # Google/EPA: ~0.2 g CO2 per search query
CAR_CO2_KG_PER_YEAR = 4600.0     # EPA: avg passenger vehicle annual emissions
HOME_CO2_KG_PER_YEAR = 7440.0    # EPA: avg US home annual energy emissions
FLIGHT_LA_NYC_CO2_KG = 403.0     # EPA: one-way passenger emissions LA-NYC
EMAIL_CO2_G = 4.0                # Berners-Lee, How Bad Are Bananas? (2020): typical email
COFFEE_CUP_CO2_G = 71.0          # UK Carbon Trust: 200 ml filter coffee, no milk (60-80 g range)
KETTLE_BOIL_CO2_G = 70.0         # ~70 g CO2 per 1 L boiled, UK grid avg, ~80% kettle efficiency
LAUNDRY_LOAD_CO2_KG = 2.4        # co2everything / Energy Saving Trust: warm wash + tumble dry
BEEF_BURGER_CO2_KG = 9.73        # Poore & Nemecek (2018, Science): 4 oz beef-herd patty
# Cloud LLM query: Epoch AI Feb 2025 (~0.3 Wh/query GPT-4o) and Altman Jun 2025 (0.34 Wh)
# converge near ~0.3 Wh. At a typical grid intensity this implies ~0.1-0.3 g CO2e;
# 0.3 g is a conservative upper-bound rounded value. (10x lower than 2023-era 3 g estimates.)
CHATGPT_QUERY_CO2_G = 0.3


def equivalents(co2_grams: float) -> dict:
    co2_kg = co2_grams / 1000
    return {
        "co2_grams": round(co2_grams, 4),
        "co2_kg": round(co2_kg, 6),
        "trees_to_offset_yearly": round(co2_kg / TREE_CO2_KG_PER_YEAR, 6),
        "km_driven": round(co2_grams / CAR_CO2_G_PER_KM, 2),
        "smartphone_charges": round(co2_grams / SMARTPHONE_CO2_G, 2),
        "streaming_hours": round(co2_grams / STREAMING_CO2_G_PER_HOUR, 2),
        "google_searches": round(co2_grams / GOOGLE_SEARCH_CO2_G, 1),
        "cars_per_year": round(co2_kg / CAR_CO2_KG_PER_YEAR, 2),
        "homes_energy_per_year": round(co2_kg / HOME_CO2_KG_PER_YEAR, 2),
        "flights_la_nyc": round(co2_kg / FLIGHT_LA_NYC_CO2_KG, 2),
        "emails_sent": round(co2_grams / EMAIL_CO2_G, 1),
        "coffee_cups": round(co2_grams / COFFEE_CUP_CO2_G, 2),
        "kettle_boils": round(co2_grams / KETTLE_BOIL_CO2_G, 2),
        "laundry_loads": round(co2_kg / LAUNDRY_LOAD_CO2_KG, 2),
        "beef_burgers": round(co2_kg / BEEF_BURGER_CO2_KG, 3),
        "chatgpt_queries": round(co2_grams / CHATGPT_QUERY_CO2_G, 1),
    }

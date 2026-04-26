import functools
from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    upstream_url: str = "http://localhost:8080"
    listen_port: int = 8080

    # Carbon intensity
    carbon_intensity: float = 400.0  # gCO2/kWh static fallback
    electricitymap_api_key: str = ""
    electricitymap_zone: str = ""  # e.g. "US-CAL-CISO"

    # Storage
    sqlite_path: str = "/data/carbon.db"

    # API keys: comma-separated key:name pairs (e.g. "sk-webui:openwebui,sk-claude:claude-code")
    api_keys: str = ""

    # Power sensor paths
    rapl_path: str = "/sys/class/powercap/intel-rapl:0/energy_uj"
    gpu_power_path: str = "/host/hwmon/power1_average"
    power_sample_hz: int = 10

    # TDP fallback (watts) -- auto-detected from chip model if not set
    tdp_cpu_watts: float = 0.0
    tdp_gpu_watts: float = 0.0

    # Offset purchasing
    # Provider: "cnaught", "tree-nation", or "both"
    offset_provider: str = "cnaught"
    # Required bearer token for POST /carbon/offset. If empty, endpoint is disabled.
    offset_api_key: str = ""

    # CNaught API (https://cnaught.com)
    cnaught_api_key: str = ""
    cnaught_base_url: str = "https://api.cnaught.com/v1"
    cnaught_portfolio_id: str = ""  # optional, uses default portfolio if empty

    # Tree-Nation API (https://tree-nation.com)
    tree_nation_api_key: str = ""
    tree_nation_planter_id: int = 0
    tree_nation_species_id: int = 0  # optional, cheapest if 0
    tree_nation_base_url: str = "https://tree-nation.com/api"

    # TRMNL e-ink display
    trmnl_enabled: bool = False
    trmnl_plugin_uuid: str = ""
    trmnl_push_interval: int = 300  # seconds

    # Auto-offset (opt-in, runtime-toggleable via dashboard; these are defaults/limits)
    auto_offset_default_enabled: bool = False
    auto_offset_daily_cap_cents: int = 100  # $1.00/day
    auto_offset_check_interval_s: int = 86400  # once per day
    auto_offset_min_purchase_grams: int = 1000  # don't buy less than 1 kg at a time

    @field_validator("power_sample_hz")
    @classmethod
    def validate_sample_hz(cls, v):
        if v < 1:
            return 1
        if v > 100:
            return 100
        return v

    @field_validator("tdp_cpu_watts", "tdp_gpu_watts", "carbon_intensity")
    @classmethod
    def validate_non_negative(cls, v):
        return max(0.0, v)

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()


@functools.lru_cache(maxsize=1)
def get_api_key_map() -> dict[str, str]:
    if not settings.api_keys:
        return {}
    result = {}
    for pair in settings.api_keys.split(","):
        pair = pair.strip()
        if ":" in pair:
            key, name = pair.split(":", 1)
            result[key.strip()] = name.strip()
    return result

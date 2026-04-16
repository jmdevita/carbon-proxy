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
    # Provider: "patch", "tree-nation", "both"
    offset_provider: str = "patch"
    # Required bearer token for POST /carbon/offset. If empty, endpoint is disabled.
    offset_api_key: str = ""

    # Patch API
    patch_api_key: str = ""
    patch_project_id: str = ""  # optional, auto-select if empty

    # Tree-Nation API
    tree_nation_api_key: str = ""
    tree_nation_planter_id: int = 0
    tree_nation_species_id: int = 0  # optional, cheapest if 0
    tree_nation_base_url: str = "https://tree-nation.com/api"

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

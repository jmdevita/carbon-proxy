# Carbon Proxy

Carbon footprint tracking for self-hosted LLMs.

Inspired by [CodeCarbon](https://github.com/mlco2/codecarbon), but designed specifically as a lightweight drop-in proxy for local LLM servers. Instead of instrumenting your application code, Carbon Proxy sits between your clients and your LLM backend -- just swap the port and every request is tracked automatically.

```
Clients (Open WebUI, Claude Code, curl, etc.)
    │
    │  :11434 (or whatever port your LLM server was on)
    ▼
Carbon Proxy ──────► LLM Backend (llama-swap, Ollama, vLLM, etc.)
    │                     :8080 (internal)
    ▼
 SQLite + Dashboard
```

Your clients keep hitting the same endpoint as before -- Carbon Proxy forwards everything transparently while logging energy use, CO2 emissions, and token counts per client and model.

## Features

- **Transparent proxy** -- zero config changes for clients, just point them at Carbon Proxy instead of the backend
- **Real power measurement** -- reads CPU power via RAPL (Intel/AMD), GPU power via NVML (NVIDIA), hwmon (AMD/Intel Arc), or macOS powermetrics
- **TDP estimation fallback** -- auto-detects CPU model from a 2,000+ entry database (sourced from CodeCarbon) when hardware sensors aren't available
- **Per-client attribution** -- identifies requests by API key, `X-Carbon-Source` header, `user` field, or User-Agent
- **Carbon intensity** -- static gCO2/kWh value or real-time data from electricityMap API
- **Carbon offset purchasing** -- integrated with Patch.io and Tree-Nation APIs
- **Web dashboard** -- live power charts, daily breakdown, CO2 equivalents
- **REST API** -- query summaries, daily breakdowns, individual requests, and more

## Setup

### Docker (recommended)

```yaml
# docker-compose.yml
carbon-proxy:
  build: ./carbon-proxy
  ports:
    - "11434:8080"
  environment:
    - UPSTREAM_URL=http://your-llm-backend:8080
  volumes:
    - ./carbon-proxy/data:/data
    - /sys/class/powercap:/sys/class/powercap:ro    # CPU power (RAPL)
    - /sys/class/drm:/sys/class/drm:ro              # GPU power (AMD/Intel hwmon)
  privileged: true    # Required to read hardware power sensors
  restart: unless-stopped
```

```bash
docker compose up -d
```

> **Why `privileged: true`?** RAPL energy counters and GPU hwmon sensors require elevated access. Without it, Carbon Proxy falls back to TDP estimation. To avoid privileged mode, grant read access on the host instead:
> ```bash
> sudo chmod a+r /sys/class/powercap/intel-rapl:*/energy_uj
> ```

### Standalone

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

UPSTREAM_URL=http://localhost:11434 uvicorn main:app --host 0.0.0.0 --port 8080
```

Then point your clients at Carbon Proxy instead of the backend directly.

### Platform Notes

| Platform | Power measurement | Docker `UPSTREAM_URL` | Notes |
|---|---|---|---|
| **Linux** | RAPL (CPU) + hwmon/NVML (GPU) | `http://your-backend:8080` | Best accuracy. Mount `/sys/class/powercap` and `/sys/class/drm` as volumes. |
| **macOS** | powermetrics (standalone) or TDP estimation (Docker) | `http://host.docker.internal:11434` | For real readings, run standalone and add passwordless sudo for `/usr/bin/powermetrics`. |
| **Windows** | TDP estimation only | `http://host.docker.internal:11434` (WSL2) | CPU model auto-detected from registry. RAPL may work in WSL2 if your kernel exposes it. |

On all platforms, Carbon Proxy auto-detects your CPU model from a 2,000+ entry TDP database as a fallback when hardware sensors aren't available.

### Verify it's working

```bash
curl http://localhost:8080/health              # Health check
curl http://localhost:8080/carbon/live          # Power detection status
open http://localhost:8080/dashboard            # Web dashboard
```

## Configuration

All settings are via environment variables (or `.env` file). See `.env.example` for the full list.

| Variable | Default | Description |
|---|---|---|
| `UPSTREAM_URL` | `http://localhost:8080` | Backend LLM server URL |
| `LISTEN_PORT` | `8080` | Port to listen on |
| `CARBON_INTENSITY` | `400` | Static carbon intensity (gCO2/kWh) |
| `ELECTRICITYMAP_API_KEY` | | Optional: real-time carbon intensity |
| `ELECTRICITYMAP_ZONE` | | e.g. `US-CAL-CISO` (required if using electricityMap) |
| `SQLITE_PATH` | `/data/carbon.db` | Database file path |
| `API_KEYS` | | Comma-separated `key:name` pairs for client identification |
| `POWER_SAMPLE_HZ` | `10` | Power sampling frequency |
| `TDP_CPU_WATTS` | auto | CPU TDP override (auto-detected if not set) |
| `TDP_GPU_WATTS` | auto | GPU TDP override (auto-detected if not set) |

### Client Identification

Clients are identified in this priority order:

1. **API key** -- `Authorization: Bearer sk-xxx` mapped via `API_KEYS` env var
2. **Custom header** -- `X-Carbon-Source: my-app` (client self-identifies, no server config needed)
3. **User field** -- `"user"` field in the request body
4. **User-Agent** -- first segment of the UA string

### Power Measurement

Carbon Proxy uses a 3-tier approach:

| Tier | Method | Accuracy | Platforms |
|---|---|---|---|
| Measured | RAPL, NVML, hwmon, powermetrics | High | Linux (Intel/AMD), NVIDIA GPU, macOS |
| Estimated | TDP lookup table | Medium | All (2,000+ CPUs, Apple Silicon) |
| None | No measurement | -- | Fallback when nothing is available |

Each request is tagged with its `power_source` field (`measured`, `estimated`, or `none`) so you know the data quality.

## API Endpoints

### Proxy

All requests are forwarded transparently to the upstream backend. POST requests are instrumented to extract token usage.

### Reporting

| Endpoint | Description |
|---|---|
| `GET /carbon/summary` | Aggregate totals (energy, CO2, tokens, equivalents) |
| `GET /carbon/daily` | Daily breakdown by source |
| `GET /carbon/requests` | Individual request log (paginated) |
| `GET /carbon/equivalents` | CO2 equivalents (trees, phone charges, Google searches) |
| `GET /carbon/sources` | List of known client sources |
| `GET /carbon/live` | Current power draw (latest sample) |
| `GET /carbon/balance` | Emissions vs offsets balance |
| `GET /carbon/offsets` | Offset purchase history |
| `POST /carbon/offset` | Trigger manual offset purchase (requires auth) |

All GET endpoints support optional query parameters: `source`, `model`, `since`, `until`.

### Dashboard

`GET /dashboard` -- web UI with live power charts, gauges, daily breakdown, and CO2 equivalents.

## Carbon Offsets

Carbon Proxy can purchase carbon offsets to balance your LLM emissions. Two providers are supported:

| Provider | What it does | Pricing |
|---|---|---|
| [Patch.io](https://patch.io) | Purchases verified carbon credits | Pay-per-gram, varies by project |
| [Tree-Nation](https://tree-nation.com) | Plants real trees | Pre-funded credits on your account |

### Setup

1. Sign up with your chosen provider and get an API key
2. Set an `OFFSET_API_KEY` -- this protects the offset endpoint so only you can trigger purchases
3. Configure the provider:

```env
OFFSET_PROVIDER=patch          # "patch", "tree-nation", or "both"
OFFSET_API_KEY=your-secret     # Required to enable POST /carbon/offset

# Patch.io
PATCH_API_KEY=your-patch-key
PATCH_PROJECT_ID=              # Optional, auto-selects if empty

# Tree-Nation
TREE_NATION_API_KEY=your-key
TREE_NATION_PLANTER_ID=12345   # Your planter account ID
TREE_NATION_SPECIES_ID=0       # Optional, cheapest species if 0
```

### Usage

```bash
# Offset your current balance
curl -X POST http://localhost:8080/carbon/offset \
  -H "Authorization: Bearer your-secret"

# Offset a specific amount
curl -X POST "http://localhost:8080/carbon/offset?co2_grams=500" \
  -H "Authorization: Bearer your-secret"

# Check your emissions vs offsets balance
curl http://localhost:8080/carbon/balance
```

## Acknowledgments

This project is inspired by [CodeCarbon](https://github.com/mlco2/codecarbon), which tracks carbon emissions from compute workloads. Carbon Proxy takes a different approach -- rather than instrumenting application code, it works as a transparent network proxy, making it ideal for self-hosted LLM setups where you may have multiple clients and don't want to modify any of them.

The CPU TDP lookup table (`data/cpu_power.csv`) is sourced from CodeCarbon under the Apache 2.0 license.

## License

MIT

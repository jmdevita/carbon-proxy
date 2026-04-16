# TRMNL Plugin Integration

Carbon footprint dashboard for the [TRMNL](https://usetrmnl.com/) e-ink display.

## Approach: Private Plugin (Webhook)

Carbon-proxy pushes summary data to TRMNL's cloud on a schedule. No public endpoints needed -- the server initiates all connections outbound.

```
carbon-proxy (background task)
    |
    |  POST every 5 min
    v
https://usetrmnl.com/api/custom_plugins/{plugin_uuid}
    |
    |  renders Liquid template -> e-ink image
    v
TRMNL device
```

## How It Works

1. A background task runs inside carbon-proxy every 5 minutes
2. It pulls summary data from `db.get_summary()` and `db.get_daily_breakdown()`
3. Formats a compact JSON payload (under 2KB)
4. POSTs it to TRMNL's webhook endpoint
5. TRMNL renders the data using a Liquid template into an 800x480 black-and-white image
6. The TRMNL device picks it up on its next playlist cycle

## Configuration

New environment variables:

| Variable | Required | Description |
|---|---|---|
| `TRMNL_PLUGIN_UUID` | Yes | Plugin UUID from TRMNL dashboard (acts as auth token) |
| `TRMNL_PUSH_INTERVAL` | No | Push interval in seconds (default: 300) |
| `TRMNL_ENABLED` | No | Enable/disable the push task (default: false) |

## Webhook Payload

POST to `https://usetrmnl.com/api/custom_plugins/{TRMNL_PLUGIN_UUID}`

Headers:
```
Content-Type: application/json
```

Body (must be under 2KB):
```json
{
  "merge_variables": {
    "total_requests": 142,
    "total_tokens": "1.2M",
    "energy_kwh": 0.083,
    "co2_grams": 32.4,
    "google_searches": 162,
    "phone_charges": 4,
    "trees": 0.001,
    "power_watts": 45.2,
    "power_source": "measured",
    "carbon_intensity": 400,
    "daily": [
      {"date": "Apr 14", "co2": 10.5},
      {"date": "Apr 15", "co2": 12.1},
      {"date": "Apr 16", "co2": 9.8}
    ],
    "updated_at": "2026-04-16T14:30:00Z"
  }
}
```

## Liquid Template (TRMNL side)

Designed for 800x480px black-and-white e-ink. Uses TRMNL's Framework UI CSS.

```html
<link rel="stylesheet" href="https://trmnl.com/css/latest/plugins.css">

<div class="view view--full">
  <div class="layout layout--col">
    <div class="columns">
      <div class="column">
        <h2>Carbon Proxy</h2>
        <p class="label">Updated {{ updated_at | date: "%b %d, %H:%M" }}</p>
      </div>
    </div>

    <div class="columns">
      <div class="column">
        <span class="value value--large">{{ co2_grams }}g</span>
        <p class="label">CO2 emitted</p>
      </div>
      <div class="column">
        <span class="value">{{ energy_kwh }} kWh</span>
        <p class="label">Energy used</p>
      </div>
      <div class="column">
        <span class="value">{{ total_requests }}</span>
        <p class="label">Requests</p>
      </div>
    </div>

    <div class="columns">
      <div class="column">
        <span class="value">{{ google_searches }}</span>
        <p class="label">Google searches equiv.</p>
      </div>
      <div class="column">
        <span class="value">{{ phone_charges }}</span>
        <p class="label">Phone charges equiv.</p>
      </div>
      <div class="column">
        <span class="value">{{ power_watts }}W</span>
        <p class="label">Current draw</p>
      </div>
    </div>

    <div class="columns">
      <div class="column">
        <p class="label">Daily CO2 (last 7 days)</p>
        <table>
          <tr>
            {% for day in daily %}
            <td class="label">{{ day.date }}</td>
            {% endfor %}
          </tr>
          <tr>
            {% for day in daily %}
            <td>{{ day.co2 }}g</td>
            {% endfor %}
          </tr>
        </table>
      </div>
    </div>
  </div>
</div>
```

## Implementation Plan

### 1. Add config fields (`config.py`)

```python
trmnl_enabled: bool = False
trmnl_plugin_uuid: str = ""
trmnl_push_interval: int = 300  # seconds
```

### 2. Create `trmnl.py` module

- Background async task using `asyncio.create_task` in lifespan
- Pulls data from existing DB functions (no new queries needed)
- Formats payload, POSTs via `httpx.AsyncClient`
- Logs success/failure, respects `trmnl_enabled` flag
- Graceful shutdown via cancellation in lifespan teardown

```python
# Rough structure
async def _push_loop():
    while True:
        try:
            summary = await asyncio.to_thread(db.get_summary)
            daily = await asyncio.to_thread(db.get_daily_breakdown)
            power = power_monitor.get_current_power()
            payload = _build_payload(summary, daily, power)
            await _post_to_trmnl(payload)
        except Exception as e:
            logger.warning("TRMNL push failed: %s", e)
        await asyncio.sleep(settings.trmnl_push_interval)
```

### 3. Wire into lifespan (`main.py`)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... existing startup ...
    trmnl_task = None
    if settings.trmnl_enabled and settings.trmnl_plugin_uuid:
        trmnl_task = asyncio.create_task(trmnl.push_loop())
        logger.info("TRMNL push enabled (every %ds)", settings.trmnl_push_interval)
    yield
    if trmnl_task:
        trmnl_task.cancel()
    # ... existing shutdown ...
```

### 4. Add env vars to Docker Compose

```yaml
carbon-proxy:
  environment:
    - TRMNL_ENABLED=true
    - TRMNL_PLUGIN_UUID=your-uuid-here
    - TRMNL_PUSH_INTERVAL=300
```

## TRMNL Setup

1. Log into [usetrmnl.com](https://usetrmnl.com) dashboard
2. Create a new Private Plugin
3. Set data strategy to **Webhook**
4. Copy the plugin UUID from the settings page
5. Paste the Liquid template into the markup editor
6. Add the UUID to your carbon-proxy env vars
7. Enable and deploy

## Constraints

- **Payload size**: 2KB max (5KB for TRMNL+ subscribers)
- **Rate limit**: 12 POSTs/hour standard, 30/hour for TRMNL+
- **Display**: 800x480px, black and white only, no animations
- **Rendering**: TRMNL skips re-render if merge variables unchanged between pushes

## Security

- No public endpoints exposed -- all connections are outbound
- Plugin UUID is the auth token -- treat it as a secret
- Only aggregate stats are sent (request counts, CO2, energy) -- no prompts or responses
- Store UUID in env var, never commit to repo

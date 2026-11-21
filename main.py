import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager

# Configure logging before any other imports so detection logs are visible
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("carbon-proxy")

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from config import settings, kv_is_configured
from core.proxy import router as proxy_router, init_client, close_client
from api.reporting import router as reporting_router
from api.api_keys import router as api_keys_router, reconciler_loop as kv_reconciler_loop
import db
from api import trmnl, auto_offset
from integrations import cloudflare_kv
from energy.power import monitor as power_monitor


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting carbon-proxy, upstream=%s", settings.upstream_url)
    db.init_db()
    init_client()
    cloudflare_kv.init_client()
    power_monitor.start()

    trmnl_task = None
    if settings.trmnl_enabled and settings.trmnl_plugin_uuid:
        trmnl_task = asyncio.create_task(trmnl.push_loop())
        logger.info("TRMNL push enabled (every %ds)", settings.trmnl_push_interval)

    auto_offset_task = asyncio.create_task(auto_offset.push_loop())

    kv_reconciler_task = None
    if kv_is_configured():
        kv_reconciler_task = asyncio.create_task(kv_reconciler_loop())

    yield

    if trmnl_task:
        trmnl_task.cancel()
    if kv_reconciler_task:
        kv_reconciler_task.cancel()
    auto_offset_task.cancel()
    power_monitor.stop()
    await close_client()
    await cloudflare_kv.close_client()
    db.close_db()
    logger.info("Carbon proxy shut down")


app = FastAPI(title="Carbon Proxy", lifespan=lifespan)


STATIC_DIR = Path(__file__).parent / "static"

# Force the browser to revalidate dashboard assets on every request.
# Responses already carry ETag and Last-Modified, and StaticFiles answers an
# unchanged file with a bodiless 304; without this header browsers fall back
# to heuristic caching and can serve stale JS for hours after a deploy.
# Set on these responses directly rather than in middleware, which would wrap
# every proxied LLM stream to tag two routes.
REVALIDATE = {"Cache-Control": "no-cache"}


class RevalidatedStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(REVALIDATE)
        return response


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return RedirectResponse("/dashboard")


@app.get("/dashboard")
async def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html", media_type="text/html", headers=REVALIDATE)


# Serve static assets (favicon, etc.)
app.mount("/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="static")

# Mount reporting + api_keys routes before proxy so /carbon/* takes priority
app.include_router(reporting_router)
app.include_router(api_keys_router)

# Mount proxy routes last (catch-all)
app.include_router(proxy_router)

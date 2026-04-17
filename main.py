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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from core.proxy import router as proxy_router, init_client, close_client
from api.reporting import router as reporting_router
import db
from api import trmnl
from energy.power import monitor as power_monitor


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting carbon-proxy, upstream=%s", settings.upstream_url)
    db.init_db()
    init_client()
    power_monitor.start()

    trmnl_task = None
    if settings.trmnl_enabled and settings.trmnl_plugin_uuid:
        trmnl_task = asyncio.create_task(trmnl.push_loop())
        logger.info("TRMNL push enabled (every %ds)", settings.trmnl_push_interval)

    yield

    if trmnl_task:
        trmnl_task.cancel()
    power_monitor.stop()
    await close_client()
    db.close_db()
    logger.info("Carbon proxy shut down")


app = FastAPI(title="Carbon Proxy", lifespan=lifespan)


STATIC_DIR = Path(__file__).parent / "static"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/dashboard")


@app.get("/dashboard")
async def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html", media_type="text/html")


# Serve static assets (favicon, etc.)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Mount reporting routes before proxy so /carbon/* takes priority
app.include_router(reporting_router)

# Mount proxy routes last (catch-all)
app.include_router(proxy_router)

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import (
    apikeys,
    calendar,
    import_lists,
    library,
    library_import,
    queue,
    search,
    series,
    settings,
    system,
)
from .api.deps import get_api_key, require_api_access
from .browser_setup import load_browser_login
from .config import config
from .db import init_db
from .jobs import scheduler
from .security import access_boundary

logging.basicConfig(
    level=config.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
# HTTP client request URLs/debug transport records may contain integration
# credentials (notably Kavita's required apiKey query parameter).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("mangarr")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.auth_username and load_browser_login() is None:
        if config.allow_unauthenticated_browser:
            log.warning("Browser login is disabled; reachable clients can obtain API access. Set MANGARR_AUTH_USERNAME and MANGARR_AUTH_PASSWORD to enable login.")
        else:
            log.info("Open Mangarr on a trusted network to create the browser login.")
    await init_db()
    await scheduler.start()
    log.info("Mangarr %s ready on %s:%d", __version__, config.host, config.port)
    yield
    scheduler.shutdown()


app = FastAPI(title="Mangarr", version=__version__, lifespan=lifespan)
app.middleware("http")(access_boundary)

api = FastAPI(dependencies=[Depends(require_api_access)])
api.include_router(series.router)
api.include_router(library_import.router)
api.include_router(library.router)
api.include_router(calendar.router)
api.include_router(import_lists.router)
api.include_router(search.router)
api.include_router(queue.router)
api.include_router(settings.router)
api.include_router(apikeys.router)
api.include_router(system.router)
app.mount("/api/v1", api)


@app.get("/initialize.json")
async def initialize(request: Request):
    """Browser bootstrap, protected by the browser access boundary."""
    mode = "managed" if request.state.browser_session else "environment" if config.auth_username else "external"
    return {"apiKey": "" if mode == "managed" else get_api_key(), "authMode": mode,
            "version": __version__, "urlBase": ""}


@app.get("/ping")
async def ping():
    """Liveness probe for the Docker HEALTHCHECK and uptime monitors, like
    the *arr apps' /ping. Public on purpose (it sits outside /api/v1, so no
    X-Api-Key) and deliberately cheap: no database, no sources, no secrets."""
    return {"status": "OK"}


# Serve the built frontend if present (production/Docker)
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        if full_path:
            # resolve + containment check: the route param is percent-decoded,
            # so "..%2f" sequences would otherwise escape the static dir
            candidate = (STATIC_DIR / full_path).resolve()
            if candidate.is_relative_to(STATIC_DIR) and candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
else:

    @app.get("/")
    async def root():
        return JSONResponse({"app": "mangarr", "version": __version__, "ui": "not built"})


def run() -> None:
    import uvicorn

    uvicorn.run(
        "mangarr.main:app", host=config.host, port=config.port, log_level="info",
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", ""),
    )


if __name__ == "__main__":
    run()

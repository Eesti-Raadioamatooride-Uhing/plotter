"""Plotter application entry point."""
from __future__ import annotations

import hashlib
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .api import jobs
from .api.routes import router
from .config import settings
from .core.data import db
from .core.terrain import providers
from .core.terrain.providers import Terrain

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.ensure_dirs()
    db.init(settings.database_url)
    app.state.terrain = Terrain.from_settings(settings)
    # Coverage results live on disk, not in this worker's memory: several
    # uvicorn workers answer the same browser, and the image request rarely
    # lands on the worker that computed it.
    app.state.coverage_runs = jobs.Store(settings.data_dir)
    app.state.coverage_runs.reap_stale()
    log = logging.getLogger(__name__)
    log.info("terrain providers: %s",
             ", ".join(p.name for p in app.state.terrain.providers))
    if not providers.HAVE_RASTERIO:
        # Every elevation read returns nodata, which becomes 0 m, so the app
        # would quietly model the whole country as a flat sea. Say so loudly:
        # a wrong number that looks plausible is worse than no number.
        log.error("rasterio did not load (%s). NO TERRAIN will be read: every "
                  "elevation reads as 0 m and every propagation result is "
                  "fiction. On Debian slim images this is usually a missing "
                  "libexpat1.", providers.RASTERIO_ERROR)
    if settings.refresh_on_start:
        from .core.data import masts
        try:
            masts.run(settings)
        except Exception as e:
            logging.getLogger(__name__).warning("mast refresh failed: %s", e)
    yield


app = FastAPI(title="Plotter",
              description="Antenna deployment planning for Estonia and Finland, "
                          "HF through microwave.",
              version="1.0.0", lifespan=lifespan,
              root_path=settings.base_path,
              # Don't publish the interactive API surface on a public deploy.
              docs_url="/docs" if settings.enable_docs else None,
              redoc_url="/redoc" if settings.enable_docs else None,
              openapi_url="/openapi.json" if settings.enable_docs else None)

if settings.cors_origins:
    app.add_middleware(CORSMiddleware,
                       allow_origins=[o.strip() for o in
                                      settings.cors_origins.split(",")],
                       allow_methods=["*"], allow_headers=["*"])

# Content-Security-Policy allows Leaflet's CSS/JS from cdnjs and map tiles from
# any https host (they come from OSM/Maa-amet/MML/Esri), inline styles (Leaflet
# and the UI set element styles), and same-origin for everything else. No
# inline scripts are used, so script-src stays strict.
_CSP = ("default-src 'self'; "
        "script-src 'self' https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; font-src 'self' data:; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'")
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Strict-Transport-Security": "max-age=31536000",
}


@app.middleware("http")
async def _harden(request, call_next):
    # Reject oversized bodies up front, before any parsing/work.
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > settings.max_body_bytes:
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("request body too large", status_code=413)
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if request.url.path.startswith("/static/"):
        # A stamped URL names one exact build of the file, so it can be cached
        # hard. An unstamped one has to be revalidated, or the browser is free
        # to invent its own expiry and serve last week's JavaScript.
        response.headers.setdefault(
            "Cache-Control",
            "public, max-age=31536000, immutable"
            if request.query_params.get("v") else "no-cache")
    return response


app.include_router(router, prefix="/api")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# index.html references its own CSS and JS as "/static/app.js?v=3". A hand
# written version like that has to be remembered on every edit, and when it is
# not, browsers keep serving the previous file: nothing has no-store on it and
# StaticFiles sends no Cache-Control, so a browser is free to reuse a cached
# copy without revalidating. The visible result is a page whose HTML is new and
# whose JS is old, where only the newest controls are dead. So stamp the query
# with a hash of the file that is actually on disk, and never cache the HTML
# that carries those stamps.
_ASSET_RE = re.compile(r'(?P<attr>(?:src|href)=")/static/(?P<file>[\w.-]+)\?v=[^"]*"')
_ASSET_VERSIONS: dict[str, tuple[tuple[int, int], str]] = {}


def _asset_version(name: str) -> str:
    if name.startswith(".") or "/" in name:
        return "0"
    path = STATIC / name
    try:
        stat = path.stat()
    except OSError:
        return "0"
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _ASSET_VERSIONS.get(name)
    if cached and cached[0] == key:
        return cached[1]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    _ASSET_VERSIONS[name] = (key, digest)
    return digest


def _render_index() -> str:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return _ASSET_RE.sub(
        lambda m: f'{m["attr"]}/static/{m["file"]}?v={_asset_version(m["file"])}"',
        html)


@app.get("/", include_in_schema=False)
def index():
    return HTMLResponse(_render_index(),
                        headers={"Cache-Control": "no-cache"})


def main() -> None:
    import uvicorn
    uvicorn.run("plotter.main:app", host=settings.host, port=settings.port,
                log_level=settings.log_level)


if __name__ == "__main__":
    main()

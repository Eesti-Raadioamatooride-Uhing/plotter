"""Plotter application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .config import settings
from .core.data import db
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
    logging.getLogger(__name__).info(
        "terrain providers: %s",
        ", ".join(p.name for p in app.state.terrain.providers))
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
    return response


app.include_router(router, prefix="/api")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


def main() -> None:
    import uvicorn
    uvicorn.run("plotter.main:app", host=settings.host, port=settings.port,
                log_level=settings.log_level)


if __name__ == "__main__":
    main()

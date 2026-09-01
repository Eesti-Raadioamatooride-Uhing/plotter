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
              root_path=settings.base_path)

if settings.cors_origins:
    app.add_middleware(CORSMiddleware,
                       allow_origins=[o.strip() for o in
                                      settings.cors_origins.split(",")],
                       allow_methods=["*"], allow_headers=["*"])

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

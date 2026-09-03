# Plotter application container. Serves the FastAPI/uvicorn app on :8000 as an
# unprivileged user, reached only via the Apache+ModSecurity reverse proxy
# (see docker-compose.yml). Dependencies come from uv.lock, so the image gets
# exactly what the checkout was tested with.

# --- build: resolve and install into /app/.venv from the lockfile
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer: they only change when uv.lock does.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

COPY pyproject.toml uv.lock ./
COPY plotter ./plotter
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# --- runtime
FROM python:3.12-slim-bookworm

# numpy/rasterio/pyproj/Pillow/lxml ship manylinux wheels with GDAL, PROJ and
# GEOS bundled, so no system GDAL is needed. They do NOT bundle libexpat, which
# the bundled GDAL links against: without it "import rasterio" raises
# ImportError, every terrain sample comes back nodata, and the whole world
# silently reads as sea level. ca-certificates is required for outbound HTTPS
# (elevation tiles, Overpass, ETAK, ionosonde).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libexpat1 \
 && rm -rf /var/lib/apt/lists/*

# Fixed uid/gid so a named volume comes out owned by the app user. The compose
# file overrides this with the host user's uid when it bind-mounts state.
RUN groupadd --gid 10001 plotter \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent plotter \
 && install -d -o 10001 -g 10001 /data

COPY --from=build --chown=root:root /app /app

# HOME and XDG_CACHE_HOME point at /tmp because nothing outside /data is
# writable and the process may run under an arbitrary uid, so every library's
# scratch space has to land somewhere it can write.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp \
    PLOTTER_DATA_DIR=/data \
    PLOTTER_CACHE_DIR=/data/cache \
    PLOTTER_DATABASE_URL=sqlite:////data/plotter.sqlite \
    PLOTTER_HOST=0.0.0.0 \
    PLOTTER_PORT=8000 \
    PLOTTER_WORKERS=4

WORKDIR /app
USER 10001:10001
EXPOSE 8000

# Trust proxy headers only from the Apache sidecar (the app has no host port).
# exec so uvicorn keeps pid 1 and stops on SIGTERM without a 10 s wait.
CMD ["sh", "-c", "exec uvicorn plotter.main:app \
     --host \"$PLOTTER_HOST\" --port \"$PLOTTER_PORT\" --workers \"$PLOTTER_WORKERS\" \
     --proxy-headers --forwarded-allow-ips '*'"]

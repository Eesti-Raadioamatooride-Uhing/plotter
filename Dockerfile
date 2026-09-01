# Plotter application container. Serves the FastAPI/uvicorn app on :8000,
# reached only via the Apache+ModSecurity reverse proxy (see docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# numpy/rasterio/pyproj/Pillow/lxml ship self-contained manylinux wheels, so no
# system GDAL/GEOS is needed; ca-certificates is required for outbound HTTPS
# (elevation tiles, Overpass, ETAK, ionosonde).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY plotter ./plotter
RUN pip install -e .

EXPOSE 8000
# Trust proxy headers only from the Apache sidecar (the app has no host port).
CMD ["uvicorn", "plotter.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "4", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]

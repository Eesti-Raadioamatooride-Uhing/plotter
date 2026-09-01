"""Elevation data providers.

Design: everything downstream asks a `Terrain` object for elevations at
lat/lon arrays. Which raster answers is decided per-request by resolution
preference, with a disk cache in between so a server only downloads a tile
once.

Sources
-------
copernicus  Copernicus GLO-30 DEM, 1 arc-second (~30 m), COGs on AWS Open
            Data. Free, no key, covers Estonia, Finland and every neighbour
            a long path might cross. This is the default.
maaamet     Estonian Land Board (Maa-amet) LiDAR DTM/DSM, 1 m. Highest
            accuracy available for Estonia. Fetched per tile on demand.
mml         National Land Survey of Finland 2 m elevation model. Needs a
            free API key (MML_API_KEY).

All providers return heights in metres above the EGM2008/EGM96 geoid as
published; we do not convert to ellipsoidal heights because every
propagation model here wants height above mean sea level.
"""
from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

try:  # rasterio is required for real data, but the module must import without it
    import rasterio
    HAVE_RASTERIO = True
except Exception:  # pragma: no cover
    rasterio = None
    HAVE_RASTERIO = False


NODATA = -32768.0


@dataclass
class Sample:
    """Elevation samples with provenance, so the UI can say where they came from."""
    values: np.ndarray
    source: str
    resolution_m: float
    missing: int = 0


class ElevationProvider:
    name = "base"
    nominal_resolution_m = 30.0

    def covers(self, lat: float, lon: float) -> bool:
        raise NotImplementedError

    def sample(self, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        raise NotImplementedError


# --------------------------------------------------------------------------
# Copernicus GLO-30
# --------------------------------------------------------------------------

class CopernicusDEM(ElevationProvider):
    """Copernicus GLO-30 global DEM, read straight from S3 as COGs.

    Tile naming, e.g. for the 1x1 degree cell whose SW corner is 59N 24E:
      Copernicus_DSM_COG_10_N59_00_E024_00_DEM/Copernicus_DSM_COG_10_N59_00_E024_00_DEM.tif
    """
    name = "copernicus-glo30"
    nominal_resolution_m = 30.0

    BUCKETS = [
        "https://copernicus-dem-30m.s3.amazonaws.com",
        "https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com",
    ]

    def __init__(self, cache_dir: Path, bucket: str | None = None,
                 timeout_s: int = 60):
        self.cache_dir = Path(cache_dir) / "copernicus"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.bucket = bucket or self.BUCKETS[0]
        self.timeout_s = timeout_s
        self._open: dict[str, object] = {}
        self._lock = threading.Lock()

    @staticmethod
    def tile_name(lat: int, lon: int) -> str:
        ns = "N" if lat >= 0 else "S"
        ew = "E" if lon >= 0 else "W"
        return (f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_"
                f"{ew}{abs(lon):03d}_00_DEM")

    def covers(self, lat: float, lon: float) -> bool:
        return -60.0 <= lat <= 84.0

    def _tile_path(self, ilat: int, ilon: int) -> Path | None:
        name = self.tile_name(ilat, ilon)
        local = self.cache_dir / f"{name}.tif"
        if local.exists():
            return local
        miss = self.cache_dir / f"{name}.missing"
        if miss.exists():
            return None
        url = f"{self.bucket}/{name}/{name}.tif"
        try:
            import httpx
            with httpx.stream("GET", url, timeout=self.timeout_s,
                              follow_redirects=True) as r:
                if r.status_code == 404:
                    miss.touch()
                    return None
                r.raise_for_status()
                tmp = local.with_suffix(".part")
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
                tmp.rename(local)
            log.info("cached Copernicus tile %s", name)
            return local
        except Exception as exc:  # network down, blocked, whatever
            log.warning("could not fetch %s: %s", url, exc)
            return None

    def _dataset(self, ilat: int, ilon: int):
        key = f"{ilat}_{ilon}"
        with self._lock:
            if key in self._open:
                return self._open[key]
        path = self._tile_path(ilat, ilon)
        ds = None
        if path is not None and HAVE_RASTERIO:
            try:
                ds = rasterio.open(path)
            except Exception as exc:  # pragma: no cover
                log.warning("cannot open %s: %s", path, exc)
        with self._lock:
            self._open[key] = ds
        return ds

    def sample(self, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        lats = np.asarray(lats, dtype=float)
        lons = np.asarray(lons, dtype=float)
        out = np.full(lats.shape, np.nan)
        ilats = np.floor(lats).astype(int)
        ilons = np.floor(lons).astype(int)
        for ilat, ilon in {(int(a), int(b)) for a, b in zip(ilats, ilons)}:
            m = (ilats == ilat) & (ilons == ilon)
            ds = self._dataset(ilat, ilon)
            if ds is None:
                continue
            out[m] = _bilinear(ds, lats[m], lons[m])
        return out


# --------------------------------------------------------------------------
# Generic on-demand WCS / tiled local sources for national LiDAR
# --------------------------------------------------------------------------

class WCSProvider(ElevationProvider):
    """Fetch a small GeoTIFF around the area of interest from an OGC WCS.

    Used for the Estonian Maa-amet and Finnish MML high-resolution models,
    which both expose a coverage service. Requests are made per 0.05 degree
    cell and cached, which keeps the request count sane along a long path.
    """
    name = "wcs"
    nominal_resolution_m = 2.0
    cell_deg = 0.05

    def __init__(self, cache_dir: Path, url: str, coverage: str,
                 bbox_wgs84: tuple[float, float, float, float],
                 resolution_m: float = 2.0, version: str = "2.0.1",
                 crs: str = "EPSG:4326", extra_params: dict | None = None,
                 timeout_s: int = 60, name: str | None = None,
                 native_crs: str = "EPSG:4326",
                 axis_e: str = "Long", axis_n: str = "Lat",
                 cell_deg: float | None = None):
        self.cache_dir = Path(cache_dir) / (name or "wcs")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.url = url
        self.coverage = coverage
        self.bbox = bbox_wgs84  # min_lat, min_lon, max_lat, max_lon
        self.nominal_resolution_m = resolution_m
        self.version = version
        self.crs = crs
        self.extra = extra_params or {}
        self.timeout_s = timeout_s
        if name:
            self.name = name
        # Coverages published in a projected national grid (e.g. Maa-amet DTM
        # in EPSG:3301) must be subset in that CRS, using its own axis labels,
        # and then sampled with reprojection from WGS-84.
        self.native_crs = native_crs
        self.axis_e = axis_e            # easting / longitude axis label
        self.axis_n = axis_n            # northing / latitude axis label
        self._reproject = native_crs.upper() not in ("EPSG:4326", "CRS:84")
        if cell_deg is not None:
            self.cell_deg = cell_deg
        self._open: dict[str, object] = {}
        self._lock = threading.Lock()
        self.enabled = bool(url)

    def covers(self, lat: float, lon: float) -> bool:
        if not self.enabled:
            return False
        a, b, c, d = self.bbox
        return a <= lat <= c and b <= lon <= d

    def _cell(self, lat: float, lon: float) -> tuple[int, int]:
        return (int(math.floor(lat / self.cell_deg)),
                int(math.floor(lon / self.cell_deg)))

    def _fetch(self, cy: int, cx: int) -> Path | None:
        key = f"{cy}_{cx}"
        local = self.cache_dir / f"{key}.tif"
        if local.exists():
            return local
        miss = self.cache_dir / f"{key}.missing"
        if miss.exists():
            return None
        lat0, lon0 = cy * self.cell_deg, cx * self.cell_deg
        lat1, lon1 = lat0 + self.cell_deg, lon0 + self.cell_deg
        # Pad the tile so points falling on a cell boundary are sampled well
        # inside the raster rather than on its edge (where bilinear neighbours
        # run into nodata fill).
        margin = 0.1 * self.cell_deg
        lat0 -= margin
        lon0 -= margin
        lat1 += margin
        lon1 += margin
        if self._reproject:
            # Reproject the WGS-84 cell corners into the coverage's native grid
            # and subset there; the server advertises no subsettingCrs.
            from pyproj import Transformer
            tr = Transformer.from_crs("EPSG:4326", self.native_crs,
                                      always_xy=True)
            es, ns = [], []
            for la in (lat0, lat1):
                for lo in (lon0, lon1):
                    e, n = tr.transform(lo, la)
                    es.append(e)
                    ns.append(n)
            e0, e1, n0, n1 = min(es), max(es), min(ns), max(ns)
            subset = [f"{self.axis_e}({e0},{e1})", f"{self.axis_n}({n0},{n1})"]
        else:
            subset = [f"{self.axis_n}({lat0},{lat1})",
                      f"{self.axis_e}({lon0},{lon1})"]
        params = {
            "SERVICE": "WCS",
            "VERSION": self.version,
            "REQUEST": "GetCoverage",
            "COVERAGEID": self.coverage,
            "FORMAT": "image/tiff",
            "SUBSET": subset,
        }
        params.update(self.extra)
        try:
            import httpx
            r = httpx.get(self.url, params=params, timeout=self.timeout_s,
                          follow_redirects=True)
            if r.status_code >= 400 or not r.content[:2] in (b"II", b"MM"):
                miss.touch()
                log.info("%s: no coverage for cell %s (%s)", self.name, key,
                         r.status_code)
                return None
            tmp = local.with_suffix(".part")
            tmp.write_bytes(r.content)
            tmp.rename(local)
            return local
        except Exception as exc:
            log.warning("%s fetch failed for %s: %s", self.name, key, exc)
            return None

    def _dataset(self, cy: int, cx: int):
        key = f"{cy}_{cx}"
        with self._lock:
            if key in self._open:
                return self._open[key]
        path = self._fetch(cy, cx)
        ds = None
        if path is not None and HAVE_RASTERIO:
            try:
                ds = rasterio.open(path)
            except Exception:
                ds = None
        with self._lock:
            self._open[key] = ds
        return ds

    def sample(self, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        lats = np.asarray(lats, dtype=float)
        lons = np.asarray(lons, dtype=float)
        out = np.full(lats.shape, np.nan)
        cys = np.floor(lats / self.cell_deg).astype(int)
        cxs = np.floor(lons / self.cell_deg).astype(int)
        for cy, cx in {(int(a), int(b)) for a, b in zip(cys, cxs)}:
            m = (cys == cy) & (cxs == cx)
            ds = self._dataset(cy, cx)
            if ds is None:
                continue
            out[m] = _bilinear(ds, lats[m], lons[m], reproject=self._reproject)
        return out


class LocalRasterProvider(ElevationProvider):
    """Any directory of georeferenced rasters already on the server's disk.

    Point this at a folder of downloaded Maa-amet or MML tiles and it will
    index them once and serve samples with no network at all. This is the
    right answer for a production install where bandwidth matters.
    """
    name = "local"

    def __init__(self, directory: Path, resolution_m: float = 1.0,
                 name: str | None = None, patterns=("*.tif", "*.tiff", "*.vrt")):
        self.directory = Path(directory)
        self.nominal_resolution_m = resolution_m
        if name:
            self.name = name
        self._index: list[tuple[tuple[float, float, float, float], Path]] = []
        self._open: dict[Path, object] = {}
        self._lock = threading.Lock()
        self.enabled = self.directory.is_dir() and HAVE_RASTERIO
        if self.enabled:
            self._build_index(patterns)

    def _build_index(self, patterns) -> None:
        from rasterio.warp import transform_bounds
        for pat in patterns:
            for p in sorted(self.directory.rglob(pat)):
                try:
                    with rasterio.open(p) as ds:
                        b = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds,
                                             densify_pts=21)
                    self._index.append(((b[1], b[0], b[3], b[2]), p))
                except Exception:
                    continue
        log.info("%s: indexed %d rasters in %s", self.name, len(self._index),
                 self.directory)

    def covers(self, lat: float, lon: float) -> bool:
        return any(a <= lat <= c and b <= lon <= d for (a, b, c, d), _ in self._index)

    def _find(self, lat: float, lon: float) -> Path | None:
        for (a, b, c, d), p in self._index:
            if a <= lat <= c and b <= lon <= d:
                return p
        return None

    def _dataset(self, path: Path):
        with self._lock:
            if path in self._open:
                return self._open[path]
        try:
            ds = rasterio.open(path)
        except Exception:
            ds = None
        with self._lock:
            self._open[path] = ds
        return ds

    def sample(self, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        lats = np.asarray(lats, dtype=float)
        lons = np.asarray(lons, dtype=float)
        out = np.full(lats.shape, np.nan)
        if not self.enabled:
            return out
        groups: dict[Path, list[int]] = {}
        for i, (la, lo) in enumerate(zip(lats, lons)):
            p = self._find(la, lo)
            if p is not None:
                groups.setdefault(p, []).append(i)
        for p, idx in groups.items():
            ds = self._dataset(p)
            if ds is None:
                continue
            ii = np.array(idx)
            out[ii] = _bilinear(ds, lats[ii], lons[ii], reproject=True)
        return out


class SyntheticProvider(ElevationProvider):
    """Deterministic pseudo-terrain. Only for tests and offline demos."""
    name = "synthetic"
    nominal_resolution_m = 30.0

    def covers(self, lat: float, lon: float) -> bool:
        return True

    def sample(self, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        la = np.asarray(lats, dtype=float)
        lo = np.asarray(lons, dtype=float)
        return (40.0
                + 45.0 * np.sin(la * 37.0) * np.cos(lo * 29.0)
                + 18.0 * np.sin(la * 131.0 + lo * 97.0)
                + 6.0 * np.sin(la * 613.0) * np.sin(lo * 547.0)).clip(0.0, None)


# --------------------------------------------------------------------------

def _bilinear(ds, lats: np.ndarray, lons: np.ndarray,
              reproject: bool = False) -> np.ndarray:
    """Bilinear sample of an open rasterio dataset at WGS-84 lat/lon."""
    xs, ys = lons, lats
    if reproject and ds.crs and ds.crs.to_string() not in ("EPSG:4326",):
        from rasterio.warp import transform
        xs, ys = transform("EPSG:4326", ds.crs, list(lons), list(lats))
        xs = np.asarray(xs)
        ys = np.asarray(ys)
    inv = ~ds.transform
    cols, rows = inv * (xs, ys)
    cols = np.asarray(cols) - 0.5
    rows = np.asarray(rows) - 0.5
    c0 = np.floor(cols).astype(int)
    r0 = np.floor(rows).astype(int)
    fc = cols - c0
    fr = rows - r0

    h, w = ds.height, ds.width
    rmin = max(int(np.min(r0)), 0)
    rmax = min(int(np.max(r0)) + 2, h)
    cmin = max(int(np.min(c0)), 0)
    cmax = min(int(np.max(c0)) + 2, w)
    if rmax <= rmin or cmax <= cmin:
        return np.full(lats.shape, np.nan)
    from rasterio.windows import Window
    block = ds.read(1, window=Window(cmin, rmin, cmax - cmin, rmax - rmin),
                    boundless=True, fill_value=NODATA).astype(float)
    nod = ds.nodata
    if nod is not None:
        block[block == nod] = np.nan
    block[block <= NODATA + 1] = np.nan

    def get(rr, cc):
        rr = np.clip(rr - rmin, 0, block.shape[0] - 1)
        cc = np.clip(cc - cmin, 0, block.shape[1] - 1)
        return block[rr, cc]

    v00 = get(r0, c0)
    v01 = get(r0, c0 + 1)
    v10 = get(r0 + 1, c0)
    v11 = get(r0 + 1, c0 + 1)
    top = v00 * (1 - fc) + v01 * fc
    bot = v10 * (1 - fc) + v11 * fc
    val = top * (1 - fr) + bot * fr
    # fall back to nearest neighbour where a corner was nodata
    bad = ~np.isfinite(val)
    if bad.any():
        val[bad] = get(np.rint(rows).astype(int)[bad], np.rint(cols).astype(int)[bad])
    return val


# --------------------------------------------------------------------------

class Terrain:
    """Facade over the configured providers with a coarse-to-fine preference."""

    def __init__(self, providers: list[ElevationProvider],
                 fallback: ElevationProvider | None = None):
        self.providers = providers
        self.fallback = fallback

    @classmethod
    def from_settings(cls, s) -> "Terrain":
        cache = Path(s.cache_dir)
        provs: list[ElevationProvider] = []
        if s.lidar_ee_dir:
            p = LocalRasterProvider(Path(s.lidar_ee_dir), 1.0, name="maaamet-local")
            if p.enabled:
                provs.append(p)
        if s.lidar_fi_dir:
            p = LocalRasterProvider(Path(s.lidar_fi_dir), 2.0, name="mml-local")
            if p.enabled:
                provs.append(p)
        if s.maaamet_wcs_url:
            # Maa-amet DTM is published in the Estonian grid (EPSG:3301) with
            # x/y axes. A small cell keeps each 1 m tile a few MB. resolution
            # tracks the coverage: dtm-1 -> 1 m, dtm-10 -> 10 m, dtm-25 -> 25 m.
            cov = s.maaamet_wcs_coverage
            res = {"dtm-1": 1.0, "dtm-10": 10.0, "dtm-25": 25.0}.get(cov, 1.0)
            provs.append(WCSProvider(
                cache, s.maaamet_wcs_url, cov,
                bbox_wgs84=(57.3, 21.5, 59.8, 28.3), resolution_m=res,
                native_crs="EPSG:3301", axis_e="x", axis_n="y",
                cell_deg=0.02, name="maaamet-wcs"))
        if s.mml_wcs_url and s.mml_api_key:
            provs.append(WCSProvider(
                cache, s.mml_wcs_url, s.mml_wcs_coverage,
                bbox_wgs84=(59.5, 19.0, 70.2, 31.7), resolution_m=2.0,
                extra_params={"api-key": s.mml_api_key}, name="mml-wcs"))
        base = CopernicusDEM(cache, bucket=s.copernicus_bucket)
        provs.append(base)
        fallback = SyntheticProvider() if s.allow_synthetic_terrain else None
        return cls(provs, fallback)

    def sample(self, lats, lons, *, high_res: bool = False) -> Sample:
        lats = np.asarray(lats, dtype=float)
        lons = np.asarray(lons, dtype=float)
        out = np.full(lats.shape, np.nan)
        used: list[str] = []
        res = 30.0
        candidates = self.providers if high_res else [
            p for p in self.providers if p.nominal_resolution_m >= 25.0] or self.providers[-1:]
        for prov in candidates:
            todo = ~np.isfinite(out)
            if not todo.any():
                break
            got = prov.sample(lats[todo], lons[todo])
            if np.isfinite(got).any():
                idx = np.where(todo)[0]
                out[idx] = got
                used.append(prov.name)
                res = min(res, prov.nominal_resolution_m) if used else prov.nominal_resolution_m
        missing = int((~np.isfinite(out)).sum())
        if missing and self.fallback is not None:
            todo = ~np.isfinite(out)
            out[todo] = self.fallback.sample(lats[todo], lons[todo])
            used.append(self.fallback.name)
        # Sea and unmapped water read as nodata in some products; treat as 0 m.
        out = np.nan_to_num(out, nan=0.0)
        return Sample(values=out, source="+".join(used) or "none",
                      resolution_m=res, missing=missing)

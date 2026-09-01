"""Building heights along a path, from the Estonian ETAK topographic database.

ETAK's building layer (``etak:e_401_hoone_ka``) is a national dataset of
building footprints, each polygon carrying a surveyed height in the
``korgus_m`` attribute. For microwave link planning a 30 m DEM says nothing
about the warehouse or apartment block sitting on the path; this fills that
gap where the data exists, which is all of Estonia.

The public WFS is genuinely open (no key, no robots restriction on the API),
so unlike JVIS this can be queried on demand. Results are cached per rounded
bounding box so a run of what-if analyses on the same path hits the network
once.

Estonia only. Finland's equivalent (Maastotietokanta buildings) needs an MML
API key and is left for a future importer; outside the Estonian bounds this
returns all zeros and says so in its provenance note.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)

ETAK_BUILDINGS_LAYER = "etak:e_401_hoone_ka"
ETAK_WFS_URL = "https://gsavalik.envir.ee/geoserver/etak/wfs"

# Rough Estonian bounding box (WGS-84). Used to skip the fetch entirely for
# paths that clearly fall outside the coverage of the dataset.
EE_BOUNDS = (57.3, 21.5, 59.9, 28.3)   # min_lat, min_lon, max_lat, max_lon

_CACHE: dict[tuple, list] = {}
_CACHE_MAX = 32


@dataclass
class BuildingSample:
    """Per-point building heights aligned to a path, plus provenance."""
    heights_m: np.ndarray
    source: str
    building_count: int
    max_height_m: float
    sampled_points: int
    note: str
    capped: bool = False
    warnings: list[str] = field(default_factory=list)


def _in_estonia(lat: float, lon: float) -> bool:
    return (EE_BOUNDS[0] <= lat <= EE_BOUNDS[2]
            and EE_BOUNDS[1] <= lon <= EE_BOUNDS[3])


def _rings(geom: dict):
    """Yield exterior rings (list of [x, y]) for Polygon / MultiPolygon."""
    t = geom.get("type")
    coords = geom.get("coordinates") or []
    if t == "Polygon":
        if coords:
            yield coords[0]
    elif t == "MultiPolygon":
        for poly in coords:
            if poly:
                yield poly[0]


def _point_in_ring(x: float, y: float, ring) -> bool:
    """Ray-casting point-in-polygon on a single ring of [x, y] pairs."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _fetch_buildings(min_x: float, min_y: float, max_x: float, max_y: float,
                     wfs_url: str, count: int, timeout_s: float):
    """Query ETAK buildings in an L-EST97 (EPSG:3301) bounding box."""
    import httpx
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": ETAK_BUILDINGS_LAYER,
        "outputFormat": "application/json", "srsName": "EPSG:3301",
        "count": str(count),
        # East,North order with the short EPSG form. The URN form would demand
        # North,East (EPSG:3301's declared axis order) and silently return
        # nothing if fed East,North.
        "bbox": f"{min_x},{min_y},{max_x},{max_y},EPSG:3301",
    }
    r = httpx.get(wfs_url, params=params, timeout=timeout_s,
                  headers={"User-Agent": "Plotter/1.0 building heights"})
    r.raise_for_status()
    data = r.json()
    feats = data.get("features", [])
    matched = data.get("numberMatched")
    out = []
    for f in feats:
        g = f.get("geometry") or {}
        h = (f.get("properties") or {}).get("korgus_m")
        if not h:
            continue
        try:
            h = float(h)
        except (TypeError, ValueError):
            continue
        for ring in _rings(g):
            if len(ring) < 3:
                continue
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            out.append((min(xs), min(ys), max(xs), max(ys), h, ring))
    return out, matched, len(feats)


def sample_along_path(lats, lons, *, wfs_url: str = ETAK_WFS_URL,
                      margin_m: float = 40.0, count: int = 8000,
                      timeout_s: float = 60.0) -> BuildingSample:
    """Return building height at each path point (0 where none / no data)."""
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    n = len(lats)
    zeros = np.zeros(n)

    mid = n // 2
    if not _in_estonia(float(lats[mid]), float(lons[mid])):
        return BuildingSample(zeros, "none", 0, 0.0, 0,
                              "outside the Estonian building dataset; "
                              "no building heights applied")

    try:
        from pyproj import Transformer
        tr = Transformer.from_crs("EPSG:4326", "EPSG:3301", always_xy=True)
        xs, ys = tr.transform(lons, lats)     # arrays -> easting, northing
        xs = np.asarray(xs, dtype=float)
        ys = np.asarray(ys, dtype=float)
    except Exception as e:  # pragma: no cover - pyproj always present in prod
        return BuildingSample(zeros, "none", 0, 0.0, 0,
                              f"coordinate transform failed: {e}",
                              warnings=[str(e)])

    min_x, max_x = float(xs.min()) - margin_m, float(xs.max()) + margin_m
    min_y, max_y = float(ys.min()) - margin_m, float(ys.max()) + margin_m
    key = (round(min_x, -1), round(min_y, -1), round(max_x, -1),
           round(max_y, -1), count)

    capped = False
    if key in _CACHE:
        buildings, matched, returned = _CACHE[key]
    else:
        try:
            buildings, matched, returned = _fetch_buildings(
                min_x, min_y, max_x, max_y, wfs_url, count, timeout_s)
        except Exception as e:
            log.warning("ETAK building fetch failed: %s", e)
            return BuildingSample(zeros, "none", 0, 0.0, 0,
                                  f"ETAK building fetch failed: {e}",
                                  warnings=[str(e)])
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (buildings, matched, returned)

    if matched and returned and matched > returned:
        capped = True

    heights = np.zeros(n)
    if buildings:
        # Interior points only; endpoints are the antenna sites themselves.
        for i in range(1, n - 1):
            px, py = xs[i], ys[i]
            best = 0.0
            for bx0, by0, bx1, by1, h, ring in buildings:
                if px < bx0 or px > bx1 or py < by0 or py > by1:
                    continue
                if h > best and _point_in_ring(px, py, ring):
                    best = h
            heights[i] = best

    hit = int(np.count_nonzero(heights))
    warns = []
    if capped:
        warns.append(f"only the first {returned} of {matched} buildings in the "
                     "corridor were read; a very long urban path may miss some")
    note = (f"ETAK e_401 buildings: {len(buildings)} in corridor, "
            f"{hit} path point(s) intersect a building"
            + (" (result capped)" if capped else ""))
    return BuildingSample(heights, "etak", len(buildings),
                          float(heights.max()) if hit else 0.0, hit, note,
                          capped=capped, warnings=warns)

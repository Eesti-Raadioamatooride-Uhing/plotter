"""Area coverage prediction: the radial sweep that produces the heatmap.

The approach is the one SPLAT! uses and for the same reason: sample the
terrain once along each radial out to the maximum range, then walk outwards
running the point-to-point model on progressively longer slices of that same
profile. One terrain fetch per azimuth instead of one per pixel.

The result is a polar grid, which the renderer resamples into a north-up
PNG that Leaflet drops on the map as an image overlay.
"""
from __future__ import annotations

import io
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from ..antennas.patterns import Antenna
from ..geodesy import destination
from ..terrain.profile import EARTH_R, free_space_loss_db
from . import itm

log = logging.getLogger(__name__)


@dataclass
class CoverageRequest:
    lat: float
    lon: float
    height_agl_m: float
    freq_mhz: float
    tx_power_w: float = 25.0
    feedline_loss_db: float = 1.0
    antenna: Antenna | None = None
    antenna_bearing_deg: float = 0.0
    rx_height_agl_m: float = 2.0
    rx_gain_dbi: float = 2.15
    sensitivity_dbm: float = -119.0
    max_range_km: float = 100.0
    azimuth_step_deg: float = 2.0
    range_step_m: float = 500.0
    ground: str = "average"
    climate: int = itm.CLIMATE_MARITIME_TEMPERATE_LAND
    polarisation: str = "vertical"
    reliability: float = 0.9
    confidence: float = 0.5
    k_factor: float = 4.0 / 3.0
    clutter_m: float = 0.0
    metric: str = "signal"       # signal | loss | margin | los


@dataclass
class CoverageResult:
    lats: np.ndarray             # (n_az, n_r)
    lons: np.ndarray
    values: np.ndarray           # the chosen metric
    los: np.ndarray              # boolean line-of-sight mask
    azimuths: np.ndarray
    ranges_m: np.ndarray
    bbox: tuple[float, float, float, float]   # min_lat, min_lon, max_lat, max_lon
    request: CoverageRequest
    stats: dict = field(default_factory=dict)


def _radial(terrain, req: CoverageRequest, az: float, n_r: int,
            ranges: np.ndarray):
    """One azimuth: sample terrain once, then step outwards."""
    lats = np.empty(n_r)
    lons = np.empty(n_r)
    for i, r in enumerate(ranges):
        lats[i], lons[i] = destination(req.lat, req.lon, az, float(r))
    s = terrain.sample(lats, lons, high_res=False)
    elev = s.values
    if req.clutter_m:
        surf = elev + req.clutter_m
        surf[0] = elev[0]
    else:
        surf = elev

    tx_amsl = float(elev[0]) + req.height_agl_m
    eps, sigma = itm.GROUND_TYPES.get(req.ground, (15.0, 0.005))
    pol = (itm.POL_VERTICAL if req.polarisation.startswith("v")
           else itm.POL_HORIZONTAL)
    ptx_dbm = 10 * math.log10(max(req.tx_power_w, 1e-4) * 1000.0)

    loss = np.full(n_r, np.nan)
    los = np.zeros(n_r, dtype=bool)
    signal = np.full(n_r, -999.0)

    spacing = float(ranges[1] - ranges[0]) if n_r > 1 else 1.0
    # incremental horizon test for the LOS mask
    max_slope = -1e9
    for i in range(1, n_r):
        d = float(ranges[i])
        bulge = d * d / (2.0 * req.k_factor * EARTH_R)
        rx_amsl = float(elev[i]) + req.rx_height_agl_m
        slope = (float(surf[i]) + bulge - tx_amsl) / d
        rx_slope = (rx_amsl + bulge - tx_amsl) / d
        los[i] = rx_slope >= max_slope
        max_slope = max(max_slope, slope)

        prof = [float(i), spacing] + [float(v) for v in surf[:i + 1]]
        try:
            r = itm.point_to_point(
                prof, max(req.height_agl_m, 0.5), max(req.rx_height_agl_m, 0.5),
                req.freq_mhz, eps_dielect=eps, sgm_conductivity=sigma,
                radio_climate=req.climate, polarization=pol,
                conf=req.confidence, rel=req.reliability)
            L = r.loss_db
        except Exception:
            L = free_space_loss_db(d, req.freq_mhz) + 40.0
        loss[i] = L

        elev_ang = math.degrees(math.atan2(
            rx_amsl - tx_amsl - bulge, d))
        g = (req.antenna.gain_at(elev_ang, az - req.antenna_bearing_deg)
             if req.antenna is not None else 2.15)
        signal[i] = ptx_dbm - req.feedline_loss_db + g - L + req.rx_gain_dbi

    loss[0] = 0.0
    signal[0] = 999.0
    los[0] = True
    return lats, lons, signal, los, loss


def compute(terrain, req: CoverageRequest, workers: int = 8) -> CoverageResult:
    n_az = max(8, int(round(360.0 / max(req.azimuth_step_deg, 0.25))))
    azimuths = np.linspace(0.0, 360.0, n_az, endpoint=False)
    n_r = max(4, int(req.max_range_km * 1000.0 / max(req.range_step_m, 25.0)) + 1)
    ranges = np.linspace(0.0, req.max_range_km * 1000.0, n_r)

    lats = np.zeros((n_az, n_r))
    lons = np.zeros((n_az, n_r))
    sig = np.zeros((n_az, n_r))
    los = np.zeros((n_az, n_r), dtype=bool)
    loss = np.zeros((n_az, n_r))

    def job(k: int):
        return k, _radial(terrain, req, float(azimuths[k]), n_r, ranges)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for k, (la, lo, sg, ls, lss) in ex.map(job, range(n_az)):
            lats[k] = la
            lons[k] = lo
            sig[k] = sg
            los[k] = ls
            loss[k] = lss

    if req.metric == "loss":
        values = loss
    elif req.metric == "margin":
        values = sig - req.sensitivity_dbm
    elif req.metric == "los":
        values = los.astype(float)
    else:
        values = sig

    bbox = (float(np.nanmin(lats)), float(np.nanmin(lons)),
            float(np.nanmax(lats)), float(np.nanmax(lons)))

    covered = sig[:, 1:] >= req.sensitivity_dbm
    # area weighting: each cell's area grows with range
    dr = float(ranges[1] - ranges[0])
    w = np.tile(ranges[1:] - dr / 2.0, (n_az, 1))
    area_km2 = float((covered * w).sum() * dr *
                     math.radians(360.0 / n_az) / 1e6)

    return CoverageResult(lats=lats, lons=lons, values=values, los=los,
                          azimuths=azimuths, ranges_m=ranges, bbox=bbox,
                          request=req,
                          stats={
                              "covered_area_km2": round(area_km2, 1),
                              "max_range_reached_km": round(
                                  float(np.max(np.where(covered.any(axis=0),
                                                        ranges[1:], 0))) / 1000.0, 1),
                              "azimuths": n_az, "range_steps": n_r,
                              "itm_calls": int(n_az * (n_r - 1)),
                          })


# --------------------------------------------------------------------- render

# Signal-strength ramp. Chosen so the strong end reads clearly on both light
# and dark basemaps, and so the steps land on values a radio amateur thinks in.
SIGNAL_STOPS = [
    (-120, (49, 54, 149, 150)),
    (-110, (69, 117, 180, 165)),
    (-100, (116, 173, 209, 175)),
    (-90, (171, 217, 233, 180)),
    (-80, (255, 255, 191, 185)),
    (-70, (254, 224, 144, 190)),
    (-60, (253, 174, 97, 195)),
    (-50, (244, 109, 67, 200)),
    (-40, (215, 48, 39, 205)),
]


def render_png(result: CoverageResult, size: int = 900,
               floor_dbm: float | None = None) -> bytes:
    """Resample the polar grid into a north-up RGBA PNG for a map overlay."""
    from PIL import Image

    req = result.request
    floor = floor_dbm if floor_dbm is not None else req.sensitivity_dbm
    R = req.max_range_km * 1000.0

    # square image spanning the bounding circle
    ys, xs = np.mgrid[0:size, 0:size]
    # image y grows downwards = north at the top
    dx = (xs - (size - 1) / 2.0) / ((size - 1) / 2.0) * R
    dy = ((size - 1) / 2.0 - ys) / ((size - 1) / 2.0) * R
    rr = np.hypot(dx, dy)
    aa = (np.degrees(np.arctan2(dx, dy))) % 360.0

    n_az, n_r = result.values.shape
    ai = aa / (360.0 / n_az)
    ri = rr / (result.ranges_m[1] - result.ranges_m[0])

    a0 = np.floor(ai).astype(int) % n_az
    a1 = (a0 + 1) % n_az
    fa = ai - np.floor(ai)
    r0 = np.clip(np.floor(ri).astype(int), 0, n_r - 1)
    r1 = np.clip(r0 + 1, 0, n_r - 1)
    fr = np.clip(ri - np.floor(ri), 0, 1)

    v = result.values
    val = ((v[a0, r0] * (1 - fa) + v[a1, r0] * fa) * (1 - fr) +
           (v[a0, r1] * (1 - fa) + v[a1, r1] * fa) * fr)
    val = np.where(rr <= R, val, np.nan)

    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    if req.metric == "los":
        m = val > 0.5
        rgba[m] = (34, 197, 94, 150)
    else:
        stops = SIGNAL_STOPS
        if req.metric == "margin":
            stops = [(s[0] + 120 - 20, s[1]) for s in SIGNAL_STOPS]
            floor = 0.0
        flat = val.ravel()
        out = np.zeros((flat.size, 4), dtype=np.uint8)
        # vectorised ramp
        finite = np.isfinite(flat) & (flat >= floor)
        xs_ = np.array([s[0] for s in stops], dtype=float)
        cs_ = np.array([s[1] for s in stops], dtype=float)
        f = np.clip(flat[finite], xs_[0], xs_[-1])
        idx = np.clip(np.searchsorted(xs_, f, side="right") - 1, 0, len(xs_) - 2)
        t = ((f - xs_[idx]) / (xs_[idx + 1] - xs_[idx]))[:, None]
        out[finite] = (cs_[idx] * (1 - t) + cs_[idx + 1] * t).astype(np.uint8)
        rgba = out.reshape(size, size, 4)

    img = Image.fromarray(rgba, "RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def overlay_bounds(result: CoverageResult) -> list[list[float]]:
    """Leaflet imageOverlay bounds for the square the PNG covers."""
    req = result.request
    R = req.max_range_km * 1000.0
    dlat = math.degrees(R / EARTH_R)
    dlon = math.degrees(R / (EARTH_R * math.cos(math.radians(req.lat))))
    return [[req.lat - dlat, req.lon - dlon], [req.lat + dlat, req.lon + dlon]]


def contour_geojson(result: CoverageResult, levels: list[float]) -> dict:
    """Signal-level contour rings as GeoJSON, for export to other tools."""
    feats = []
    v = result.values
    n_az, n_r = v.shape
    for lvl in levels:
        ring = []
        for k in range(n_az):
            row = v[k]
            idx = np.where(row >= lvl)[0]
            j = int(idx.max()) if idx.size else 0
            ring.append([float(result.lons[k, j]), float(result.lats[k, j])])
        if len(ring) > 3:
            ring.append(ring[0])
            feats.append({
                "type": "Feature",
                "properties": {"level_dbm": lvl,
                               "metric": result.request.metric},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            })
    return {"type": "FeatureCollection", "features": feats}

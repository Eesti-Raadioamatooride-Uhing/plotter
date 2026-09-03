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
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
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
    # The level that reads S9 on this band: -73 dBm on HF, -93 dBm above
    # 30 MHz (IARU Region 1). The colour bands hang off it.
    s9_dbm: float = -93.0
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

        # .tolist() rather than a float() comprehension: this runs once per
        # range step per radial, so it is the single hottest line in a sweep.
        prof = [float(i), spacing] + surf[:i + 1].tolist()
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


# --- process pool -----------------------------------------------------------
#
# The ITM port is pure Python, so a thread pool buys nothing on it: measured on
# a 32 core box, one worker did a sweep in 0.68 s and eight did it in 0.78 s,
# because the GIL serialises every solve and the pool only adds contention.
# Real cores need real processes.
#
# Each worker builds its own Terrain rather than inheriting one across the
# fork: GDAL file handles do not survive being forked, and one solver per path
# is what the ITM port requires anyway.

_W: dict = {}


def _init_worker(terrain_factory, req, n_r, ranges) -> None:
    _W["terrain"] = terrain_factory()
    _W["req"] = req
    _W["n_r"] = n_r
    _W["ranges"] = ranges


def _radial_task(k_az):
    k, az = k_az
    return k, _radial(_W["terrain"], _W["req"], az, _W["n_r"], _W["ranges"])


def compute(terrain, req: CoverageRequest, workers: int = 8,
            progress=None, terrain_factory=None) -> CoverageResult:
    """Sweep every radial. `progress` is called with 0..1 as radials land, so
    a long run can report itself instead of looking hung.

    `terrain_factory` is a picklable callable returning a Terrain. Given one,
    the sweep runs across processes and actually uses the machine; without
    one it falls back to threads, which is right for a small sweep where
    process startup would cost more than it saves.
    """
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

    # Process startup and the pickling of each result cost something, so only
    # pay it when there is enough work to win it back.
    use_procs = (terrain_factory is not None and workers > 1
                 and n_az * n_r >= 20000)
    if use_procs:
        pool = ProcessPoolExecutor(
            max_workers=workers, initializer=_init_worker,
            initargs=(terrain_factory, req, n_r, ranges))
        tasks = pool.map(_radial_task,
                         [(k, float(azimuths[k])) for k in range(n_az)],
                         chunksize=max(1, n_az // (workers * 4)))
    else:
        pool = ThreadPoolExecutor(max_workers=workers)
        tasks = pool.map(job, range(n_az))

    try:
        for done, (k, (la, lo, sg, ls, lss)) in enumerate(tasks, start=1):
            lats[k] = la
            lons[k] = lo
            sig[k] = sg
            los[k] = ls
            loss[k] = lss
            if progress is not None and (done % 4 == 0 or done == n_az):
                progress(done / n_az)
    finally:
        pool.shutdown()

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


def sample_grid(result: CoverageResult, max_cells: int = 60000) -> dict:
    """The polar value grid, small enough to ship to the browser.

    The map readout wants a value under the cursor without a round trip per
    mouse move, so the grid travels with the response. A hover hint does not
    need 0.1 dB or 250 m resolution, so decimate to a cell budget and round.
    """
    v = result.values
    n_az, n_r = v.shape
    stride_r = max(1, math.ceil(n_az * n_r / max(max_cells, 1)))
    v = v[:, ::stride_r]
    rng = result.ranges_m[::stride_r]
    # Range 0 is the station itself and carries a sentinel (999 dBm signal,
    # 0 dB loss) rather than a propagation answer, and -999 marks a radial
    # step that was not computed. JSON has no NaN, so both become null.
    def cell(x, i):
        if i == 0 or not np.isfinite(x) or x <= -998.0 or x >= 998.0:
            return None
        return round(float(x), 1)

    rows = [[cell(x, i) for i, x in enumerate(row)] for row in v]
    return {
        "azimuth_step_deg": float(360.0 / n_az),
        "range_step_m": float(rng[1] - rng[0]) if len(rng) > 1 else 0.0,
        "n_az": int(n_az), "n_r": int(v.shape[1]),
        "metric": result.request.metric,
        "values": rows,
    }


# --------------------------------------------------------------------- render

# Signal-strength ramp, banded on S-meter units.
#
# Blue -> cyan -> green -> yellow -> red -> deep red, the convention every RF
# coverage tool uses (SPLAT!, Radio Mobile, the commercial planners). General
# data-viz guidance says sequential magnitude takes one hue and never a
# rainbow, and that is right for a reader with no prior. This reader has a
# strong one: an operator reads these maps daily and already knows red is loud
# and blue is barely there, so the convention wins.
#
# What that guidance is actually protecting against is real, so it is handled
# rather than ignored. The six hexes were picked by running the palette
# validator, not by eye: worst adjacent pair is 17.3 delta-E under protanopia
# and 19.9 under normal vision (target 8 and 15), so neighbouring bands stay
# apart for red-green colour blindness. Alpha also rises with signal, giving
# a second channel that survives any colour vision at all. The two ends sit
# outside the validator's categorical lightness band on purpose: that check
# keeps peer series from out-shouting each other, and these are not peers.
#
# Band edges are dB relative to S9, all multiples of 6, so every edge in the
# picture is a whole S unit that the legend names.
SIGNAL_BANDS = [
    # (dB relative to S9 where the band starts, label, RGB, alpha)
    (-48.0, "S1-S2", (43, 108, 176), 140),
    (-36.0, "S3-S4", (0, 180, 216), 158),
    (-24.0, "S5-S6", (64, 160, 43), 176),
    (-12.0, "S7-S8", (255, 212, 59), 194),
    (0.0, "S9", (224, 49, 49), 208),
    (20.0, "S9+20", (139, 0, 0), 222),
]

# Bumped when the rendering changes in a way that makes a stored run stale.
# The palette itself is hashed into the run key, so this is only for changes
# the bands do not describe (geometry, banding rule, image encoding).
RENDER_VERSION = 3


def render_signature() -> str:
    """Identifies this renderer, so cached runs do not survive a change to it.

    Coverage results are cached on disk under a hash of the request. Without
    the renderer in that hash, editing the palette leaves every stored run
    serving its old picture.
    """
    import hashlib
    return hashlib.sha1(
        f"{RENDER_VERSION}:{SIGNAL_BANDS!r}".encode()).hexdigest()[:12]


# One S unit is 6 dB, on every band (IARU Region 1).
S_UNIT_DB = 6.0


def s_unit_dbm(s: float, s9_dbm: float) -> float:
    """The level that reads S`s` against this band's S9 reference."""
    return s9_dbm + (s - 9.0) * S_UNIT_DB


def band_edges_dbm(s9_dbm: float) -> list[float]:
    return [s9_dbm + off for off, _, _, _ in SIGNAL_BANDS]


def legend(s9_dbm: float) -> list[dict]:
    """The ramp as the UI draws it: one entry per band, in S units and dBm."""
    return [{"label": label, "dbm": round(s9_dbm + off, 1),
             "rgba": list(rgb) + [a]}
            for off, label, rgb, a in SIGNAL_BANDS]


def render_png(result: CoverageResult, size: int = 900,
               floor_dbm: float | None = None) -> bytes:
    """Resample the polar grid into a north-up RGBA PNG for a map overlay."""
    from PIL import Image

    req = result.request
    s9 = req.s9_dbm
    # Paint down to the bottom of the ramp, not to the receiver's threshold.
    # Anchoring the floor to the mode made the map edge the sensitivity limit
    # (S5 for FM on 2 m), which hides every weak-signal contact the operator
    # can actually make on SSB or CW.
    floor = floor_dbm if floor_dbm is not None else s9 + SIGNAL_BANDS[0][0]
    R = req.max_range_km * 1000.0

    # Draw in the projection the map will draw it in.
    #
    # Leaflet stretches an imageOverlay linearly between its corners in Web
    # Mercator. This used to be painted as a flat metric square, which is an
    # azimuthal-equidistant view, and the two do not agree: Mercator's
    # latitude scale grows toward the pole, so equal degrees north and south
    # of the station are not equal pixels and the whole picture slid toward
    # the pole. Measured at 58 N that was 1.8 km north at a 120 km radius,
    # 8 km at 250 km and 20.5 km at 400 km.
    #
    # So work backwards from where each pixel will actually land: take its
    # latitude and longitude the way Leaflet will, then ask what range and
    # bearing that is from the station.
    south, north, west, east = _overlay_box(req)
    y_top, y_bot = _merc_y(north), _merc_y(south)
    lat_px = _inv_merc(np.linspace(y_top, y_bot, size))     # north at the top
    lon_px = np.linspace(west, east, size)
    lat_g = np.radians(lat_px)[:, None]
    lon_g = np.radians(lon_px)[None, :]
    lat_s, lon_s = math.radians(req.lat), math.radians(req.lon)

    # Great circle from the station. The sweep itself walks out on the WGS-84
    # ellipsoid; over these distances the difference between the two is far
    # smaller than one grid cell, and a cell is 7 km wide at 400 km.
    dlon_g = lon_g - lon_s
    sin_dlat = np.sin((lat_g - lat_s) / 2.0)
    sin_dlon = np.sin(dlon_g / 2.0)
    hav = sin_dlat ** 2 + np.cos(lat_s) * np.cos(lat_g) * sin_dlon ** 2
    rr = 2.0 * EARTH_R * np.arcsin(np.sqrt(np.clip(hav, 0.0, 1.0)))
    rr = np.broadcast_to(rr, (size, size))
    yb = np.sin(dlon_g) * np.cos(lat_g)
    xb = (math.cos(lat_s) * np.sin(lat_g)
          - math.sin(lat_s) * np.cos(lat_g) * np.cos(dlon_g))
    aa = np.broadcast_to(np.degrees(np.arctan2(yb, xb)) % 360.0, (size, size))

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
        # Band edges in the metric's own units. "margin" is dB above the
        # receiver's threshold, so its bands sit on the same 6 dB spacing but
        # start at 0 rather than at an absolute level.
        if req.metric == "margin":
            base = SIGNAL_BANDS[0][0]
            edges = [off - base for off, _, _, _ in SIGNAL_BANDS]
            if floor_dbm is None:
                floor = 0.0
        else:
            edges = band_edges_dbm(s9)
        colours = np.array([list(rgb) + [a] for _, _, rgb, a in SIGNAL_BANDS],
                           dtype=np.uint8)

        flat = val.ravel()
        out = np.zeros((flat.size, 4), dtype=np.uint8)
        shown = np.isfinite(flat) & (flat >= floor)
        # Discrete bands, not a gradient: an edge in the picture is then always
        # an S boundary that the legend names, never an arbitrary level.
        idx = np.clip(np.searchsorted(np.array(edges, dtype=float),
                                      flat[shown], side="right") - 1,
                      0, len(edges) - 1)
        out[shown] = colours[idx]
        rgba = out.reshape(size, size, 4)

    img = Image.fromarray(rgba, "RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _merc_y(lat):
    """Web Mercator y for a latitude, in radians of Mercator units."""
    return np.log(np.tan(np.pi / 4.0 + np.radians(lat) / 2.0))


def _inv_merc(y):
    return np.degrees(2.0 * np.arctan(np.exp(y)) - np.pi / 2.0)


def _overlay_box(req) -> tuple[float, float, float, float]:
    """south, north, west, east of the box the overlay occupies."""
    R = req.max_range_km * 1000.0
    dlat = math.degrees(R / EARTH_R)
    dlon = math.degrees(R / (EARTH_R * math.cos(math.radians(req.lat))))
    return (req.lat - dlat, req.lat + dlat, req.lon - dlon, req.lon + dlon)


def overlay_bounds(result: CoverageResult) -> list[list[float]]:
    """Leaflet imageOverlay bounds for the box the PNG covers."""
    south, north, west, east = _overlay_box(result.request)
    return [[south, west], [north, east]]


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

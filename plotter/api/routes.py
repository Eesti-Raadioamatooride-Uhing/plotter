"""HTTP API."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request, Response

from ..config import settings
from ..core.antennas.library import (ANTENNA_PRESETS, BANDS, BANDS_BY_KEY,
                                     MODE_SENSITIVITY, band_for_frequency,
                                     build_antenna)
from ..core.data import buildings, db, ionosphere, jvis, masts
from ..core.geodesy import destination, vincenty_inverse
from ..core.propagation import coverage as cov
from ..core.propagation import hf as hfmod
from ..core.propagation import itm
from ..core.propagation import linkbudget as lb
from ..core.terrain.profile import build_profile, horizon_distance_km
from .schemas import CoverageRequest, HFRequest, LinkRequest, ProfileRequest

log = logging.getLogger(__name__)
router = APIRouter()

_coverage_cache: dict[str, tuple[bytes, dict]] = {}


def _terrain(request: Request):
    return request.app.state.terrain


def _antenna(spec, freq_mhz: float, ground: str):
    eps, sigma = itm.GROUND_TYPES.get(ground, (15.0, 0.005))
    return build_antenna(spec.preset, freq_mhz, eps_r=eps, sigma=sigma,
                         **{k: v for k, v in spec.model_dump().items()
                            if k != "preset" and v is not None})


def _resolve_freq(band: str | None, freq_mhz: float | None,
                  default: float) -> float:
    if freq_mhz:
        return float(freq_mhz)
    if band and band in BANDS_BY_KEY:
        return BANDS_BY_KEY[band].centre_mhz
    return default


def _mode(mode: str, override: float | None):
    m = MODE_SENSITIVITY.get(mode, MODE_SENSITIVITY["fm"])
    return (override if override is not None else m["sensitivity_dbm"],
            float(m["bandwidth_hz"]))


def _apply_buildings(prof):
    """Fold ETAK building heights into a profile's clutter, in place.

    Returns the BuildingSample so callers can surface its provenance.
    """
    bs = buildings.sample_along_path(prof.lats, prof.lons,
                                     wfs_url=settings.etak_wfs_url)
    if bs.source == "etak" and bs.sampled_points:
        prof.clutter_m = np.maximum(prof.clutter_m, bs.heights_m)
        prof.clutter_m[0] = 0.0
        prof.clutter_m[-1] = 0.0
    return bs


# --------------------------------------------------------------- reference

@router.get("/meta")
def meta():
    return {
        "bands": [b.__dict__ for b in BANDS],
        "antennas": ANTENNA_PRESETS,
        "modes": MODE_SENSITIVITY,
        "grounds": {k: {"eps_r": v[0], "sigma": v[1]}
                    for k, v in itm.GROUND_TYPES.items()},
        "climates": {
            1: "Equatorial", 2: "Continental subtropical",
            3: "Maritime subtropical", 4: "Desert",
            5: "Continental temperate", 6: "Maritime temperate, over land",
            7: "Maritime temperate, over sea",
        },
        "tile_sources": settings.tile_source_list,
        "defaults": {
            "climate": settings.default_climate,
            "ground": settings.default_ground,
            "k_factor": settings.default_k_factor,
            "max_coverage_range_km": settings.max_coverage_range_km,
            "max_link_length_km": settings.max_link_length_km,
        },
    }


@router.get("/health")
def health(request: Request):
    t = _terrain(request)
    return {"ok": True,
            "terrain_providers": [p.name for p in t.providers],
            "database": settings.database_url,
            "time_utc": dt.datetime.utcnow().isoformat()}


# ------------------------------------------------------------------ terrain

@router.get("/elevation")
def elevation(request: Request, lat: float, lon: float, high_res: bool = False):
    s = _terrain(request).sample(np.array([lat]), np.array([lon]),
                                 high_res=high_res)
    return {"lat": lat, "lon": lon, "elevation_m": round(float(s.values[0]), 2),
            "source": s.source, "resolution_m": s.resolution_m}


@router.post("/profile")
def profile(request: Request, req: ProfileRequest):
    d, fwd, rev = vincenty_inverse(req.lat1, req.lon1, req.lat2, req.lon2)
    if d / 1000.0 > settings.max_link_length_km:
        raise HTTPException(400, f"path is {d/1000:.0f} km, longer than the "
                                 f"{settings.max_link_length_km:.0f} km limit")
    p = build_profile(_terrain(request), req.lat1, req.lon1, req.lat2, req.lon2,
                      points=req.points, high_res=req.high_res)
    building_info = None
    if req.use_buildings:
        bs = _apply_buildings(p)
        building_info = {"source": bs.source, "count": bs.building_count,
                         "intersecting_points": bs.sampled_points,
                         "max_height_m": round(bs.max_height_m, 1),
                         "note": bs.note}
    return {
        "distance_km": round(d / 1000.0, 4),
        "bearing_deg": round(fwd, 3), "reverse_bearing_deg": round(rev, 3),
        "source": p.source, "resolution_m": p.resolution_m,
        "points": len(p.distances_m),
        "distance_km_series": [round(float(v) / 1000, 4) for v in p.distances_m],
        "elevation_m": [round(float(v), 1) for v in p.elevations_m],
        "surface_m": [round(float(v), 1) for v in p.surface_m],
        "lat": [round(float(v), 6) for v in p.lats],
        "lon": [round(float(v), 6) for v in p.lons],
        "buildings": building_info,
    }


# --------------------------------------------------------------------- link

@router.post("/link")
def link(request: Request, req: LinkRequest):
    freq = _resolve_freq(req.band, req.freq_mhz, 5760.0)
    sens, bw = _mode(req.mode, req.sensitivity_dbm)
    if req.bandwidth_hz:
        bw = req.bandwidth_hz

    d, _, _ = vincenty_inverse(req.tx.lat, req.tx.lon, req.rx.lat, req.rx.lon)
    if d <= 0:
        raise HTTPException(400, "the two ends are at the same place")
    if d / 1000.0 > settings.max_link_length_km:
        raise HTTPException(400, f"path is {d/1000:.0f} km, longer than the "
                                 f"{settings.max_link_length_km:.0f} km limit")

    prof = build_profile(_terrain(request), req.tx.lat, req.tx.lon,
                         req.rx.lat, req.rx.lon, points=req.points,
                         high_res=req.high_res_terrain,
                         clutter_m=req.clutter_m)

    building_info = None
    if req.use_buildings:
        bs = _apply_buildings(prof)
        building_info = {"source": bs.source, "count": bs.building_count,
                         "intersecting_points": bs.sampled_points,
                         "max_height_m": round(bs.max_height_m, 1),
                         "note": bs.note}

    tx = lb.Endpoint(name=req.tx.name, lat=req.tx.lat, lon=req.tx.lon,
                     height_agl_m=req.tx.height_agl_m,
                     antenna=_antenna(req.tx.antenna, freq, req.ground),
                     tx_power_w=req.tx.tx_power_w,
                     feedline_loss_db=req.tx.feedline_loss_db,
                     rx_noise_figure_db=req.tx.rx_noise_figure_db)
    rx = lb.Endpoint(name=req.rx.name, lat=req.rx.lat, lon=req.rx.lon,
                     height_agl_m=req.rx.height_agl_m,
                     antenna=_antenna(req.rx.antenna, freq, req.ground),
                     tx_power_w=req.rx.tx_power_w,
                     feedline_loss_db=req.rx.feedline_loss_db,
                     rx_noise_figure_db=req.rx.rx_noise_figure_db)

    result = lb.evaluate(prof, tx, rx, freq, mode_sensitivity_dbm=sens,
                         bandwidth_hz=bw, k_factor=req.k_factor,
                         clutter_m=req.clutter_m, climate=req.climate,
                         polarisation=req.polarisation, ground=req.ground,
                         target_availability_pct=req.availability_pct,
                         include_profile=req.include_profile)
    out = lb.to_dict(result)
    out["frequency_mhz"] = freq
    out["band"] = (band_for_frequency(freq).label
                   if band_for_frequency(freq) else None)
    out["mode"] = req.mode
    out["tx"] = {"name": tx.name, "lat": tx.lat, "lon": tx.lon,
                 "antenna": tx.antenna.describe(),
                 "height_agl_m": tx.height_agl_m}
    out["rx"] = {"name": rx.name, "lat": rx.lat, "lon": rx.lon,
                 "antenna": rx.antenna.describe(),
                 "height_agl_m": rx.height_agl_m}
    if building_info is not None:
        out["buildings"] = building_info
        out.setdefault("notes", []).insert(0, "Buildings: " + building_info["note"])
    # reverse direction, because the two ends usually differ in power and gain
    if req.rx.tx_power_w:
        rev = lb.evaluate(_reverse(prof), rx, tx, freq,
                          mode_sensitivity_dbm=sens, bandwidth_hz=bw,
                          k_factor=req.k_factor, clutter_m=req.clutter_m,
                          climate=req.climate, polarisation=req.polarisation,
                          ground=req.ground,
                          target_availability_pct=req.availability_pct,
                          include_profile=False)
        out["reverse"] = {"rx_level_dbm": rev.rx_level_dbm,
                          "link_margin_db": rev.link_margin_db,
                          "verdict": rev.verdict,
                          "eirp_dbm": rev.eirp_dbm}
    return out


def _reverse(p):
    import copy
    q = copy.copy(p)
    q.lats = p.lats[::-1].copy()
    q.lons = p.lons[::-1].copy()
    q.elevations_m = p.elevations_m[::-1].copy()
    q.clutter_m = p.clutter_m[::-1].copy()
    q.distances_m = (p.total_distance_m - p.distances_m[::-1]).copy()
    q.bearing_deg = p.reverse_bearing_deg
    q.reverse_bearing_deg = p.bearing_deg
    return q


@router.post("/link/best-heights")
def best_heights(request: Request, req: LinkRequest):
    """Grid the two antenna heights and report where the link comes alive."""
    from ..core.terrain.profile import analyse_link
    freq = _resolve_freq(req.band, req.freq_mhz, 5760.0)
    prof = build_profile(_terrain(request), req.tx.lat, req.tx.lon,
                         req.rx.lat, req.rx.lon, high_res=req.high_res_terrain,
                         clutter_m=req.clutter_m)
    if req.use_buildings:
        _apply_buildings(prof)
    use_clutter = req.clutter_m > 0 or bool(np.any(prof.clutter_m > 0))
    heights = [2, 5, 8, 10, 12, 15, 18, 21, 25, 30, 36, 42, 50, 60, 75, 90, 120]
    grid = []
    for ht in heights:
        row = []
        for hr in heights:
            g = analyse_link(prof, ht, hr, freq, req.k_factor,
                             use_clutter=use_clutter)
            row.append(round(g.min_fresnel_fraction, 3))
        grid.append(row)
    return {"heights_m": heights, "fresnel_fraction": grid,
            "distance_km": round(prof.total_distance_m / 1000, 3),
            "note": "1.0 means the first Fresnel zone is fully clear; "
                    "0.6 is the usual design target"}


# ----------------------------------------------------------------- coverage

def _coverage_key(req: CoverageRequest) -> str:
    return hashlib.sha1(
        json.dumps(req.model_dump(), sort_keys=True, default=str).encode()
    ).hexdigest()


@router.post("/coverage")
def coverage(request: Request, req: CoverageRequest):
    freq = _resolve_freq(req.band, req.freq_mhz, 145.0)
    sens, _ = _mode(req.mode, req.sensitivity_dbm)
    if req.max_range_km > settings.max_coverage_range_km:
        raise HTTPException(400, f"max_range_km is capped at "
                                 f"{settings.max_coverage_range_km:.0f}")

    key = _coverage_key(req)
    ant = _antenna(req.site.antenna, freq, req.ground)
    creq = cov.CoverageRequest(
        lat=req.site.lat, lon=req.site.lon,
        height_agl_m=req.site.height_agl_m, freq_mhz=freq,
        tx_power_w=req.site.tx_power_w,
        feedline_loss_db=req.site.feedline_loss_db,
        antenna=ant, antenna_bearing_deg=req.antenna_bearing_deg,
        rx_height_agl_m=req.rx_height_agl_m, rx_gain_dbi=req.rx_gain_dbi,
        sensitivity_dbm=sens, max_range_km=req.max_range_km,
        azimuth_step_deg=req.azimuth_step_deg, range_step_m=req.range_step_m,
        ground=req.ground, climate=req.climate,
        polarisation=req.polarisation, reliability=req.reliability,
        confidence=req.confidence, k_factor=req.k_factor,
        clutter_m=req.clutter_m, metric=req.metric)

    result = cov.compute(_terrain(request), creq,
                         workers=settings.coverage_workers)
    png = cov.render_png(result, size=req.image_size)
    payload = {
        "id": key,
        "bounds": cov.overlay_bounds(result),
        "image_url": f"{settings.base_path}/api/coverage/{key}.png",
        "stats": result.stats,
        "frequency_mhz": freq,
        "sensitivity_dbm": sens,
        "metric": req.metric,
        "antenna": ant.describe(),
        "site": {"lat": req.site.lat, "lon": req.site.lon,
                 "height_agl_m": req.site.height_agl_m,
                 "ground_amsl_m": round(float(result.lats.shape[0] and
                                              0.0), 1)},
        "horizon_km": round(horizon_distance_km(req.site.height_agl_m,
                                                req.k_factor), 1),
        "legend": [{"dbm": s[0], "rgba": list(s[1])} for s in cov.SIGNAL_STOPS],
    }
    contours = cov.contour_geojson(result, [sens, sens + 10, sens + 20])
    payload["contours"] = contours
    _coverage_cache[key] = (png, payload)
    if len(_coverage_cache) > 24:
        for k in list(_coverage_cache)[:-24]:
            _coverage_cache.pop(k, None)
    return payload


@router.get("/coverage/{key}.png")
def coverage_png(key: str):
    hit = _coverage_cache.get(key)
    if not hit:
        raise HTTPException(404, "that coverage run has expired, re-run it")
    return Response(content=hit[0], media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/coverage/{key}.geojson")
def coverage_geojson(key: str):
    hit = _coverage_cache.get(key)
    if not hit:
        raise HTTPException(404, "that coverage run has expired, re-run it")
    return hit[1]["contours"]


# ----------------------------------------------------------------------- HF

@router.post("/hf")
def hf(request: Request, req: HFRequest):
    band = BANDS_BY_KEY.get(req.band)
    freq = req.freq_mhz or (band.centre_mhz if band else 3.65)
    sens, _ = _mode(req.mode, req.sensitivity_dbm)
    eps, sigma = itm.GROUND_TYPES.get(req.ground, (15.0, 0.005))
    when = (dt.datetime.fromisoformat(req.when.replace("Z", ""))
            if req.when else dt.datetime.utcnow())

    iono = ionosphere.current(req.site.lat, req.site.lon, when,
                              use_network=req.use_live_ionosphere)
    ant = _antenna(req.site.antenna, freq, req.ground)
    pol = ("vertical" if getattr(ant, "kind", "") == "vertical"
           or getattr(ant, "polarisation", "") == "vertical" else "horizontal")

    distances = req.distances_km or [25, 50, 100, 150, 200, 300, 400, 500, 700,
                                     900, 1200, 1500, 2000, 2500, 3000, 4000]
    pred = hfmod.predict(freq, distances, iono=iono,
                         tx_power_w=req.site.tx_power_w, antenna=ant,
                         eps_r=eps, sigma=sigma, rx_sensitivity_dbm=sens,
                         polarisation=pol)

    # elevation pattern, the thing that actually decides HF performance
    angles, gains = ant.elevation_cut(step=1.0)
    rings = []
    for m in pred.modes:
        rings.append({
            "distance_km": m.distance_km, "mode": m.name,
            "takeoff_deg": round(m.takeoff_deg, 1), "hops": m.hops,
            "layer": m.layer,
            "path_loss_db": round(m.path_loss_db, 1),
            "antenna_gain_dbi": round(m.antenna_gain_dbi, 1),
            "rx_level_dbm": round(m.rx_level_dbm, 1),
            "s_meter": m.s_meter, "muf_mhz": (round(m.muf_mhz, 2)
                                              if math.isfinite(m.muf_mhz) else None),
            "usable": m.usable, "note": m.note,
        })

    azimuths = req.azimuths_deg or list(range(0, 360, 15))
    footprints = []
    for m in pred.modes:
        if not m.usable:
            continue
        ring = []
        for az in azimuths:
            la, lo = destination(req.site.lat, req.site.lon, az,
                                 m.distance_km * 1000.0)
            ring.append([round(lo, 5), round(la, 5)])
        footprints.append({"distance_km": m.distance_km, "mode": m.name,
                           "s_meter": m.s_meter,
                           "rx_level_dbm": round(m.rx_level_dbm, 1),
                           "ring": ring})

    return {
        "frequency_mhz": freq,
        "band": band.label if band else None,
        "when_utc": when.isoformat(),
        "ionosphere": {
            "fo_f2_mhz": round(iono.fo_f2_mhz, 2),
            "fo_e_mhz": round(iono.fo_e_mhz, 2),
            "h_f2_km": round(iono.h_f2_km, 0),
            "is_day": iono.is_day,
            "absorption_index": round(iono.absorption_index, 3),
            "source": iono.source,
            "muf_3000_mhz": round(pred.muf_3000_mhz, 2),
        },
        "antenna": ant.describe(),
        "elevation_pattern": {
            "angles_deg": [float(a) for a in angles],
            "gain_dbi": [round(float(g), 2) for g in gains],
        },
        "ground_wave_range_km": round(pred.ground_wave_range_km, 1),
        "nvis": {"usable": pred.nvis_ok,
                 "range_km": round(pred.nvis_range_km, 0),
                 "gain_at_zenith_dbi": round(ant.gain_at(88.0), 2)},
        "distances": rings,
        "footprints": footprints,
        "notes": pred.notes,
        "polarisation": pol,
    }


@router.get("/hf/space-weather")
def space_weather():
    return ionosphere.fetch_solar_indices()


# ------------------------------------------------------------------ registry

@router.get("/sites")
def sites(min_lat: float = Query(...), min_lon: float = Query(...),
          max_lat: float = Query(...), max_lon: float = Query(...),
          kinds: str | None = None, limit: int = 3000):
    ks = [k.strip() for k in kinds.split(",")] if kinds else None
    return {"sites": db.query_sites((min_lat, min_lon, max_lat, max_lon),
                                    ks, limit)}


@router.get("/transmitters")
def transmitters(min_lat: float | None = None, min_lon: float | None = None,
                 max_lat: float | None = None, max_lon: float | None = None,
                 amateur_only: bool | None = None,
                 freq_min: float | None = None, freq_max: float | None = None,
                 band: str | None = None, service: str | None = None,
                 search: str | None = None, limit: int = 3000):
    if band and band in BANDS_BY_KEY:
        b = BANDS_BY_KEY[band]
        freq_min, freq_max = b.low_mhz, b.high_mhz
    bbox = None
    if None not in (min_lat, min_lon, max_lat, max_lon):
        bbox = (min_lat, min_lon, max_lat, max_lon)
    return {"transmitters": db.query_transmitters(
        bbox, amateur_only, freq_min, freq_max, service, search, limit)}


@router.get("/registry/status")
def registry_status():
    return db.stats()


@router.get("/registry/jvis/discover")
def jvis_discover():
    """Report what the JVIS public module looks like right now."""
    sc = jvis.JVISScraper(settings.jvis_base_url, settings.jvis_module_path,
                          settings.jvis_user_agent,
                          settings.jvis_request_delay_s,
                          respect_robots=settings.jvis_respect_robots)
    d = sc.discover()
    return d.__dict__


@router.post("/registry/refresh")
def registry_refresh(source: str = Query("all", pattern="^(all|jvis|masts)$")):
    out = {}
    if source in ("all", "masts"):
        n, msg = masts.run(settings)
        out["masts"] = {"records": n, "note": msg}
    if source in ("all", "jvis"):
        n, msg = jvis.run(settings)
        out["jvis"] = {"records": n, "note": msg}
    return out


# ------------------------------------------------------------------ helpers

@router.get("/horizon")
def horizon(request: Request, lat: float, lon: float, height_agl_m: float = 10.0,
            k_factor: float = 4.0 / 3.0, azimuth_step_deg: float = 2.0,
            max_range_km: float = 80.0):
    """Radio horizon ring: how far you can see in every direction."""
    from ..core.terrain.profile import EARTH_R
    terrain = _terrain(request)
    n_az = int(360 / azimuth_step_deg)
    n_r = 200
    ring = []
    base = terrain.sample(np.array([lat]), np.array([lon])).values[0]
    tx_amsl = float(base) + height_agl_m
    for k in range(n_az):
        az = k * 360.0 / n_az
        rr = np.linspace(200.0, max_range_km * 1000.0, n_r)
        las = np.empty(n_r)
        los_ = np.empty(n_r)
        for i, r in enumerate(rr):
            las[i], los_[i] = destination(lat, lon, az, float(r))
        el = terrain.sample(las, los_).values
        bulge = rr * rr / (2.0 * k_factor * EARTH_R)
        slope = (el + bulge - tx_amsl) / rr
        best = -1e9
        reach = rr[-1]
        for i in range(n_r):
            if slope[i] > best:
                best = slope[i]
                reach = rr[i]
        la, lo = destination(lat, lon, az, float(reach))
        ring.append([round(lo, 5), round(la, 5)])
    ring.append(ring[0])
    return {"type": "Feature",
            "properties": {"height_agl_m": height_agl_m,
                           "geometric_horizon_km": round(
                               horizon_distance_km(height_agl_m, k_factor), 1),
                           "ground_amsl_m": round(float(base), 1)},
            "geometry": {"type": "Polygon", "coordinates": [ring]}}


@router.get("/antenna/pattern")
def antenna_pattern(preset: str = "dipole", freq_mhz: float = 3.65,
                    height_m: float = 12.0, ground: str = "average",
                    diameter_m: float | None = None,
                    peak_gain_dbi: float | None = None,
                    radials: int | None = None,
                    orientation_deg: float | None = None):
    eps, sigma = itm.GROUND_TYPES.get(ground, (15.0, 0.005))
    ant = build_antenna(preset, freq_mhz, eps_r=eps, sigma=sigma,
                        height_m=height_m, diameter_m=diameter_m,
                        peak_gain_dbi=peak_gain_dbi, radials=radials,
                        orientation_deg=orientation_deg)
    ea, eg = ant.elevation_cut(step=1.0)
    aa, ag = ant.azimuth_cut(elevation_deg=max(
        5.0, float(ea[int(np.argmax(eg))])), step=2.0)
    return {"antenna": ant.describe(),
            "elevation": {"angles_deg": [float(x) for x in ea],
                          "gain_dbi": [round(float(x), 2) for x in eg]},
            "azimuth": {"angles_deg": [float(x) for x in aa],
                        "gain_dbi": [round(float(x), 2) for x in ag]}}

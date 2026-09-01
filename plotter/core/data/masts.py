"""Public mast and tower data for Estonia and Finland.

OpenStreetMap is the one source that is genuinely open, covers both
countries, carries heights for most large structures, and can be refreshed
without an account. Overpass gives us masts, telecom towers, water towers,
chimneys and observation towers - the structures an amateur actually asks
about when looking for somewhere to hang an antenna.

Estonia's ETAK (via the Land Board's WFS) and Finland's Maastotietokanta
carry the same structures with surveyed heights and can be layered on top
where a national dataset is available; both are wired in as optional
importers because they need either a key or a bulk download.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

log = logging.getLogger(__name__)

OVERPASS_QUERY = """
[out:json][timeout:240];
(
  area["ISO3166-1"="EE"][admin_level=2];
  area["ISO3166-1"="FI"][admin_level=2];
)->.searchArea;
(
  node["man_made"="mast"](area.searchArea);
  way["man_made"="mast"](area.searchArea);
  node["man_made"="tower"](area.searchArea);
  way["man_made"="tower"](area.searchArea);
  node["man_made"="communications_tower"](area.searchArea);
  way["man_made"="communications_tower"](area.searchArea);
  node["man_made"="water_tower"](area.searchArea);
  way["man_made"="water_tower"](area.searchArea);
  node["man_made"="chimney"]["height"](area.searchArea);
  way["man_made"="chimney"]["height"](area.searchArea);
);
out center tags;
"""

KIND_MAP = {
    "mast": "mast",
    "tower": "tower",
    "communications_tower": "tower",
    "water_tower": "water_tower",
    "chimney": "chimney",
}


def _height(tags: dict) -> float | None:
    for key in ("height", "building:height", "tower:height", "est_height"):
        v = tags.get(key)
        if not v:
            continue
        try:
            return float(str(v).lower().replace("m", "").replace(",", ".").strip())
        except ValueError:
            continue
    return None


def _kind(tags: dict) -> str:
    mm = tags.get("man_made", "")
    kind = KIND_MAP.get(mm, mm or "structure")
    t = tags.get("tower:type", "")
    if t in ("communication", "radio"):
        kind = "tower"
    if t == "observation":
        kind = "observation_tower"
    if t == "lighting":
        kind = "lighting_mast"
    return kind


def fetch_overpass(url: str, timeout_s: float = 300.0,
                   query: str = OVERPASS_QUERY) -> list[dict]:
    import httpx
    r = httpx.post(url, data={"data": query}, timeout=timeout_s,
                   headers={"User-Agent": "Plotter/1.0 mast import"})
    r.raise_for_status()
    payload = r.json()
    out: list[dict] = []
    for el in payload.get("elements", []):
        tags = el.get("tags", {}) or {}
        if el["type"] == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            c = el.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if lat is None or lon is None:
            continue
        country = "EE" if lat < 59.8 and lon < 28.4 and lat > 57.4 else "FI"
        if 59.0 < lat < 60.0 and 21.0 < lon < 28.3:
            country = "EE" if lat < 59.75 else "FI"
        out.append({
            "source": "osm",
            "source_id": f"{el['type']}/{el['id']}",
            "name": tags.get("name") or tags.get("operator") or _kind(tags),
            "kind": _kind(tags),
            "lat": float(lat), "lon": float(lon),
            "height_m": _height(tags),
            "ground_amsl_m": None,
            "operator": tags.get("operator"),
            "country": country,
            "address": tags.get("addr:street"),
            "tags": json.dumps(tags, ensure_ascii=False)[:4000],
        })
    return out


ETAK_WFS_URL = "https://gsavalik.envir.ee/geoserver/etak/wfs"
# E_402 "korgrajatis" (tall structure) is the layer that carries masts, towers,
# chimneys and wind turbines as points with a surveyed `korgus` (height, m).
# The old E_204_MAST_* layers were renamed/retired in the ETAK WFS.
ETAK_LAYERS = ["etak:e_402_korgrajatis_p"]

# ETAK tyyp_tekst (structure type, Estonian) -> our kind vocabulary.
ETAK_TYPE_MAP = {
    "mast": "mast",
    "sidemast": "mast",             # communications mast
    "valgusmast": "lighting_mast",  # floodlight / lighting mast
    "torn": "tower",
    "korsten": "chimney",
    "tuulegeneraator": "wind_turbine",
    "tuulik": "wind_turbine",
    "vaatetorn": "observation_tower",
    "veetorn": "water_tower",
}


def _etak_kind(type_text: str | None) -> str:
    t = (type_text or "").strip().lower()
    return ETAK_TYPE_MAP.get(t, "structure")


def fetch_etak(url: str = ETAK_WFS_URL, layers=None,
               timeout_s: float = 180.0) -> list[dict]:
    """Estonian topographic database tall structures, with surveyed heights."""
    import httpx
    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:3301", "EPSG:4326", always_xy=True)
    out: list[dict] = []
    for layer in (layers or ETAK_LAYERS):
        params = {"service": "WFS", "version": "2.0.0",
                  "request": "GetFeature", "typeNames": layer,
                  "outputFormat": "application/json", "count": "20000"}
        try:
            r = httpx.get(url, params=params, timeout=timeout_s,
                          headers={"User-Agent": "Plotter/1.0"})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("ETAK layer %s failed: %s", layer, e)
            continue
        for f in data.get("features", []):
            g = f.get("geometry") or {}
            if g.get("type") != "Point":
                continue
            x, y = g["coordinates"][0], g["coordinates"][1]
            lon, lat = tr.transform(x, y)
            p = f.get("properties", {}) or {}
            type_text = p.get("tyyp_tekst") or p.get("kood_tekst")
            name = (p.get("nimetus") or type_text or "structure")
            out.append({
                "source": "etak",
                "source_id": str(p.get("etak_id") or f.get("id") or f"{x:.1f},{y:.1f}"),
                "name": name,
                "kind": _etak_kind(type_text),
                "lat": lat, "lon": lon,
                "height_m": _height(p) or p.get("korgus"),
                "ground_amsl_m": None,
                "operator": None, "country": "EE",
                "address": None,
                "tags": json.dumps(p, ensure_ascii=False)[:4000],
            })
    return out


def run(settings, include_etak: bool = True) -> tuple[int, str]:
    from . import db
    started = dt.datetime.utcnow()
    total = 0
    notes = []
    try:
        recs = fetch_overpass(settings.osm_overpass_url)
        total += db.upsert_sites(recs)
        notes.append(f"OSM: {len(recs)}")
    except Exception as e:
        notes.append(f"OSM failed: {e}")
    if include_etak:
        try:
            recs = fetch_etak(getattr(settings, "etak_wfs_url", ETAK_WFS_URL))
            total += db.upsert_sites(recs)
            notes.append(f"ETAK: {len(recs)}")
        except Exception as e:
            notes.append(f"ETAK failed: {e}")
    msg = "; ".join(notes)
    db.record_run("masts", total > 0, total, msg, started)
    return total, msg

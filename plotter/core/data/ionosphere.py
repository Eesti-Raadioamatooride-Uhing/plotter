"""Live space-weather and ionosonde input for the HF model.

The nearest useful ionosondes to Estonia are Sodankyla (SO166, 67.4 N) and
Juliusruh (JR055, 54.6 N). Between them they bracket the paths that matter
here. When neither is reachable the HF model falls back on its own diurnal
model, which is fine for showing the shape of the day but should not be
trusted for a marginal opening.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from ..propagation.hf import Ionosphere

log = logging.getLogger(__name__)

NOAA_SSN_URL = "https://services.swpc.noaa.gov/json/solar-cycle/observed-solar-cycle-indices.json"
NOAA_KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
GIRO_URL = "https://lgdc.uml.edu/common/DIDBGetValues"

STATIONS = {
    "sodankyla": {"code": "SO166", "lat": 67.37, "lon": 26.63,
                  "label": "Sodankyla, Finland"},
    "juliusruh": {"code": "JR055", "lat": 54.60, "lon": 13.40,
                  "label": "Juliusruh, Germany"},
    "pruhonice": {"code": "PQ052", "lat": 50.00, "lon": 14.60,
                  "label": "Pruhonice, Czechia"},
}

_cache: dict = {"at": None, "value": None}


def fetch_solar_indices(timeout_s: float = 20.0) -> dict:
    """Sunspot number, 10.7 cm flux and Kp from NOAA SWPC."""
    import httpx
    out = {"ssn": 60.0, "sfi": 110.0, "kp": 2.0, "source": "default"}
    try:
        r = httpx.get(NOAA_SSN_URL, timeout=timeout_s)
        r.raise_for_status()
        rows = r.json()
        if rows:
            last = rows[-1]
            out["ssn"] = float(last.get("ssn") or out["ssn"])
            out["sfi"] = float(last.get("f10.7") or out["sfi"])
            out["source"] = "noaa-swpc"
            out["month"] = last.get("time-tag")
    except Exception as e:
        log.info("solar index fetch failed: %s", e)
    try:
        r = httpx.get(NOAA_KP_URL, timeout=timeout_s)
        r.raise_for_status()
        rows = r.json()
        if len(rows) > 1:
            out["kp"] = float(rows[-1][1])
    except Exception as e:
        log.info("Kp fetch failed: %s", e)
    return out


def fetch_ionosonde(station: str = "sodankyla", hours: int = 2,
                    timeout_s: float = 25.0) -> dict | None:
    """Latest foF2 / hmF2 from the GIRO DIDBase text service."""
    import httpx
    st = STATIONS.get(station)
    if not st:
        return None
    now = dt.datetime.utcnow()
    params = {
        "ursiCode": st["code"],
        "charName": "foF2,hmF2,foE",
        "DMUF": "3000",
        "fromDate": (now - dt.timedelta(hours=hours)).strftime("%Y.%m.%d %H:%M:%S"),
        "toDate": now.strftime("%Y.%m.%d %H:%M:%S"),
    }
    try:
        r = httpx.get(GIRO_URL, params=params, timeout=timeout_s,
                      headers={"User-Agent": "Plotter/1.0"})
        r.raise_for_status()
    except Exception as e:
        log.info("ionosonde %s fetch failed: %s", station, e)
        return None
    fo_f2 = hm_f2 = fo_e = None
    stamp = None
    for line in r.text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = re.split(r"\s+", line.strip())
        if len(parts) < 4:
            continue
        try:
            vals = [float(p) for p in parts[1:] if re.match(r"^-?\d+\.?\d*$", p)]
        except ValueError:
            continue
        if not vals:
            continue
        stamp = parts[0]
        if len(vals) >= 1 and 1.0 < vals[0] < 20.0:
            fo_f2 = vals[0]
        if len(vals) >= 2 and 150.0 < vals[1] < 600.0:
            hm_f2 = vals[1]
        if len(vals) >= 3 and 0.5 < vals[2] < 8.0:
            fo_e = vals[2]
    if fo_f2 is None:
        return None
    return {"station": st["label"], "code": st["code"], "lat": st["lat"],
            "lon": st["lon"], "fo_f2_mhz": fo_f2, "hm_f2_km": hm_f2,
            "fo_e_mhz": fo_e, "observed_at": stamp}


def current(lat: float, lon: float, when: dt.datetime | None = None,
            use_network: bool = True, cache_seconds: int = 900) -> Ionosphere:
    """Best available ionosphere for a point: live if we can, modelled if not."""
    when = when or dt.datetime.utcnow()
    now = dt.datetime.utcnow()
    if (_cache["at"] and (now - _cache["at"]).total_seconds() < cache_seconds
            and _cache["value"]):
        cached = _cache["value"]
    elif use_network:
        cached = {"solar": fetch_solar_indices(),
                  "sonde": fetch_ionosonde("sodankyla")}
        _cache["at"] = now
        _cache["value"] = cached
    else:
        cached = {"solar": {"ssn": 60.0, "source": "default"}, "sonde": None}

    ssn = float(cached.get("solar", {}).get("ssn") or 60.0)
    iono = Ionosphere.model(lat, lon, when, ssn=ssn)
    sonde = cached.get("sonde")
    if sonde and sonde.get("fo_f2_mhz"):
        # Scale the ionosonde's foF2 to our latitude: foF2 falls off towards
        # the auroral zone, so a Sodankyla reading understates Tallinn by a
        # little in the day and rather more at night.
        dlat = lat - sonde["lat"]
        scale = 1.0 + 0.012 * (-dlat)   # ~1.2% per degree southwards
        iono.fo_f2_mhz = max(1.5, sonde["fo_f2_mhz"] * max(0.6, min(1.6, scale)))
        if sonde.get("hm_f2_km"):
            iono.h_f2_km = sonde["hm_f2_km"]
        if sonde.get("fo_e_mhz"):
            iono.fo_e_mhz = sonde["fo_e_mhz"]
        iono.source = f"{sonde['station']} {sonde.get('observed_at') or ''}".strip()
    else:
        iono.source = f"model (SSN {ssn:.0f}, {cached.get('solar', {}).get('source')})"
    return iono

"""Scraper for the TTJA JVIS public portal (jvis.ttja.ee).

JVIS is the Estonian Consumer Protection and Technical Regulatory Authority's
information system. Its public side exposes the frequency-licence register:
every registered emission in Estonia, amateur repeaters and professional
systems alike, with coordinates, frequency, power and antenna height. That is
exactly the dataset an antenna planner needs, both to find repeaters worth
working and to avoid sitting on top of a licensed user.

Two things to know before running it:

1. The portal is a server-rendered Java application whose module paths and
   form field names change between releases. Rather than hard-coding one
   layout, this scraper *discovers* the search form, maps its fields, and
   parses whatever result table comes back. `discover()` reports what it
   found so you can see if the site has moved before a scheduled run silently
   collects nothing.

2. It is a public service run for the public, not an API. The scraper
   identifies itself, honours robots.txt, requests one page at a time with a
   delay, and caches aggressively. Do not lower PLOTTER_JVIS_REQUEST_DELAY_S
   below a second, and run the refresh nightly at most.

If TTJA publishes a proper open-data export or an X-Road service, point
`PLOTTER_JVIS_MODULE_PATH` at it or write a small importer instead - it will
be faster, kinder and more reliable than any HTML scrape.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

log = logging.getLogger(__name__)

AMATEUR_HINTS = re.compile(
    r"amatöör|amatoor|amateur|harrastaja|raadioamatöör", re.I)

# Amateur allocations in Estonia, used to classify records whose service
# field is vague. Ranges in MHz.
AMATEUR_RANGES = [
    (0.1357, 0.1378), (0.472, 0.479), (1.810, 2.000), (3.500, 3.800),
    (5.2515, 5.3665), (7.000, 7.200), (10.100, 10.150), (14.000, 14.350),
    (18.068, 18.168), (21.000, 21.450), (24.890, 24.990), (28.000, 29.700),
    (50.000, 52.000), (70.000, 70.500), (144.000, 146.000),
    (430.000, 440.000), (1240.0, 1300.0), (2300.0, 2450.0),
    (3400.0, 3410.0), (5650.0, 5850.0), (10000.0, 10500.0),
    (24000.0, 24250.0), (47000.0, 47200.0), (76000.0, 81000.0),
]

# Column header synonyms seen in JVIS exports, Estonian and English.
FIELD_MAP = {
    "callsign": ["kutsung", "tunnus", "kutsungmärk", "callsign", "call sign"],
    "name": ["nimetus", "nimi", "jaama nimi", "objekt", "name", "station"],
    "service": ["teenus", "raadioteenistus", "liik", "tüüp", "service", "type"],
    "tx_mhz": ["saatesagedus", "sagedus", "tx", "väljundsagedus",
               "transmit frequency", "frequency", "sagedus mhz"],
    "rx_mhz": ["vastuvõtusagedus", "rx", "sisendsagedus",
               "receive frequency"],
    "bandwidth_khz": ["ribalaius", "kanaliriba", "bandwidth"],
    "power_w": ["võimsus", "saatevõimsus", "power"],
    "erp_w": ["erp", "eirp", "kiirgusvõimsus"],
    "antenna_height_m": ["antenni kõrgus", "kõrgus maapinnast", "antennikõrgus",
                         "antenna height", "kõrgus"],
    "azimuth_deg": ["asimuut", "suund", "azimuth"],
    "polarisation": ["polarisatsioon", "polarization", "polarisation"],
    "lat": ["laius", "laiuskraad", "latitude", "n-koordinaat", "y"],
    "lon": ["pikkus", "pikkuskraad", "longitude", "e-koordinaat", "x"],
    "licensee": ["loa omanik", "omanik", "ettevõte", "isik", "holder",
                 "licensee", "loaomanik"],
    "licence_no": ["loa number", "loa nr", "number", "licence", "luba"],
    "valid_from": ["kehtiv alates", "algus", "valid from"],
    "valid_until": ["kehtiv kuni", "lõpp", "kehtivuse lõpp", "valid until"],
    "address": ["aadress", "asukoht", "address", "location"],
    "locator": ["lokaator", "locator", "qth"],
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _num(s):
    if s is None:
        return None
    t = str(s).replace(" ", " ").strip()
    t = t.replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    return float(m.group(0)) if m else None


def parse_coordinate(value: str) -> float | None:
    """Accept decimal degrees, DMS, or Estonian L-EST97 metres."""
    if value is None:
        return None
    t = str(value).strip().replace(",", ".")
    if not t:
        return None
    # DMS like 59°26'12.3"N or 59 26 12.3
    m = re.match(r"^\s*(\d{1,3})[^\d]+(\d{1,2})[^\d]+(\d{1,2}(?:\.\d+)?)\s*([NSEWnsew])?",
                 t)
    if m and ("°" in t or "'" in t or '"' in t or t.count(" ") >= 2):
        d, mi, se = float(m.group(1)), float(m.group(2)), float(m.group(3))
        v = d + mi / 60.0 + se / 3600.0
        if (m.group(4) or "").upper() in ("S", "W"):
            v = -v
        return v
    v = _num(t)
    if v is None:
        return None
    if abs(v) > 1000:   # looks like projected metres, caller must convert
        return v
    return v


def lest97_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """Estonian L-EST97 (EPSG:3301) to WGS-84. x = easting, y = northing."""
    try:
        from pyproj import Transformer
        tr = Transformer.from_crs("EPSG:3301", "EPSG:4326", always_xy=True)
        lon, lat = tr.transform(x, y)
        return lat, lon
    except Exception:
        return 0.0, 0.0


def etrs_tm35fin_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """Finnish ETRS-TM35FIN (EPSG:3067) to WGS-84."""
    try:
        from pyproj import Transformer
        tr = Transformer.from_crs("EPSG:3067", "EPSG:4326", always_xy=True)
        lon, lat = tr.transform(x, y)
        return lat, lon
    except Exception:
        return 0.0, 0.0


def is_amateur(service: str | None, freq_mhz: float | None,
               callsign: str | None) -> bool:
    if service and AMATEUR_HINTS.search(service):
        return True
    if callsign and re.match(r"^(ES|ER|OH|OF|OG|OI)\d", callsign.strip(), re.I):
        return True
    if freq_mhz:
        for lo, hi in AMATEUR_RANGES:
            if lo <= freq_mhz <= hi:
                return True
    return False


@dataclass
class Discovery:
    ok: bool
    url: str
    form_action: str | None = None
    method: str = "get"
    fields: dict = field(default_factory=dict)
    columns: list[str] = field(default_factory=list)
    pagination: str | None = None
    export_links: list[str] = field(default_factory=list)
    note: str = ""


class JVISScraper:
    def __init__(self, base_url: str, module_path: str, user_agent: str,
                 delay_s: float = 1.5, max_pages: int = 400,
                 timeout_s: float = 45.0, respect_robots: bool = True):
        self.base_url = base_url.rstrip("/")
        self.module_path = module_path
        self.user_agent = user_agent
        self.delay_s = max(delay_s, 1.0)
        self.max_pages = max_pages
        self.timeout_s = timeout_s
        self.respect_robots = respect_robots
        self._robots: RobotFileParser | None = None
        self._last = 0.0

    # ------------------------------------------------------------- plumbing
    def _client(self):
        import httpx
        # The register is served as an AJAX fragment: a plain GET 404s, the
        # portal only renders the module for XMLHttpRequest calls.
        return httpx.Client(headers={"User-Agent": self.user_agent,
                                     "Accept-Language": "et,en;q=0.8",
                                     "X-Requested-With": "XMLHttpRequest",
                                     "Accept": "text/html, application/json"},
                            timeout=self.timeout_s, follow_redirects=True)

    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        if self._robots is None:
            self._robots = RobotFileParser()
            self._robots.set_url(urljoin(self.base_url, "/robots.txt"))
            try:
                self._robots.read()
            except Exception:
                # no robots.txt reachable: default to allowing, but stay slow
                self._robots = None
                return True
        return self._robots.can_fetch(self.user_agent, url)

    def _get(self, client, url: str, **kw):
        return self._request(client, url, "get", **kw)

    def _request(self, client, url: str, method: str = "get", **kw):
        if not self._allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")
        wait = self.delay_s - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        if method == "post":
            return client.post(url, **kw)
        return client.get(url, **kw)

    # ------------------------------------------------------------ discovery
    def discover(self) -> Discovery:
        """Find the public search form and describe it, without harvesting."""
        from bs4 import BeautifulSoup
        url = urljoin(self.base_url + "/", self.module_path.lstrip("/"))
        try:
            with self._client() as c:
                r = self._get(c, url)
        except PermissionError as e:
            return Discovery(ok=False, url=url, note=str(e))
        except Exception as e:
            return Discovery(ok=False, url=url, note=f"request failed: {e}")
        if r.status_code >= 400:
            return Discovery(ok=False, url=url,
                             note=f"HTTP {r.status_code} - the module path has "
                                  "probably moved; set PLOTTER_JVIS_MODULE_PATH")
        soup = BeautifulSoup(r.text, "lxml")
        form = None
        for f in soup.find_all("form"):
            if f.find_all(["input", "select"]):
                form = f
                break
        fields = {}
        if form:
            for el in form.find_all(["input", "select", "textarea"]):
                nm = el.get("name")
                if not nm or el.get("type") in ("hidden", "submit", "button"):
                    continue
                label = ""
                if el.get("id"):
                    lab = soup.find("label", attrs={"for": el.get("id")})
                    if lab:
                        label = _norm(lab.get_text())
                fields[nm] = label or _norm(el.get("placeholder", "")) or nm
        cols = []
        table = soup.find("table")
        if table:
            head = table.find("thead") or table
            tr = head.find("tr")
            if tr:
                cols = [_norm(th.get_text()) for th in tr.find_all(["th", "td"])]
        exports = [urljoin(url, a["href"]) for a in soup.find_all("a", href=True)
                   if re.search(r"\.(csv|xlsx?|json)(\?|$)", a["href"], re.I)
                   or re.search(r"export|download|väljastus", a["href"], re.I)]
        pag = None
        for a in soup.find_all("a", href=True):
            if re.search(r"page=|leht=|offset=", a["href"]):
                pag = a["href"]
                break
        return Discovery(ok=True, url=url,
                         form_action=(urljoin(url, form.get("action") or url)
                                      if form else None),
                         method=(form.get("method", "get").lower()
                                 if form else "get"),
                         fields=fields, columns=cols, pagination=pag,
                         export_links=exports[:10],
                         note="Form and columns discovered. Check that the "
                              "columns look like a frequency register before "
                              "running a full harvest.")

    # -------------------------------------------------------------- harvest
    def _map_columns(self, headers: list[str]) -> dict[int, str]:
        out: dict[int, str] = {}
        for i, h in enumerate(headers):
            hn = _norm(h)
            for key, names in FIELD_MAP.items():
                if any(hn == n or n in hn for n in names):
                    out.setdefault(i, key)
                    break
        return out

    def _rows_from_html(self, html: str):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        best = None
        best_n = 0
        for t in soup.find_all("table"):
            rows = t.find_all("tr")
            if len(rows) > best_n:
                best, best_n = t, len(rows)
        if best is None:
            return [], []
        rows = best.find_all("tr")
        headers = [_norm(c.get_text()) for c in rows[0].find_all(["th", "td"])]
        data = []
        for tr in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if cells and any(cells):
                data.append(cells)
        return headers, data

    def _rows_from_csv(self, text: str):
        import csv
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        except Exception:
            dialect = csv.excel
            dialect.delimiter = ";"
        rd = list(csv.reader(io.StringIO(text), dialect))
        if not rd:
            return [], []
        return [_norm(h) for h in rd[0]], rd[1:]

    def harvest(self, extra_params: dict | None = None) -> tuple[list[dict], str]:
        """Walk the result pages and return normalised transmitter records."""
        disc = self.discover()
        if not disc.ok:
            return [], disc.note

        records: list[dict] = []
        note_parts = []

        with self._client() as c:
            # Prefer a CSV/Excel export if the portal offers one - one request
            # instead of hundreds, and no HTML parsing to break.
            for link in disc.export_links:
                if not re.search(r"\.csv(\?|$)", link, re.I):
                    continue
                try:
                    r = self._get(c, link)
                    if r.status_code < 400 and len(r.text) > 200:
                        headers, rows = self._rows_from_csv(r.text)
                        cmap = self._map_columns(headers)
                        if len(cmap) >= 3 and "tx_mhz" in cmap.values():
                            for row in rows:
                                rec = self._normalise(headers, row, cmap, link)
                                if rec:
                                    records.append(rec)
                            note_parts.append(f"used CSV export {link}")
                            return records, "; ".join(note_parts)
                except Exception as e:
                    log.info("export %s not usable: %s", link, e)

            url = disc.form_action or disc.url
            is_post = disc.method == "post"
            # Empty search = list everything the register will return.
            base_fields = {name: "" for name in (disc.fields or {})}
            page = 0
            seen_signatures = set()
            while page < self.max_pages:
                params = dict(extra_params or {})
                params["page"] = page + 1
                try:
                    if is_post:
                        data = dict(base_fields)
                        data.update(params)
                        r = self._request(c, url, "post", data=data)
                    else:
                        r = self._get(c, url, params=params)
                except PermissionError as e:
                    return records, str(e)
                except Exception as e:
                    note_parts.append(f"stopped at page {page}: {e}")
                    break
                if r.status_code >= 400:
                    note_parts.append(f"HTTP {r.status_code} at page {page}")
                    break
                headers, rows = self._rows_from_html(r.text)
                if not rows:
                    break
                sig = hash(tuple(tuple(x) for x in rows[:3]))
                if sig in seen_signatures:
                    break  # pagination is not advancing
                seen_signatures.add(sig)
                cmap = self._map_columns(headers)
                if len(cmap) < 3:
                    note_parts.append(
                        "result table columns did not match any known frequency "
                        f"register layout: {headers[:12]}")
                    break
                # The public listing is a licence index (number, owner, type,
                # validity) with no frequency or coordinate column - those live
                # on the per-licence detail pages. Refuse rather than crawl
                # hundreds of pages that yield no transmitter records.
                if "tx_mhz" not in cmap.values():
                    note_parts.append(
                        "listing has no frequency column (licence-level view: "
                        f"{headers[:12]}); per-licence detail crawling is not "
                        "implemented, so nothing was harvested")
                    break
                for row in rows:
                    rec = self._normalise(headers, row, cmap, f"{url}?page={page+1}")
                    if rec:
                        records.append(rec)
                page += 1

        note_parts.append(f"{len(records)} records from {page} page(s)")
        return records, "; ".join(note_parts)

    def _normalise(self, headers, row, cmap, provenance) -> dict | None:
        vals: dict[str, str] = {}
        for i, cell in enumerate(row):
            key = cmap.get(i)
            if key:
                vals.setdefault(key, cell)
        tx = _num(vals.get("tx_mhz"))
        if tx is None:
            return None
        # Some registers publish kHz or Hz; normalise to MHz
        if tx > 1e6:
            tx = tx / 1e6
        elif tx > 1e5:
            tx = tx / 1e3

        lat = parse_coordinate(vals.get("lat"))
        lon = parse_coordinate(vals.get("lon"))
        if lat and lon and (abs(lat) > 90 or abs(lon) > 180):
            lat, lon = lest97_to_wgs84(lon, lat)
        if not lat or not lon:
            return None

        callsign = (vals.get("callsign") or "").strip() or None
        service = (vals.get("service") or "").strip() or None
        rx = _num(vals.get("rx_mhz"))
        if rx and rx > 1e5:
            rx = rx / 1e3 if rx < 1e6 else rx / 1e6

        def parse_date(v):
            if not v:
                return None
            for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
                try:
                    return dt.datetime.strptime(v.strip()[:10], fmt)
                except Exception:
                    continue
            return None

        sid = "|".join(filter(None, [
            callsign or "", f"{tx:.6f}", f"{lat:.5f}", f"{lon:.5f}",
            (vals.get("licence_no") or "").strip()]))

        return {
            "source": "jvis",
            "source_id": sid[:150],
            "callsign": callsign,
            "name": (vals.get("name") or "").strip() or None,
            "service": service,
            "is_amateur": is_amateur(service, tx, callsign),
            "tx_mhz": tx,
            "rx_mhz": rx,
            "shift_mhz": (round(rx - tx, 6) if rx else None),
            "ctcss_hz": None,
            "mode": None,
            "bandwidth_khz": _num(vals.get("bandwidth_khz")),
            "power_w": _num(vals.get("power_w")),
            "erp_w": _num(vals.get("erp_w")),
            "antenna_height_m": _num(vals.get("antenna_height_m")),
            "antenna_pattern": None,
            "azimuth_deg": _num(vals.get("azimuth_deg")),
            "polarisation": (vals.get("polarisation") or "").strip() or None,
            "lat": lat, "lon": lon, "ground_amsl_m": None,
            "locator": (vals.get("locator") or "").strip() or None,
            "licensee": (vals.get("licensee") or "").strip() or None,
            "licence_no": (vals.get("licence_no") or "").strip() or None,
            "valid_from": parse_date(vals.get("valid_from")),
            "valid_until": parse_date(vals.get("valid_until")),
            "country": "EE",
            "site_id": None,
            "raw": json.dumps({"provenance": provenance, "cells": row},
                              ensure_ascii=False)[:4000],
        }


def run(settings) -> tuple[int, str]:
    """Entry point used by the refresh CLI and the scheduled timer."""
    from . import db
    started = dt.datetime.utcnow()
    if not settings.jvis_enabled:
        db.record_run("jvis", False, 0, "disabled by configuration", started)
        return 0, "disabled"
    sc = JVISScraper(settings.jvis_base_url, settings.jvis_module_path,
                     settings.jvis_user_agent, settings.jvis_request_delay_s,
                     settings.jvis_max_pages,
                     respect_robots=getattr(settings, "jvis_respect_robots", True))
    try:
        records, note = sc.harvest()
    except Exception as e:
        db.record_run("jvis", False, 0, f"failed: {e}", started)
        return 0, f"failed: {e}"
    n = db.upsert_transmitters(records) if records else 0
    db.record_run("jvis", bool(records), n, note, started)
    return n, note

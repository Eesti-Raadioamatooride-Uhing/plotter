# Plotter

Antenna deployment planning over real topography for **Estonia and Finland**,
from 136 kHz to 24 GHz. It answers the three questions an amateur actually
asks before climbing a mast:

1. **"If I put this up here, how far will I be heard?"** — Longley-Rice
   coverage prediction over Copernicus/LiDAR terrain, rendered as a heatmap.
2. **"Will this microwave link work?"** — terrain profile, Fresnel clearance,
   diffraction, rain and gas loss, aiming angles, and the full budget both
   ways.
3. **"What does my 80 m dipole at 12 m actually do?"** — modelled elevation
   pattern over real ground, matched against the takeoff angles each hop
   distance needs and against the current ionosphere.

Plus the data an antenna planner needs beside the map: public masts and towers,
and the licensed-transmitter register — amateur repeaters and professional
systems alike.

---

## What it computes, and how much to trust it

| Mode | Model | Confidence |
|---|---|---|
| VHF/UHF area coverage | ITM (Longley-Rice) v1.2.2, point-to-point mode down every radial | Good. This is the same engine as SPLAT!, ported faithfully. |
| Microwave links | Free space + ITU-R P.526 diffraction (Deygout + Bullington + smooth-sphere) + P.676 gas + P.838/P.530 rain, cross-checked against ITM | Good on clear paths, conservative on obstructed ones. |
| Fresnel & clearance | Exact geometry with a configurable k-factor | Exact, up to terrain accuracy. |
| Antenna patterns | Analytic: wires use the real Fresnel ground reflection with a power-balance normalisation; dishes use ITU-R F.699 | Within ~1 dB of NEC for wires. Not a substitute for modelling a real array. |
| HF ground wave | Norton flat-earth surface wave over the path's ground constants | Reasonable. Assumes homogeneous ground. |
| HF skywave | Hop geometry + secant-law MUF + ITU-R P.533 absorption and excess system loss | **Indicative.** This is not VOACAP. It gets the shape of the day and the skip zone right; do not plan a contest around the dB. |

Everything reports where its terrain came from and at what resolution, and the
link report tells you when ITM has flagged a parameter out of range rather than
quietly returning a number.

---

## Quick start

`make` on its own lists every target. The common ones appear below.

### Development

```bash
make sync                                # creates .venv from uv.lock
make serve                               # uvicorn with reload on :8088
```

Open <http://127.0.0.1:8088>. Elevation tiles download on first use and cache
under `./data/cache`. If you would rather not install `uv`, the container
gives you the same thing:

```bash
make dev                                 # app only, on 127.0.0.1:8088
```

### Production (Docker, no root)

```bash
make up
```

Two containers, both run by an ordinary user: the app on an internal network
only, and Apache with ModSecurity v3 and the OWASP Core Rule Set in front of
it, published on `:8088`. Nothing needs sudo, nothing is installed on the
host, and state lives in `~/.local/share/plotter`. Put TLS in front of it
(Cloudflare, or `deploy/nginx.conf`) for anything public facing.

Then warm the caches and install the nightly refresh timer:

```bash
make warm                                # ~4 GB of DEM
make refresh                             # masts + register
make timer                               # nightly refresh at 03:20
```

`deploy/DEPLOY.md` has the full flow, including how to migrate off the old
root install.

---

## Data sources

### Terrain

| Source | Resolution | Coverage | Key needed |
|---|---|---|---|
| Copernicus GLO-30 | 30 m | Global, used by default | No |
| Maa-amet LiDAR DTM | 1 m | Estonia | No, but bulk download |
| MML *Korkeusmalli 2 m* | 2 m | Finland | Free API key |

Copernicus is enough for coverage maps and HF. For microwave link planning,
point `PLOTTER_LIDAR_EE_DIR` / `PLOTTER_LIDAR_FI_DIR` at a directory of
downloaded national LiDAR tiles (or a `.vrt` over them) and the link tool will
prefer them automatically — a 30 m DEM will not tell you the truth about
whether a 5.8 GHz path clears a tree line.

### Basemaps

OpenStreetMap, OpenTopoMap, Maa-amet base map / relief / orthophoto, the
Finnish topographic map, and Esri satellite imagery. All keyless.

### Masts and towers

OpenStreetMap via Overpass (both countries, refreshed nightly) and the Estonian
ETAK topographic database via the Land Board's WFS, which carries surveyed
heights.

### The TTJA JVIS frequency register

This is the one source that needs a caveat.

JVIS (`jvis.ttja.ee`) is the Estonian Consumer Protection and Technical
Regulatory Authority's information system. Its public side carries the
frequency-licence register — every registered emission in Estonia with
coordinates, frequency, power and antenna height. That is exactly what an
antenna planner wants, both to find repeaters and to avoid sitting on a
licensed user.

It is a server-rendered Java application, not an API, and its module paths and
form field names change between releases. So the scraper in
`plotter/core/data/jvis.py` **discovers** the search form rather than hard-coding
one layout: it maps form fields and result-table columns against a synonym
table (Estonian and English), prefers a CSV export if the portal offers one,
and reports what it found.

**Check it before you trust it:**

```bash
plotter-refresh discover
```

or click *Check the JVIS portal* in the Data tab. If the module has moved, set
`PLOTTER_JVIS_MODULE_PATH` to the right path. If the columns come back looking
nothing like a frequency register, the harvest will refuse to run rather than
silently collecting rubbish.

The scraper honours `robots.txt`, identifies itself (set a real contact address
in `PLOTTER_JVIS_USER_AGENT`), and waits between requests. Do not drop
`PLOTTER_JVIS_REQUEST_DELAY_S` below 1 second, and do not run the harvest more
than nightly. If TTJA ever publishes a proper open-data export or an X-Road
service, point the importer at that instead — it will be faster, kinder and
more reliable than any HTML scrape.

> I was not able to verify the live JVIS layout while building this — the
> portal disallows automated fetching from the environment I built in, and I
> did not work around that. The discovery step exists precisely so you can
> check it against the real site in one command on your own server, where you
> are the one making the request.

### Ionosphere

Live foF2 from the Sodankylä (or Juliusruh/Průhonice) ionosonde via the GIRO
DIDBase service, and sunspot number / Kp from NOAA SWPC, with a diurnal and
seasonal model as the fallback when the network is unavailable.

---

## Layout

```
plotter/
  core/
    geodesy.py              WGS-84 Vincenty, great circles, bearings
    terrain/
      providers.py          Copernicus, WCS, local LiDAR, cache
      profile.py            path profiles, Fresnel, clearance, k-factor
    propagation/
      itm.py                Longley-Rice v1.2.2, faithful port
      diffraction.py        ITU-R P.526 knife edge / Deygout / Bullington
      atmosphere.py         P.676 gas, P.838 rain, P.530 multipath
      hf.py                 ground wave, NVIS, hop geometry, MUF
      linkbudget.py         assembles a point-to-point answer
      coverage.py           radial sweep + PNG/GeoJSON rendering
    antennas/
      patterns.py           wires over real ground, yagis, dishes, sectors
      library.py            band plan and antenna presets
    data/
      db.py                 SQLite store for sites and transmitters
      jvis.py               TTJA JVIS scraper (see caveat above)
      masts.py              OSM Overpass + Estonian ETAK
      ionosphere.py         GIRO ionosonde + NOAA solar indices
  api/                      FastAPI routes and request models
  static/                   the web UI (Leaflet, no build step)
deploy/                     deploy script, user timer, nginx reference
Makefile                    the commands above, self-documenting
tests/                      reference-value tests
```

## API

Everything the UI does is available over HTTP. `GET /docs` gives the full
OpenAPI browser. The interesting ones:

```
POST /api/link           full point-to-point analysis with terrain profile
POST /api/link/best-heights   clearance matrix over both antenna heights
POST /api/coverage       area prediction; returns a PNG overlay + contours
POST /api/hf             HF prediction with the antenna's elevation pattern
GET  /api/horizon        terrain horizon ring as GeoJSON
GET  /api/profile        terrain profile between two points
GET  /api/elevation      one point
GET  /api/transmitters   register query, bbox/band/callsign filters
GET  /api/sites          masts and towers
GET  /api/antenna/pattern    elevation and azimuth cuts for any preset
GET  /api/registry/jvis/discover   inspect the JVIS portal layout
```

Example — a 5.8 GHz link from the command line:

```bash
plotter-refresh link 59.4370 24.7536 59.3960 24.6650 \
    --freq 5760 --h1 25 --h2 20 --antenna dish_600 --high-res
```

## Configuration

All settings are environment variables prefixed `PLOTTER_`, or a `.env` file.
See `.env.example`. The ones worth knowing:

| Variable | Default | Notes |
|---|---|---|
| `PLOTTER_LIDAR_EE_DIR` | — | Directory of Maa-amet 1 m tiles or a `.vrt` |
| `PLOTTER_LIDAR_FI_DIR` | — | Directory of MML 2 m tiles |
| `PLOTTER_MML_API_KEY` | — | For on-demand Finnish elevation |
| `PLOTTER_JVIS_MODULE_PATH` | `/modules/side/raadiosagedus/avalik` | Set this if the portal moves |
| `PLOTTER_JVIS_USER_AGENT` | generic | **Put a real contact address here** |
| `PLOTTER_MAX_COVERAGE_RANGE_KM` | 2000 | ITM's own documented ceiling |
| `PLOTTER_MAX_COVERAGE_POINTS` | 500000 | ITM solves per sweep; the steps coarsen to fit |
| `PLOTTER_COVERAGE_WORKERS` | 8 | Threads for the radial sweep |

## Tests

```bash
make check                               # pytest plus pyflakes
```

The tests check against published reference values where they exist: free-space
loss at 1 GHz/1 km, the ITU-R P.526 knife-edge curve, P.838 rain coefficients,
the `4.12·√h` horizon rule, dish gain against the aperture formula, and the
Vincenty round trip. The propagation models are also checked for the
monotonicity properties they must have — loss rising with distance, frequency
and terrain roughness — which catches most porting mistakes.

## Legal and licensing note

Transmitting requires a licence. The register data here is for planning and
coordination; it is a snapshot of a public register and may be stale or
incomplete. Check power and EIRP limits for your band and country before
transmitting, and check whether a site needs aviation obstacle marking or
planning permission before you erect anything on it. Plotter tells you what
should work on the air; it does not tell you what you are allowed to build.

# Plotter — context for Claude Code

Antenna deployment planning over real topography for Estonia and Finland,
136 kHz to 24 GHz. FastAPI backend, vanilla Leaflet frontend, no build step.
Deploys to a Linux server via venv + systemd.

## Deploy target

`root@172.16.0.74`, reachable by SSH key from the user's Windows machine.
See `deploy/DEPLOY.md` for the rsync + `install.sh` flow. The installer is
idempotent — re-run it after every sync to upgrade.

## Layout

```
plotter/core/geodesy.py            WGS-84 Vincenty, great circles, bearings
plotter/core/terrain/providers.py  Copernicus GLO-30, WCS, local LiDAR, cache
plotter/core/terrain/profile.py    path profiles, Fresnel, clearance, k-factor
plotter/core/propagation/itm.py    Longley-Rice v1.2.2 port  <- the load-bearing file
plotter/core/propagation/diffraction.py  ITU-R P.526 knife edge / Deygout / Bullington
plotter/core/propagation/atmosphere.py   P.676 gas, P.838 rain, P.530 multipath
plotter/core/propagation/hf.py     ground wave, NVIS, hop geometry, MUF, P.533 absorption
plotter/core/propagation/linkbudget.py   assembles a point-to-point answer
plotter/core/propagation/coverage.py     radial sweep + PNG/GeoJSON rendering
plotter/core/antennas/patterns.py  wires over real ground, yagis, dishes, sectors
plotter/core/antennas/library.py   band plan (EE/FI) and antenna presets
plotter/core/data/                 SQLite store, JVIS scraper, OSM/ETAK masts, ionosonde
plotter/api/routes.py              all HTTP endpoints
plotter/static/                    index.html + style.css + app.js, no bundler
```

## Conventions

- Frequencies in MHz, distances in metres internally and km at the API edge,
  gains in dBi, levels in dBm, bearings in degrees clockwise from true north.
- Every propagation result carries its provenance: which terrain source, what
  resolution, and any ITM out-of-range warning. Do not drop these when
  refactoring — they are how the user knows whether to trust a number.
- `itm.py` is a faithful port of the NTIA reference implementation. The C
  `static` variables live on the `ITM` solver instance so the coverage sweep
  can run paths in parallel. **Do not "clean up" the algorithm** — variable
  names and structure deliberately mirror the reference so it can be diffed
  against it. One solver per path.
- Antenna gain is real gain, not directivity: `_calibrate()` charges the
  antenna for power the ground absorbs. That is why a low 80 m dipole comes
  out near 6.9 dBi and not 8.6.

## Testing

```bash
python -m pytest tests -q          # 33 tests, all passing
python -m pyflakes plotter tests    # clean
```

Tests check against published reference values (free-space loss at 1 GHz/1 km,
the P.526 knife-edge curve, P.838 rain coefficients, the 4.12·√h horizon rule,
dish gain vs the aperture formula) plus monotonicity properties that catch
porting mistakes.

## Open items

1. **The JVIS scraper is unverified against the live portal.** `jvis.ttja.ee`
   disallowed automated fetching from the environment it was written in, so
   `plotter/core/data/jvis.py` discovers the search form and maps result
   columns against an Estonian/English synonym table rather than hard-coding
   a layout. Run `plotter-refresh discover` against the real site, then tune
   `FIELD_MAP` and `PLOTTER_JVIS_MODULE_PATH` to match what comes back. It
   refuses to harvest if the columns do not look like a frequency register,
   so a wrong guess fails loudly rather than collecting rubbish.
   Keep the robots.txt check, the identifying User-Agent and the request
   delay — this is a public service, not an API.

2. **High-resolution terrain is wired but not fed.** Copernicus GLO-30 works
   out of the box. For microwave link planning, download Maa-amet 1 m and MML
   2 m tiles and point `PLOTTER_LIDAR_EE_DIR` / `PLOTTER_LIDAR_FI_DIR` at them
   (a `.vrt` over the tiles works). A 30 m DEM will not tell you the truth
   about whether a 5.8 GHz path clears a tree line.

3. **HF skywave is indicative, not VOACAP.** Hop geometry, secant-law MUF and
   P.533 absorption. It gets the shape of the day and the skip zone right.
   If it needs to be better, the path is real ionosonde ingest (the GIRO
   hook already exists in `data/ionosphere.py`) or wrapping VOACAP.

4. Clutter is a single flat height added to bare terrain. Real land-cover
   data (Corine, or Estonian ETAK forest polygons) would improve VHF/UHF
   coverage accuracy more than anything else on this list.

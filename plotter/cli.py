"""Command line utilities: data refresh, cache warming, quick calculations."""
from __future__ import annotations

import argparse
import json
import logging
import sys


def cmd_refresh(args) -> int:
    from .config import settings
    from .core.data import db, jvis, masts
    db.init(settings.database_url)
    rc = 0
    if args.source in ("all", "masts"):
        n, msg = masts.run(settings, include_etak=not args.no_etak)
        print(f"masts: {n} records - {msg}")
        rc |= 0 if n else 1
    if args.source in ("all", "jvis"):
        n, msg = jvis.run(settings)
        print(f"jvis: {n} records - {msg}")
        if not n:
            print("  (run 'plotter-refresh discover' to see what the portal "
                  "looks like right now)", file=sys.stderr)
    return 0 if args.tolerant else rc


def cmd_discover(args) -> int:
    from .config import settings
    from .core.data.jvis import JVISScraper
    sc = JVISScraper(settings.jvis_base_url, settings.jvis_module_path,
                     settings.jvis_user_agent, settings.jvis_request_delay_s)
    print(json.dumps(sc.discover().__dict__, indent=2, ensure_ascii=False))
    return 0


def cmd_warm(args) -> int:
    """Pre-download the Copernicus tiles covering Estonia and Finland."""
    from .config import settings
    from .core.terrain.providers import CopernicusDEM, TileFetchError
    from pathlib import Path
    dem = CopernicusDEM(Path(settings.cache_dir), settings.copernicus_bucket)
    got = missing = failed = 0
    for lat in range(args.min_lat, args.max_lat):
        for lon in range(args.min_lon, args.max_lon):
            try:
                p = dem._tile_path(lat, lon)
            except TileFetchError as exc:
                failed += 1
                print(f"  {dem.tile_name(lat, lon)}  FAILED: {exc}")
                continue
            if p:
                got += 1
                print(f"  {dem.tile_name(lat, lon)}  ok")
            else:
                missing += 1
    print(f"{got} tiles cached, {missing} unavailable (sea or outside coverage)"
          + (f", {failed} failed and will be retried next run" if failed else ""))
    return 0


def cmd_link(args) -> int:
    from .config import settings
    from .core.antennas.library import build_antenna
    from .core.propagation import linkbudget as lb
    from .core.terrain.profile import build_profile
    from .core.terrain.providers import Terrain
    terrain = Terrain.from_settings(settings)
    prof = build_profile(terrain, args.lat1, args.lon1, args.lat2, args.lon2,
                         high_res=args.high_res)
    ant = build_antenna(args.antenna, args.freq)
    a = lb.Endpoint("A", args.lat1, args.lon1, args.h1, ant, args.power)
    b = lb.Endpoint("B", args.lat2, args.lon2, args.h2, ant, args.power)
    r = lb.evaluate(prof, a, b, args.freq, include_profile=False)
    print(json.dumps(lb.to_dict(r), indent=2, default=str))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="plotter-refresh",
                                description="Plotter maintenance commands")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("refresh", help="refresh mast and register data")
    r.add_argument("source", nargs="?", default="all",
                   choices=["all", "masts", "jvis"])
    r.add_argument("--no-etak", action="store_true")
    r.add_argument("--tolerant", action="store_true",
                   help="always exit 0, for unattended timers")
    r.set_defaults(func=cmd_refresh)

    d = sub.add_parser("discover", help="inspect the JVIS public module")
    d.set_defaults(func=cmd_discover)

    w = sub.add_parser("warm", help="pre-download elevation tiles")
    w.add_argument("--min-lat", type=int, default=57)
    w.add_argument("--max-lat", type=int, default=71)
    w.add_argument("--min-lon", type=int, default=19)
    w.add_argument("--max-lon", type=int, default=32)
    w.set_defaults(func=cmd_warm)

    l = sub.add_parser("link", help="one path from the command line")
    for a in ("lat1", "lon1", "lat2", "lon2"):
        l.add_argument(a, type=float)
    l.add_argument("--freq", type=float, default=5760.0)
    l.add_argument("--h1", type=float, default=20.0)
    l.add_argument("--h2", type=float, default=20.0)
    l.add_argument("--power", type=float, default=0.5)
    l.add_argument("--antenna", default="dish_600")
    l.add_argument("--high-res", action="store_true")
    l.set_defaults(func=cmd_link)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

"""Terrain path profiles and the geometry that hangs off them."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..geodesy import great_circle_points, vincenty_inverse

C = 299_792_458.0
EARTH_R = 6_371_008.8


@dataclass
class PathProfile:
    lats: np.ndarray
    lons: np.ndarray
    distances_m: np.ndarray      # cumulative along the path
    elevations_m: np.ndarray     # bare terrain / DSM height above MSL
    clutter_m: np.ndarray        # added obstruction height (trees, buildings)
    source: str
    resolution_m: float
    total_distance_m: float
    bearing_deg: float
    reverse_bearing_deg: float

    @property
    def spacing_m(self) -> float:
        return float(self.total_distance_m / max(1, len(self.distances_m) - 1))

    @property
    def surface_m(self) -> np.ndarray:
        """Terrain plus clutter - what actually blocks the path."""
        return self.elevations_m + self.clutter_m

    def itm_array(self, use_clutter: bool = False) -> list[float]:
        """Profile in the array layout ITM expects."""
        z = self.surface_m if use_clutter else self.elevations_m
        return [float(len(z) - 1), self.spacing_m] + [float(v) for v in z]


def sample_count(distance_m: float, resolution_m: float,
                 max_points: int = 2000, min_points: int = 32) -> int:
    n = int(distance_m / max(resolution_m, 1.0)) + 1
    return int(min(max(n, min_points), max_points))


def _despike_endpoints(z: np.ndarray) -> None:
    """Flatten a single anomalous first/last terrain sample, in place.

    The two path endpoints are the antenna sites, sampled at the exact corner
    of a terrain tile where edge effects and nodata fills bite. A lone sample
    tens of metres above its inward neighbours is such an artifact - and it is
    doubly damaging because the link budget reads the endpoint as the antenna's
    ground height. A real hill would lift the neighbours too, so compare each
    endpoint against the median of the few samples just inside it and only
    correct a clear outlier.
    """
    n = len(z)
    if n < 6:
        return
    step = float(np.median(np.abs(np.diff(z[1:-1])))) if n > 3 else 1.0
    tol = max(20.0, 10.0 * step)
    lead = float(np.median(z[1:5]))
    if abs(float(z[0]) - lead) > tol:
        z[0] = lead
    tail = float(np.median(z[-5:-1]))
    if abs(float(z[-1]) - tail) > tol:
        z[-1] = tail


def build_profile(terrain, lat1: float, lon1: float, lat2: float, lon2: float,
                  *, points: int | None = None, high_res: bool = False,
                  clutter_m: float = 0.0) -> PathProfile:
    dist, fwd, rev = vincenty_inverse(lat1, lon1, lat2, lon2)
    if points is None:
        target = 15.0 if high_res else 60.0
        points = sample_count(dist, target)
    pts = great_circle_points(lat1, lon1, lat2, lon2, points)
    lats = np.array([p[0] for p in pts])
    lons = np.array([p[1] for p in pts])
    s = terrain.sample(lats, lons, high_res=high_res)
    _despike_endpoints(s.values)
    d = np.linspace(0.0, dist, points)
    cl = np.full(points, float(clutter_m))
    cl[0] = 0.0
    cl[-1] = 0.0
    return PathProfile(lats=lats, lons=lons, distances_m=d,
                       elevations_m=s.values, clutter_m=cl,
                       source=s.source, resolution_m=s.resolution_m,
                       total_distance_m=dist, bearing_deg=fwd,
                       reverse_bearing_deg=rev)


# ---------------------------------------------------------------- geometry

def earth_bulge_m(d1_m: np.ndarray | float, d2_m: np.ndarray | float,
                  k: float = 4.0 / 3.0) -> np.ndarray | float:
    """Apparent rise of the earth between two points, k-factor corrected."""
    return (np.asarray(d1_m) * np.asarray(d2_m)) / (2.0 * k * EARTH_R)


def fresnel_radius_m(d1_m, d2_m, freq_hz: float, n: int = 1):
    """Radius of the n-th Fresnel ellipsoid at a point splitting the path."""
    d1 = np.asarray(d1_m, dtype=float)
    d2 = np.asarray(d2_m, dtype=float)
    lam = C / freq_hz
    total = d1 + d2
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.sqrt(n * lam * d1 * d2 / total)
    return np.nan_to_num(r)


def free_space_loss_db(distance_m: float, freq_mhz: float) -> float:
    if distance_m <= 0:
        return 0.0
    return 32.44778 + 20 * math.log10(freq_mhz) + 20 * math.log10(distance_m / 1000.0)


@dataclass
class Obstruction:
    index: int
    distance_m: float
    lat: float
    lon: float
    terrain_m: float
    los_m: float
    clearance_m: float          # negative means the path is blocked
    fresnel_radius_m: float
    fresnel_fraction: float     # clearance / F1 radius
    v: float                    # Fresnel-Kirchhoff diffraction parameter


@dataclass
class LinkGeometry:
    profile: PathProfile
    tx_height_m: float
    rx_height_m: float
    tx_amsl_m: float
    rx_amsl_m: float
    freq_mhz: float
    k_factor: float
    los_line_m: np.ndarray       # straight line between antennas
    effective_terrain_m: np.ndarray  # terrain + clutter + earth bulge
    clearance_m: np.ndarray
    f1_m: np.ndarray
    fresnel_fraction: np.ndarray
    worst: Obstruction
    obstructions: list[Obstruction] = field(default_factory=list)
    is_los: bool = True
    tx_tilt_deg: float = 0.0
    rx_tilt_deg: float = 0.0

    @property
    def min_fresnel_fraction(self) -> float:
        return float(np.min(self.fresnel_fraction[1:-1])) if len(self.f1_m) > 2 else 1.0


def analyse_link(profile: PathProfile, tx_agl_m: float, rx_agl_m: float,
                 freq_mhz: float, k_factor: float = 4.0 / 3.0,
                 use_clutter: bool = True) -> LinkGeometry:
    """Full point-to-point geometry: LOS line, bulge, Fresnel, obstructions."""
    d = profile.distances_m
    total = profile.total_distance_m
    terr = profile.surface_m if use_clutter else profile.elevations_m
    tx_amsl = float(profile.elevations_m[0]) + tx_agl_m
    rx_amsl = float(profile.elevations_m[-1]) + rx_agl_m

    los = tx_amsl + (rx_amsl - tx_amsl) * (d / max(total, 1e-9))
    bulge = earth_bulge_m(d, total - d, k_factor)
    eff = terr + bulge
    clearance = los - eff

    f1 = fresnel_radius_m(d, total - d, freq_mhz * 1e6, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        frac = np.where(f1 > 0, clearance / f1, np.inf)
    frac = np.nan_to_num(frac, nan=np.inf, posinf=99.0, neginf=-99.0)

    lam = C / (freq_mhz * 1e6)
    d1 = np.maximum(d, 1e-6)
    d2 = np.maximum(total - d, 1e-6)
    h = -clearance  # positive when the obstacle pokes above the LOS line
    v = h * np.sqrt(np.maximum(2.0 * (d1 + d2) / (lam * d1 * d2), 0.0))

    interior = slice(1, -1) if len(d) > 2 else slice(0, len(d))
    j = int(np.argmin(frac[interior])) + (1 if len(d) > 2 else 0)

    def mk(i: int) -> Obstruction:
        return Obstruction(index=i, distance_m=float(d[i]),
                           lat=float(profile.lats[i]), lon=float(profile.lons[i]),
                           terrain_m=float(terr[i]), los_m=float(los[i]),
                           clearance_m=float(clearance[i]),
                           fresnel_radius_m=float(f1[i]),
                           fresnel_fraction=float(frac[i]), v=float(v[i]))

    worst = mk(j)
    obstructions = [mk(i) for i in range(1, len(d) - 1) if clearance[i] < 0]

    tx_tilt = math.degrees(math.atan2(rx_amsl - tx_amsl, total)) if total else 0.0
    rx_tilt = math.degrees(math.atan2(tx_amsl - rx_amsl, total)) if total else 0.0

    return LinkGeometry(profile=profile, tx_height_m=tx_agl_m, rx_height_m=rx_agl_m,
                        tx_amsl_m=tx_amsl, rx_amsl_m=rx_amsl, freq_mhz=freq_mhz,
                        k_factor=k_factor, los_line_m=los, effective_terrain_m=eff,
                        clearance_m=clearance, f1_m=f1, fresnel_fraction=frac,
                        worst=worst, obstructions=obstructions,
                        is_los=bool(np.all(clearance[1:-1] > 0)) if len(d) > 2 else True,
                        tx_tilt_deg=tx_tilt, rx_tilt_deg=rx_tilt)


def required_height_for_clearance(profile: PathProfile, other_agl_m: float,
                                  freq_mhz: float, at_tx: bool = True,
                                  fresnel_fraction: float = 0.6,
                                  k_factor: float = 4.0 / 3.0,
                                  max_h: float = 300.0) -> float:
    """Lowest antenna height that achieves the wanted Fresnel clearance."""
    lo, hi = 0.0, max_h
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        tx = mid if at_tx else other_agl_m
        rx = other_agl_m if at_tx else mid
        g = analyse_link(profile, tx, rx, freq_mhz, k_factor)
        if g.min_fresnel_fraction >= fresnel_fraction:
            hi = mid
        else:
            lo = mid
        if hi - lo < 0.05:
            break
    return hi


def horizon_distance_km(height_m: float, k_factor: float = 4.0 / 3.0) -> float:
    """Radio horizon for an antenna at `height_m` above ground."""
    return math.sqrt(2.0 * k_factor * EARTH_R * max(height_m, 0.0)) / 1000.0

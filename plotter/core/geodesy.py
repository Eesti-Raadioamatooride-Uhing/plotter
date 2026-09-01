"""Geodesy helpers: WGS-84 inverse/direct problems, bearings, path sampling.

Everything here works in degrees for lat/lon and metres for distance, and
returns bearings in degrees clockwise from true north.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# WGS-84
A = 6378137.0
F = 1.0 / 298.257223563
B = A * (1.0 - F)

MEAN_EARTH_RADIUS = 6371008.8  # metres, IUGG mean radius


@dataclass(frozen=True)
class Point:
    lat: float
    lon: float

    def as_tuple(self) -> tuple[float, float]:
        return (self.lat, self.lon)


def vincenty_inverse(lat1: float, lon1: float, lat2: float, lon2: float
                     ) -> tuple[float, float, float]:
    """Distance (m), bearing from 1 to 2, bearing from 2 back to 1 (deg).

    The third value is the back-azimuth, which is what you point the far end
    of a link at - not the "final bearing" of the forward path.
    """
    if abs(lat1 - lat2) < 1e-12 and abs(lon1 - lon2) < 1e-12:
        return 0.0, 0.0, 0.0

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    L = math.radians(lon2 - lon1)
    U1 = math.atan((1 - F) * math.tan(phi1))
    U2 = math.atan((1 - F) * math.tan(phi2))
    sinU1, cosU1 = math.sin(U1), math.cos(U1)
    sinU2, cosU2 = math.sin(U2), math.cos(U2)

    lam = L
    sin_sigma = cos_sigma = sigma = cos_sq_alpha = cos2_sigma_m = 0.0
    for _ in range(200):
        sin_lam, cos_lam = math.sin(lam), math.cos(lam)
        sin_sigma = math.sqrt((cosU2 * sin_lam) ** 2 +
                              (cosU1 * sinU2 - sinU1 * cosU2 * cos_lam) ** 2)
        if sin_sigma == 0:
            return 0.0, 0.0, 0.0
        cos_sigma = sinU1 * sinU2 + cosU1 * cosU2 * cos_lam
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cosU1 * cosU2 * sin_lam / sin_sigma
        cos_sq_alpha = 1 - sin_alpha ** 2
        cos2_sigma_m = (cos_sigma - 2 * sinU1 * sinU2 / cos_sq_alpha
                        if cos_sq_alpha != 0 else 0.0)
        C = F / 16 * cos_sq_alpha * (4 + F * (4 - 3 * cos_sq_alpha))
        lam_prev = lam
        lam = L + (1 - C) * F * sin_alpha * (
            sigma + C * sin_sigma * (cos2_sigma_m + C * cos_sigma *
                                     (-1 + 2 * cos2_sigma_m ** 2)))
        if abs(lam - lam_prev) < 1e-12:
            break
    else:  # pragma: no cover - antipodal fallback
        return haversine(lat1, lon1, lat2, lon2), initial_bearing(lat1, lon1, lat2, lon2), 0.0

    u_sq = cos_sq_alpha * (A * A - B * B) / (B * B)
    Ac = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    Bc = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))
    d_sigma = Bc * sin_sigma * (
        cos2_sigma_m + Bc / 4 * (
            cos_sigma * (-1 + 2 * cos2_sigma_m ** 2) -
            Bc / 6 * cos2_sigma_m * (-3 + 4 * sin_sigma ** 2) *
            (-3 + 4 * cos2_sigma_m ** 2)))
    s = B * Ac * (sigma - d_sigma)

    sin_lam, cos_lam = math.sin(lam), math.cos(lam)
    fwd = math.atan2(cosU2 * sin_lam, cosU1 * sinU2 - sinU1 * cosU2 * cos_lam)
    rev = math.atan2(cosU1 * sin_lam, -sinU1 * cosU2 + cosU1 * sinU2 * cos_lam)
    return (s, math.degrees(fwd) % 360.0,
            (math.degrees(rev) + 180.0) % 360.0)


def vincenty_direct(lat1: float, lon1: float, bearing_deg: float, distance_m: float
                    ) -> tuple[float, float, float]:
    """Destination lat, lon and final bearing given start, bearing, distance."""
    alpha1 = math.radians(bearing_deg)
    phi1 = math.radians(lat1)
    lam1 = math.radians(lon1)

    U1 = math.atan((1 - F) * math.tan(phi1))
    sinU1, cosU1 = math.sin(U1), math.cos(U1)
    sigma1 = math.atan2(math.tan(U1), math.cos(alpha1))
    sin_alpha = cosU1 * math.sin(alpha1)
    cos_sq_alpha = 1 - sin_alpha ** 2
    u_sq = cos_sq_alpha * (A * A - B * B) / (B * B)
    Ac = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    Bc = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))

    sigma = distance_m / (B * Ac)
    for _ in range(200):
        cos2_sigma_m = math.cos(2 * sigma1 + sigma)
        sin_sigma, cos_sigma = math.sin(sigma), math.cos(sigma)
        d_sigma = Bc * sin_sigma * (
            cos2_sigma_m + Bc / 4 * (
                cos_sigma * (-1 + 2 * cos2_sigma_m ** 2) -
                Bc / 6 * cos2_sigma_m * (-3 + 4 * sin_sigma ** 2) *
                (-3 + 4 * cos2_sigma_m ** 2)))
        sigma_prev = sigma
        sigma = distance_m / (B * Ac) + d_sigma
        if abs(sigma - sigma_prev) < 1e-12:
            break

    sin_sigma, cos_sigma = math.sin(sigma), math.cos(sigma)
    cos2_sigma_m = math.cos(2 * sigma1 + sigma)
    tmp = sinU1 * sin_sigma - cosU1 * cos_sigma * math.cos(alpha1)
    phi2 = math.atan2(sinU1 * cos_sigma + cosU1 * sin_sigma * math.cos(alpha1),
                      (1 - F) * math.sqrt(sin_alpha ** 2 + tmp ** 2))
    lam = math.atan2(sin_sigma * math.sin(alpha1),
                     cosU1 * cos_sigma - sinU1 * sin_sigma * math.cos(alpha1))
    C = F / 16 * cos_sq_alpha * (4 + F * (4 - 3 * cos_sq_alpha))
    L = lam - (1 - C) * F * sin_alpha * (
        sigma + C * sin_sigma * (cos2_sigma_m + C * cos_sigma *
                                 (-1 + 2 * cos2_sigma_m ** 2)))
    lon2 = (math.degrees(lam1 + L) + 540) % 360 - 180
    alpha2 = math.atan2(sin_alpha, -tmp)
    return math.degrees(phi2), lon2, math.degrees(alpha2) % 360.0


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Spherical distance in metres. Fast; use where 0.5% error is fine."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * MEAN_EARTH_RADIUS * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def great_circle_points(lat1: float, lon1: float, lat2: float, lon2: float,
                        n: int) -> list[tuple[float, float]]:
    """`n` points along the great circle, inclusive of both endpoints."""
    if n < 2:
        return [(lat1, lon1), (lat2, lon2)]
    p1, l1 = math.radians(lat1), math.radians(lon1)
    p2, l2 = math.radians(lat2), math.radians(lon2)
    d = 2 * math.asin(min(1.0, math.sqrt(
        math.sin((p2 - p1) / 2) ** 2 +
        math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2)))
    if d == 0:
        return [(lat1, lon1)] * n
    out = []
    for i in range(n):
        f = i / (n - 1)
        a = math.sin((1 - f) * d) / math.sin(d)
        b = math.sin(f * d) / math.sin(d)
        x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
        y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
        z = a * math.sin(p1) + b * math.sin(p2)
        out.append((math.degrees(math.atan2(z, math.hypot(x, y))),
                    math.degrees(math.atan2(y, x))))
    return out


def destination(lat: float, lon: float, bearing_deg: float, distance_m: float
                ) -> tuple[float, float]:
    """Spherical destination point - cheap version of vincenty_direct."""
    d = distance_m / MEAN_EARTH_RADIUS
    br = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) +
                   math.cos(p1) * math.sin(d) * math.cos(br))
    l2 = l1 + math.atan2(math.sin(br) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def bbox_around(lat: float, lon: float, radius_m: float
                ) -> tuple[float, float, float, float]:
    """(min_lat, min_lon, max_lat, max_lon) bounding a circle."""
    dlat = math.degrees(radius_m / MEAN_EARTH_RADIUS)
    dlon = math.degrees(radius_m / (MEAN_EARTH_RADIUS *
                                    max(math.cos(math.radians(lat)), 1e-6)))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)

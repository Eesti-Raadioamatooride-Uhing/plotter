"""Diffraction losses - ITU-R P.526 style.

Used for the microwave link mode, where ITM's statistical treatment is less
useful than an explicit obstacle-by-obstacle answer the user can point at.
"""
from __future__ import annotations

import math

import numpy as np

C = 299_792_458.0
EARTH_R = 6_371_008.8


def knife_edge_db(v: float) -> float:
    """ITU-R P.526 single knife-edge, valid for v > -0.78."""
    if v <= -0.78:
        return 0.0
    return 6.9 + 20.0 * math.log10(math.sqrt((v - 0.1) ** 2 + 1.0) + v - 0.1)


def fresnel_v(h_m: float, d1_m: float, d2_m: float, freq_mhz: float) -> float:
    """Fresnel-Kirchhoff parameter. h is obstacle height above the LOS line."""
    lam = C / (freq_mhz * 1e6)
    d1 = max(d1_m, 1e-6)
    d2 = max(d2_m, 1e-6)
    return h_m * math.sqrt(2.0 * (d1 + d2) / (lam * d1 * d2))


def bullington_db(distances_m, heights_m, tx_amsl: float, rx_amsl: float,
                  freq_mhz: float, k_factor: float = 4.0 / 3.0) -> tuple[float, float]:
    """ITU-R P.526 Bullington construction.

    Returns (loss_dB, equivalent v). Robust and never over-predicts the way a
    naive multi-edge sum can, but under-predicts on paths with several
    well-separated ridges - which is why we also run Deygout.
    """
    d = np.asarray(distances_m, dtype=float)
    h = np.asarray(heights_m, dtype=float)
    total = float(d[-1])
    if total <= 0:
        return 0.0, -99.0
    bulge = d * (total - d) / (2.0 * k_factor * EARTH_R)
    heff = h + bulge

    interior = slice(1, -1)
    dd = d[interior]
    hh = heff[interior]
    if dd.size == 0:
        return 0.0, -99.0

    # steepest slope seen from each end
    st = np.max((hh - tx_amsl) / np.maximum(dd, 1e-9))
    sr = np.max((hh - rx_amsl) / np.maximum(total - dd, 1e-9))
    str_ = (rx_amsl - tx_amsl) / total

    if st < str_:  # line of sight, use the largest v directly
        v = np.max((hh - (tx_amsl + str_ * dd)) *
                   np.sqrt(2.0 * total / ((C / (freq_mhz * 1e6)) *
                                          np.maximum(dd, 1e-9) *
                                          np.maximum(total - dd, 1e-9))))
        return knife_edge_db(float(v)), float(v)

    # virtual Bullington point
    db = (rx_amsl - tx_amsl + sr * total) / (st + sr)
    hb = tx_amsl + st * db
    hstar = hb - (tx_amsl * (total - db) + rx_amsl * db) / total
    v = hstar * math.sqrt(2.0 * total /
                          ((C / (freq_mhz * 1e6)) * max(db, 1e-9) *
                           max(total - db, 1e-9)))
    return knife_edge_db(v), v


def deygout_db(distances_m, heights_m, tx_amsl: float, rx_amsl: float,
               freq_mhz: float, k_factor: float = 4.0 / 3.0,
               max_edges: int = 3) -> tuple[float, list[dict]]:
    """Deygout multiple knife-edge with the Causebrook correction.

    Returns (loss_dB, list of the edges it used). This is the number quoted in
    the link report because it names the actual hills that are in the way.
    """
    d = np.asarray(distances_m, dtype=float)
    h = np.asarray(heights_m, dtype=float)
    total = float(d[-1])
    if total <= 0 or d.size < 3:
        return 0.0, []
    bulge = d * (total - d) / (2.0 * k_factor * EARTH_R)
    heff = h + bulge
    lam = C / (freq_mhz * 1e6)
    edges: list[dict] = []

    def main_edge(i0: int, i1: int, ha: float, hb: float):
        if i1 - i0 < 2:
            return None
        seg = slice(i0 + 1, i1)
        dd = d[seg] - d[i0]
        dr = d[i1] - d[seg]
        los = ha + (hb - ha) * (d[seg] - d[i0]) / (d[i1] - d[i0])
        hh = heff[seg] - los
        v = hh * np.sqrt(2.0 * (dd + dr) / (lam * np.maximum(dd, 1e-9) *
                                            np.maximum(dr, 1e-9)))
        j = int(np.argmax(v))
        if v[j] <= -0.78:
            return None
        return i0 + 1 + j, float(v[j])

    def recurse(i0: int, i1: int, ha: float, hb: float, depth: int) -> float:
        if depth > max_edges:
            return 0.0
        found = main_edge(i0, i1, ha, hb)
        if found is None:
            return 0.0
        j, v = found
        loss = knife_edge_db(v)
        if loss <= 0.0:
            return 0.0
        edges.append({
            "index": j, "distance_m": float(d[j]), "height_m": float(h[j]),
            "v": v, "loss_db": loss,
        })
        left = recurse(i0, j, ha, float(heff[j]), depth + 1)
        right = recurse(j, i1, float(heff[j]), hb, depth + 1)
        return loss + left + right

    raw = recurse(0, len(d) - 1, tx_amsl, rx_amsl, 1)
    if not edges:
        return 0.0, []
    main = max(e["loss_db"] for e in edges)
    if len(edges) > 1:
        # Deygout 1991 empirical correction: plain summation over-predicts on
        # long multi-obstacle paths. Never let it fall below the main edge.
        raw = max(main, raw - (10.0 + 0.04 * total / 1000.0))
    edges.sort(key=lambda e: -e["loss_db"])
    return max(0.0, raw), edges[:max_edges]


def spherical_earth_db(distance_m: float, ht_m: float, hr_m: float,
                       freq_mhz: float, k_factor: float = 4.0 / 3.0,
                       polarisation: str = "vertical",
                       eps_r: float = 15.0, sigma: float = 0.005) -> float:
    """ITU-R P.526 §3.1 smooth-sphere diffraction (first-term approximation)."""
    ae = k_factor * EARTH_R
    f = freq_mhz
    dlos = math.sqrt(2 * ae) * (math.sqrt(max(ht_m, 0.1)) + math.sqrt(max(hr_m, 0.1)))
    if distance_m < dlos:
        return 0.0
    # ITU-R P.526 eq. 30/31: the normalised surface admittance
    K = 0.36 * (ae / 1000.0) ** (-1 / 3) * ((eps_r - 1) ** 2 +
                                            (18000 * sigma / f) ** 2) ** (-0.25)
    if polarisation.startswith("v"):
        K *= (eps_r ** 2 + (18000 * sigma / f) ** 2) ** 0.5
    K = max(K, 1e-6)
    beta = (1 + 1.6 * K ** 2 + 0.67 * K ** 4) / (1 + 4.5 * K ** 2 + 1.53 * K ** 4)

    X = 2.188 * beta * f ** (1 / 3) * ae ** (-2 / 3) * (distance_m / 1000.0)
    def Y(h):
        return 9.575e-3 * beta * f ** (2 / 3) * ae ** (-1 / 3) * h

    def FX(x):
        if x >= 1.6:
            return 11.0 + 10.0 * math.log10(x) - 17.6 * x
        return -20.0 * math.log10(x) - 5.6488 * x ** 1.425

    def GY(y):
        if y > 2.0:
            return 17.6 * (y - 1.1) ** 0.5 - 5 * math.log10(y - 1.1) - 8.0
        return 20.0 * math.log10(y + 0.1 * y ** 3)

    yt, yr = Y(max(ht_m, 0.1)), Y(max(hr_m, 0.1))
    val = -(FX(X) + GY(yt) + GY(yr))
    return max(0.0, val)

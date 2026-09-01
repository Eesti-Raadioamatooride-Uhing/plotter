"""Irregular Terrain Model (Longley-Rice) v1.2.2.

A faithful Python port of the NTIA/ITS reference implementation, restructured
so the C `static` variables live on a solver instance instead of module state.
That makes it safe to run many paths concurrently, which the area-coverage
engine relies on.

Two entry points:
  point_to_point(...)  - terrain profile known, the mode used for links
  area(...)            - statistical terrain, used for quick first looks

Reference: G.A. Hufford, A.G. Longley, W.A. Kissick, "A Guide to the Use of
the ITS Irregular Terrain Model in the Area Prediction Mode", NTIA Report
82-100, and the 1985 memorandum to users of ITM.
"""
from __future__ import annotations

import cmath
import math
from dataclasses import dataclass, field

THIRD = 1.0 / 3.0

# Radio climate codes
CLIMATE_EQUATORIAL = 1
CLIMATE_CONTINENTAL_SUBTROPICAL = 2
CLIMATE_MARITIME_SUBTROPICAL = 3
CLIMATE_DESERT = 4
CLIMATE_CONTINENTAL_TEMPERATE = 5
CLIMATE_MARITIME_TEMPERATE_LAND = 6
CLIMATE_MARITIME_TEMPERATE_SEA = 7

POL_HORIZONTAL = 0
POL_VERTICAL = 1

# Ground constants (dielectric, conductivity S/m)
GROUND_TYPES = {
    "average": (15.0, 0.005),
    "poor": (4.0, 0.001),
    "good": (25.0, 0.020),
    "fresh_water": (81.0, 0.010),
    "sea_water": (81.0, 5.000),
    "wet_ground": (30.0, 0.010),
    "forest": (13.0, 0.005),
    "bog": (12.0, 0.030),
}

WARN_TEXT = {
    0: "ok",
    1: "caution: some parameters are nearly out of range",
    2: "note: default parameter substituted",
    3: "warning: a parameter is out of range",
    4: "warning: parameters are far out of range - results unreliable",
}


def _dim(x: float, y: float) -> float:
    """FORTRAN DIM: positive difference."""
    return x - y if x - y > 0 else 0.0


def qerfi(q: float) -> float:
    """Inverse of the standard normal complementary cumulative distribution."""
    c0, c1, c2 = 2.515516698, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    x = 0.5 - q
    t = max(0.5 - abs(x), 1e-6)
    t = math.sqrt(-2.0 * math.log(t))
    v = t - ((c2 * t + c1) * t + c0) / (((d3 * t + d2) * t + d1) * t + 1.0)
    return -v if x < 0.0 else v


def aknfe(v2: float) -> float:
    """Attenuation from a single knife edge, v2 = square of Fresnel parameter."""
    if v2 < 5.76:
        return 6.02 + 9.11 * math.sqrt(v2) - 1.27 * v2
    return 12.953 + 4.343 * math.log(v2)


def fht(x: float, pk: float) -> float:
    """Height-gain over a smooth spherical earth."""
    if x < 200.0:
        w = -math.log(pk)
        if pk < 1e-5 or x * w * w * w > 5495.0:
            v = -117.0
            if x > 1.0:
                v = 17.372 * math.log(x) + v
        else:
            v = 2.5e-5 * x * x / pk - 8.686 * w - 15.0
    else:
        v = 0.05751 * x - 4.343 * math.log(x)
        if x < 2000.0:
            w = 0.0134 * x * math.exp(-0.005 * x)
            v = (1.0 - w) * v + w * (17.372 * math.log(x) - 117.0)
    return v


_H0F_A = (25.0, 80.0, 177.0, 395.0, 705.0)
_H0F_B = (24.0, 45.0, 68.0, 80.0, 105.0)


def h0f(r: float, et: float) -> float:
    """Scatter efficiency factor."""
    it = int(et)
    if it <= 0:
        it, q = 1, 0.0
    elif it >= 5:
        it, q = 5, 0.0
    else:
        q = et - it
    x = (1.0 / r) ** 2
    v = 4.343 * math.log((_H0F_A[it - 1] * x + _H0F_B[it - 1]) * x + 1.0)
    if q != 0.0:
        v = (1.0 - q) * v + q * 4.343 * math.log(
            (_H0F_A[it] * x + _H0F_B[it]) * x + 1.0)
    return v


def ahd(td: float) -> float:
    """Scatter attenuation with distance."""
    a = (133.4, 104.6, 71.8)
    b = (0.332e-3, 0.212e-3, 0.157e-3)
    c = (-4.343, -1.086, 2.171)
    i = 0 if td <= 10e3 else (1 if td <= 70e3 else 2)
    return a[i] + b[i] * td + c[i] * math.log(td)


def _curve(c1: float, c2: float, x1: float, x2: float, x3: float, de: float) -> float:
    return ((c1 + c2 / (1.0 + ((de - x2) / x3) ** 2)) *
            (de / x1) ** 2 / (1.0 + (de / x1) ** 2))


def _qtile_simple(a, ir: int) -> float:
    """Equivalent, robust: ir-th largest, 0-based."""
    s = sorted(a, reverse=True)
    return s[min(max(0, ir), len(s) - 1)]


def _z1sq1(z: list[float], x1: float, x2: float) -> tuple[float, float]:
    """Least-squares fit of a straight line to profile z over [x1, x2].

    z[0] = number of intervals, z[1] = spacing, z[2:] = heights.
    Returns the fitted height at the start and the end of the whole profile.
    """
    xn = z[0]
    xa = float(int(_dim(x1 / z[1], 0.0)))
    xb = xn - float(int(_dim(xn, x2 / z[1])))
    if xb <= xa:
        xa = _dim(xa, 1.0)
        xb = xn - _dim(xn, xb + 1.0)
    ja = int(xa)
    jb = int(xb)
    n = jb - ja
    xa = xb - xa
    x = -0.5 * xa
    xb += x
    a = 0.5 * (z[ja + 2] + z[jb + 2])
    b = 0.5 * (z[ja + 2] - z[jb + 2]) * x
    for _ in range(2, n + 1):
        ja += 1
        x += 1.0
        a += z[ja + 2]
        b += z[ja + 2] * x
    a /= xa
    b = b * 12.0 / ((xa * xa + 2.0) * xa)
    return a - b * xb, a + b * (xn - xb)


def _d1thx(pfl: list[float], x1: float, x2: float) -> float:
    """Terrain irregularity parameter dh over the interval [x1, x2]."""
    np_ = int(pfl[0])
    xa = x1 / pfl[1]
    xb = x2 / pfl[1]
    if xb - xa < 2.0:
        return 0.0
    ka = int(0.1 * (xb - xa + 8.0))
    ka = min(max(4, ka), 25)
    n = 10 * ka - 5
    kb = n - ka + 1
    sn = n - 1
    s = [0.0] * (n + 2)
    s[0] = float(sn)
    s[1] = 1.0
    xb = (xb - xa) / sn
    k = int(xa + 1.0)
    xa -= float(k)
    for j in range(n):
        while xa > 0.0 and k < np_:
            xa -= 1.0
            k += 1
        s[j + 2] = pfl[k + 2] + (pfl[k + 2] - pfl[k + 1]) * xa
        xa = xa + xb
    za, zb = _z1sq1(s, 0.0, float(sn))
    xb = (zb - za) / sn
    xa = za
    for j in range(n):
        s[j + 2] -= xa
        xa = xa + xb
    body = s[2:2 + n]
    v = _qtile_simple(body, ka - 1) - _qtile_simple(body, kb - 1)
    return v / (1.0 - 0.8 * math.exp(-(x2 - x1) / 50.0e3))


@dataclass
class Prop:
    aref: float = 0.0
    dist: float = 0.0
    hg: list[float] = field(default_factory=lambda: [0.0, 0.0])
    wn: float = 0.0
    dh: float = 0.0
    ens: float = 0.0
    gme: float = 0.0
    zgnd: complex = 0j
    he: list[float] = field(default_factory=lambda: [0.0, 0.0])
    dl: list[float] = field(default_factory=lambda: [0.0, 0.0])
    the: list[float] = field(default_factory=lambda: [0.0, 0.0])
    kwx: int = 0
    mdp: int = 0


@dataclass
class Propa:
    dlsa: float = 0.0
    dx: float = 0.0
    ael: float = 0.0
    ak1: float = 0.0
    ak2: float = 0.0
    aed: float = 0.0
    emd: float = 0.0
    aes: float = 0.0
    ems: float = 0.0
    dls: list[float] = field(default_factory=lambda: [0.0, 0.0])
    dla: float = 0.0
    tha: float = 0.0


@dataclass
class Propv:
    sgc: float = 0.0
    lvar: int = 0
    mdvar: int = 0
    klim: int = 5


class ITM:
    """One ITM solver. Not thread-safe; make one per path."""

    def __init__(self) -> None:
        self.prop = Prop()
        self.propa = Propa()
        self.propv = Propv()
        # adiff statics
        self._wd1 = self._xd1 = self._afo = self._qk = self._aht = self._xht = 0.0
        # ascat statics
        self._ad = self._rr = self._etq = self._h0s = 0.0
        # alos statics
        self._wls = 0.0
        # lrprop statics
        self._wlos = False
        self._wscat = False
        self._dmin = self._xae = 0.0
        # avar statics
        self._kdv = 0
        self._ws = self._w1 = False
        self._av = {}

    # ---------------------------------------------------------------- adiff
    def adiff(self, d: float) -> float:
        p, pa = self.prop, self.propa
        D = 50e3
        HQ = 20.0
        if d == 0:
            q = p.hg[0] * p.hg[1]
            self._qk = p.he[0] * p.he[1] - q
            if p.mdp < 0.0:
                q += HQ
            self._wd1 = math.sqrt(1.0 + self._qk / q)
            self._xd1 = pa.dla + pa.tha / p.gme
            q = (1.0 - 0.8 * math.exp(-pa.dlsa / D)) * p.dh
            q *= 0.78 * math.exp(-((q / 16.0) ** 0.25))
            self._afo = min(15.0, 2.171 * math.log(
                1.0 + 4.77e-4 * p.hg[0] * p.hg[1] * p.wn * q))
            self._qk = 1.0 / abs(p.zgnd)
            self._aht = 20.0
            self._xht = 0.0
            for j in range(2):
                a = 0.5 * (p.dl[j] ** 2) / p.he[j]
                wa = (a * p.wn) ** THIRD
                pk = self._qk / wa
                q = (1.607 - pk) * 151.0 * wa * p.dl[j] / a
                self._xht += q
                self._aht += fht(q, pk)
            return 0.0

        th = pa.tha + d * p.gme
        ds = d - pa.dla
        q = 0.0795775 * p.wn * ds * th * th
        adiffv = (aknfe(q * p.dl[0] / (ds + p.dl[0])) +
                  aknfe(q * p.dl[1] / (ds + p.dl[1])))
        a = ds / th
        wa = (a * p.wn) ** THIRD
        pk = self._qk / wa
        q = (1.607 - pk) * 151.0 * wa * th + self._xht
        ar = 0.05751 * q - 4.343 * math.log(q) - self._aht
        q = (self._wd1 + self._xd1 / d) * min(
            (1.0 - 0.8 * math.exp(-d / D)) * p.dh * p.wn, 6283.2)
        wd = 25.1 / (25.1 + math.sqrt(q))
        return ar * wd + (1.0 - wd) * adiffv + self._afo

    # ---------------------------------------------------------------- ascat
    def ascat(self, d: float) -> float:
        p, pa = self.prop, self.propa
        if d == 0.0:
            self._ad = p.dl[0] - p.dl[1]
            self._rr = p.he[1] / p.he[0]
            if self._ad < 0.0:
                self._ad = -self._ad
                self._rr = 1.0 / self._rr
            self._etq = (5.67e-6 * p.ens - 2.32e-3) * p.ens + 0.031
            self._h0s = -15.0
            return 0.0

        if self._h0s > 15.0:
            h0 = self._h0s
        else:
            th = p.the[0] + p.the[1] + d * p.gme
            r2 = 2.0 * p.wn * th
            r1 = r2 * p.he[0]
            r2 *= p.he[1]
            if r1 < 0.2 and r2 < 0.2:
                return 1001.0
            ss = (d - self._ad) / (d + self._ad)
            q = self._rr / ss
            ss = max(0.1, ss)
            q = min(max(0.1, q), 10.0)
            z0 = (d - self._ad) * (d + self._ad) * th * 0.25 / d
            et = (self._etq * math.exp(-(min(1.7, z0 / 8.0e3) ** 6.0)) + 1.0) * z0 / 1.7556e3
            ett = max(et, 1.0)
            h0 = (h0f(r1, ett) + h0f(r2, ett)) * 0.5
            h0 += min(h0, (1.38 - math.log(ett)) * math.log(ss) * math.log(q) * 0.49)
            h0 = _dim(h0, 0.0)
            if et < 1.0:
                h0 = et * h0 + (1.0 - et) * 4.343 * math.log(
                    (((1.0 + 1.4142 / r1) * (1.0 + 1.4142 / r2)) ** 2) *
                    (r1 + r2) / (r1 + r2 + 2.8284))
            if h0 > 15.0 and self._h0s >= 0.0:
                h0 = self._h0s
        self._h0s = h0
        th = pa.tha + d * p.gme
        return (ahd(th * d) + 4.343 * math.log(47.7 * p.wn * (th ** 4)) -
                0.1 * (p.ens - 301.0) * math.exp(-th * d / 40e3) + h0)

    # ----------------------------------------------------------------- alos
    def alos(self, d: float) -> float:
        p, pa = self.prop, self.propa
        if d == 0.0:
            self._wls = 0.021 / (0.021 + p.wn * p.dh / max(10e3, pa.dlsa))
            return 0.0
        q = (1.0 - 0.8 * math.exp(-d / 50e3)) * p.dh
        s = 0.78 * q * math.exp(-((q / 16.0) ** 0.25))
        q = p.he[0] + p.he[1]
        sps = q / math.sqrt(d * d + q * q)
        r = ((sps - p.zgnd) / (sps + p.zgnd) *
             cmath.exp(-min(10.0, p.wn * s * sps)))
        q = abs(r) ** 2
        if q < 0.25 or q < sps:
            r = r * math.sqrt(sps / q)
        alosv = pa.emd * d + pa.aed
        q = p.wn * p.he[0] * p.he[1] * 2.0 / d
        if q > 1.57:
            q = 3.14 - 2.4649 / q
        return (-4.343 * math.log(abs(complex(math.cos(q), -math.sin(q)) + r))
                - alosv) * self._wls + alosv

    # --------------------------------------------------------------- lrprop
    def lrprop(self, d: float) -> None:
        p, pa = self.prop, self.propa
        if p.mdp != 0:
            for j in range(2):
                pa.dls[j] = math.sqrt(2.0 * p.he[j] / p.gme)
            pa.dlsa = pa.dls[0] + pa.dls[1]
            pa.dla = p.dl[0] + p.dl[1]
            pa.tha = max(p.the[0] + p.the[1], -pa.dla * p.gme)
            self._wlos = False
            self._wscat = False
            if p.wn < 0.838 or p.wn > 210.0:
                p.kwx = max(p.kwx, 1)
            for j in range(2):
                if p.hg[j] < 1.0 or p.hg[j] > 1000.0:
                    p.kwx = max(p.kwx, 1)
            for j in range(2):
                if (abs(p.the[j]) > 200e-3 or p.dl[j] < 0.1 * pa.dls[j]
                        or p.dl[j] > 3.0 * pa.dls[j]):
                    p.kwx = max(p.kwx, 3)
            if (p.ens < 250.0 or p.ens > 400.0 or p.gme < 75e-9 or p.gme > 250e-9
                    or p.zgnd.real <= abs(p.zgnd.imag)
                    or p.wn < 0.419 or p.wn > 420.0):
                p.kwx = 4
            for j in range(2):
                if p.hg[j] < 0.5 or p.hg[j] > 3000.0:
                    p.kwx = 4
            self._dmin = abs(p.he[0] - p.he[1]) / 200e-3
            self.adiff(0.0)
            self._xae = (p.wn * p.gme ** 2) ** (-THIRD)
            d3 = max(pa.dlsa, 1.3787 * self._xae + pa.dla)
            d4 = d3 + 2.7574 * self._xae
            a3 = self.adiff(d3)
            a4 = self.adiff(d4)
            pa.emd = (a4 - a3) / (d4 - d3)
            pa.aed = a3 - pa.emd * d3

        if p.mdp >= 0:
            p.mdp = 0
            p.dist = d
        if p.dist > 0.0:
            if p.dist > 1000e3:
                p.kwx = max(p.kwx, 1)
            if p.dist < self._dmin:
                p.kwx = max(p.kwx, 3)
            if p.dist < 1e3 or p.dist > 2000e3:
                p.kwx = 4

        if p.dist < pa.dlsa:
            if not self._wlos:
                self.alos(0.0)
                d2 = pa.dlsa
                a2 = pa.aed + d2 * pa.emd
                d0 = 1.908 * p.wn * p.he[0] * p.he[1]
                if pa.aed >= 0.0:
                    d0 = min(d0, 0.5 * pa.dla)
                    d1 = d0 + 0.25 * (pa.dla - d0)
                else:
                    d1 = max(-pa.aed / pa.emd, 0.25 * pa.dla)
                a1 = self.alos(d1)
                wq = False
                if d0 < d1:
                    a0 = self.alos(d0)
                    q = math.log(d2 / d0)
                    pa.ak2 = max(0.0, ((d2 - d0) * (a1 - a0) - (d1 - d0) * (a2 - a0)) /
                                 ((d2 - d0) * math.log(d1 / d0) - (d1 - d0) * q))
                    wq = pa.aed >= 0.0 or pa.ak2 > 0.0
                    if wq:
                        pa.ak1 = (a2 - a0 - pa.ak2 * q) / (d2 - d0)
                        if pa.ak1 < 0.0:
                            pa.ak1 = 0.0
                            pa.ak2 = _dim(a2, a0) / q
                            if pa.ak2 == 0.0:
                                pa.ak1 = pa.emd
                if not wq:
                    pa.ak1 = _dim(a2, a1) / (d2 - d1)
                    pa.ak2 = 0.0
                    if pa.ak1 == 0.0:
                        pa.ak1 = pa.emd
                pa.ael = a2 - pa.ak1 * d2 - pa.ak2 * math.log(d2)
                self._wlos = True
            if p.dist > 0.0:
                p.aref = pa.ael + pa.ak1 * p.dist + pa.ak2 * math.log(p.dist)

        if p.dist <= 0.0 or p.dist >= pa.dlsa:
            if not self._wscat:
                self.ascat(0.0)
                d5 = pa.dla + 200e3
                d6 = d5 + 200e3
                a6 = self.ascat(d6)
                a5 = self.ascat(d5)
                if a5 < 1000.0:
                    pa.ems = (a6 - a5) / 200e3
                    pa.dx = max(pa.dlsa,
                                max(pa.dla + 0.3 * self._xae * math.log(47.7 * p.wn),
                                    (a5 - pa.aed - pa.ems * d5) / (pa.emd - pa.ems)))
                    pa.aes = (pa.emd - pa.ems) * pa.dx + pa.aed
                else:
                    pa.ems = pa.emd
                    pa.aes = pa.aed
                    pa.dx = 10.0e6
                self._wscat = True
            if p.dist > pa.dx:
                p.aref = pa.aes + pa.ems * p.dist
            else:
                p.aref = pa.aed + pa.emd * p.dist

        p.aref = max(p.aref, 0.0)

    # ----------------------------------------------------------------- avar
    _BV1 = (-9.67, -0.62, 1.26, -9.21, -0.62, -0.39, 3.15)
    _BV2 = (12.7, 9.19, 15.5, 9.05, 9.19, 2.86, 857.9)
    _XV1 = (144.9e3, 228.9e3, 262.6e3, 84.1e3, 228.9e3, 141.7e3, 2222e3)
    _XV2 = (190.3e3, 205.2e3, 185.2e3, 101.1e3, 205.2e3, 315.9e3, 164.8e3)
    _XV3 = (133.8e3, 143.6e3, 99.8e3, 98.6e3, 143.6e3, 167.4e3, 116.3e3)
    _BSM1 = (2.13, 2.66, 6.11, 1.98, 2.68, 6.86, 8.51)
    _BSM2 = (159.5, 7.67, 6.65, 13.11, 7.16, 10.38, 169.8)
    _XSM1 = (762.2e3, 100.4e3, 138.2e3, 139.1e3, 93.7e3, 187.8e3, 609.8e3)
    _XSM2 = (123.6e3, 172.5e3, 242.2e3, 132.7e3, 186.8e3, 169.6e3, 119.9e3)
    _XSM3 = (94.5e3, 136.4e3, 178.6e3, 193.5e3, 133.5e3, 108.9e3, 106.6e3)
    _BSP1 = (2.11, 6.87, 10.08, 3.68, 4.75, 8.58, 8.43)
    _BSP2 = (102.3, 15.53, 9.60, 159.3, 8.12, 13.97, 8.19)
    _XSP1 = (636.9e3, 138.7e3, 165.3e3, 464.4e3, 93.2e3, 216.0e3, 136.2e3)
    _XSP2 = (134.8e3, 143.7e3, 225.7e3, 93.1e3, 135.9e3, 152.0e3, 188.5e3)
    _XSP3 = (95.6e3, 98.6e3, 129.7e3, 94.2e3, 113.4e3, 122.7e3, 122.9e3)
    _BSD1 = (1.224, 0.801, 1.380, 1.000, 1.224, 1.518, 3.615)
    _BZD1 = (1.282, 2.161, 1.282, 20.0, 1.282, 1.282, 1.282)
    _BFM1 = (1.0, 1.0, 1.0, 1.0, 0.92, 1.0, 1.0)
    _BFM2 = (0.0, 0.0, 0.0, 0.0, 0.25, 0.0, 0.0)
    _BFM3 = (0.0, 0.0, 0.0, 0.0, 1.77, 0.0, 0.0)
    _BFP1 = (1.0, 0.93, 1.0, 0.93, 0.93, 1.0, 1.0)
    _BFP2 = (0.0, 0.31, 0.0, 0.19, 0.31, 0.0, 0.0)
    _BFP3 = (0.0, 2.00, 0.0, 1.79, 2.00, 0.0, 0.0)

    def avar(self, zzt: float, zzl: float, zzc: float) -> float:
        p, pv = self.prop, self.propv
        s = self._av
        rt, rl = 7.8, 24.0

        if pv.lvar > 0:
            lv = pv.lvar
            if lv >= 5:
                if pv.klim <= 0 or pv.klim > 7:
                    pv.klim = 5
                    p.kwx = max(p.kwx, 2)
                k = pv.klim - 1
                s["cv1"], s["cv2"] = self._BV1[k], self._BV2[k]
                s["yv1"], s["yv2"], s["yv3"] = self._XV1[k], self._XV2[k], self._XV3[k]
                s["csm1"], s["csm2"] = self._BSM1[k], self._BSM2[k]
                s["ysm1"], s["ysm2"], s["ysm3"] = self._XSM1[k], self._XSM2[k], self._XSM3[k]
                s["csp1"], s["csp2"] = self._BSP1[k], self._BSP2[k]
                s["ysp1"], s["ysp2"], s["ysp3"] = self._XSP1[k], self._XSP2[k], self._XSP3[k]
                s["csd1"], s["zd"] = self._BSD1[k], self._BZD1[k]
                s["cfm1"], s["cfm2"], s["cfm3"] = self._BFM1[k], self._BFM2[k], self._BFM3[k]
                s["cfp1"], s["cfp2"], s["cfp3"] = self._BFP1[k], self._BFP2[k], self._BFP3[k]
            if lv >= 4:
                self._kdv = pv.mdvar
                self._ws = self._kdv >= 20
                if self._ws:
                    self._kdv -= 20
                self._w1 = self._kdv >= 10
                if self._w1:
                    self._kdv -= 10
                if self._kdv < 0 or self._kdv > 3:
                    self._kdv = 0
                    p.kwx = max(p.kwx, 2)
            if lv >= 3:
                q = math.log(0.133 * p.wn)
                s["gm"] = s["cfm1"] + s["cfm2"] / ((s["cfm3"] * q) ** 2 + 1.0)
                s["gp"] = s["cfp1"] + s["cfp2"] / ((s["cfp3"] * q) ** 2 + 1.0)
            if lv >= 2:
                s["dexa"] = (math.sqrt(18e6 * p.he[0]) + math.sqrt(18e6 * p.he[1]) +
                             (575.7e12 / p.wn) ** THIRD)
            if p.dist < s["dexa"]:
                s["de"] = 130e3 * p.dist / s["dexa"]
            else:
                s["de"] = 130e3 + p.dist - s["dexa"]

            de = s["de"]
            s["vmd"] = _curve(s["cv1"], s["cv2"], s["yv1"], s["yv2"], s["yv3"], de)
            s["sgtm"] = _curve(s["csm1"], s["csm2"], s["ysm1"], s["ysm2"], s["ysm3"], de) * s["gm"]
            s["sgtp"] = _curve(s["csp1"], s["csp2"], s["ysp1"], s["ysp2"], s["ysp3"], de) * s["gp"]
            s["sgtd"] = s["sgtp"] * s["csd1"]
            s["tgtd"] = (s["sgtp"] - s["sgtd"]) * s["zd"]
            if self._w1:
                s["sgl"] = 0.0
            else:
                q = (1.0 - 0.8 * math.exp(-p.dist / 50e3)) * p.dh * p.wn
                s["sgl"] = 10.0 * q / (q + 13.0)
            s["vs0"] = 0.0 if self._ws else (5.0 + 3.0 * math.exp(-de / 100e3)) ** 2
            pv.lvar = 0

        zt, zl, zc = zzt, zzl, zzc
        if self._kdv == 0:
            zt = zc
            zl = zc
        elif self._kdv == 1:
            zl = zc
        elif self._kdv == 2:
            zl = zt
        if abs(zt) > 3.1 or abs(zl) > 3.1 or abs(zc) > 3.1:
            p.kwx = max(p.kwx, 1)

        if zt < 0.0:
            sgt = s["sgtm"]
        elif zt <= s["zd"]:
            sgt = s["sgtp"]
        else:
            sgt = s["sgtd"] + s["tgtd"] / zt

        vs = (s["vs0"] + (sgt * zt) ** 2 / (rt + zc * zc) +
              (s["sgl"] * zl) ** 2 / (rl + zc * zc))
        if self._kdv == 0:
            yr = 0.0
            pv.sgc = math.sqrt(sgt * sgt + s["sgl"] ** 2 + vs)
        elif self._kdv == 1:
            yr = sgt * zt
            pv.sgc = math.sqrt(s["sgl"] ** 2 + vs)
        elif self._kdv == 2:
            yr = math.sqrt(sgt * sgt + s["sgl"] ** 2) * zt
            pv.sgc = math.sqrt(vs)
        else:
            yr = sgt * zt + s["sgl"] * zl
            pv.sgc = math.sqrt(vs)

        avarv = p.aref - s["vmd"] - yr - pv.sgc * zc
        if avarv < 0.0:
            avarv = avarv * (29.0 - avarv) / (29.0 - 10.0 * avarv)
        return avarv

    # ----------------------------------------------------------------- qlrps
    def qlrps(self, fmhz: float, zsys: float, en0: float, ipol: int,
              eps: float, sgm: float) -> None:
        gma = 157e-9
        p = self.prop
        p.wn = fmhz / 47.7
        p.ens = en0
        if zsys != 0.0:
            p.ens *= math.exp(-zsys / 9460.0)
        p.gme = gma * (1.0 - 0.04665 * math.exp(p.ens / 179.3))
        zq = complex(eps, 376.62 * sgm / p.wn)
        zgnd = cmath.sqrt(zq - 1.0)
        if ipol != 0:
            zgnd = zgnd / zq
        p.zgnd = zgnd

    # ------------------------------------------------------------------ hzns
    def hzns(self, pfl: list[float]) -> None:
        p = self.prop
        np_ = int(pfl[0])
        xi = pfl[1]
        za = pfl[2] + p.hg[0]
        zb = pfl[np_ + 2] + p.hg[1]
        qc = 0.5 * p.gme
        q = qc * p.dist
        p.the[1] = (zb - za) / p.dist
        p.the[0] = p.the[1] - q
        p.the[1] = -p.the[1] - q
        p.dl[0] = p.dist
        p.dl[1] = p.dist
        if np_ >= 2:
            sa = 0.0
            sb = p.dist
            wq = True
            for i in range(1, np_):
                sa += xi
                sb -= xi
                q = pfl[i + 2] - (qc * sa + p.the[0]) * sa - za
                if q > 0.0:
                    p.the[0] += q / sa
                    p.dl[0] = sa
                    wq = False
                if not wq:
                    q = pfl[i + 2] - (qc * sb + p.the[1]) * sb - zb
                    if q > 0.0:
                        p.the[1] += q / sb
                        p.dl[1] = sb

    # ---------------------------------------------------------------- qlrpfl
    def qlrpfl(self, pfl: list[float], klimx: int, mdvarx: int) -> None:
        p, pv = self.prop, self.propv
        p.dist = pfl[0] * pfl[1]
        np_ = int(pfl[0])
        self.hzns(pfl)
        xl = [min(15.0 * p.hg[j], 0.1 * p.dl[j]) for j in range(2)]
        xl[1] = p.dist - xl[1]
        p.dh = _d1thx(pfl, xl[0], xl[1])
        if p.dl[0] + p.dl[1] > 1.5 * p.dist:
            za, zb = _z1sq1(pfl, xl[0], xl[1])
            p.he[0] = p.hg[0] + _dim(pfl[2], za)
            p.he[1] = p.hg[1] + _dim(pfl[np_ + 2], zb)
            for j in range(2):
                p.dl[j] = (math.sqrt(2.0 * p.he[j] / p.gme) *
                           math.exp(-0.07 * math.sqrt(p.dh / max(p.he[j], 5.0))))
            q = p.dl[0] + p.dl[1]
            if q <= p.dist:
                q = (p.dist / q) ** 2
                for j in range(2):
                    p.he[j] *= q
                    p.dl[j] = (math.sqrt(2.0 * p.he[j] / p.gme) *
                               math.exp(-0.07 * math.sqrt(p.dh / max(p.he[j], 5.0))))
            for j in range(2):
                q = math.sqrt(2.0 * p.he[j] / p.gme)
                p.the[j] = (0.65 * p.dh * (q / p.dl[j] - 1.0) - 2.0 * p.he[j]) / q
        else:
            za, _ = _z1sq1(pfl, xl[0], 0.9 * p.dl[0])
            _, zb = _z1sq1(pfl, p.dist - 0.9 * p.dl[1], xl[1])
            p.he[0] = p.hg[0] + _dim(pfl[2], za)
            p.he[1] = p.hg[1] + _dim(pfl[np_ + 2], zb)
        p.mdp = -1
        pv.lvar = max(pv.lvar, 3)
        if mdvarx >= 0:
            pv.mdvar = mdvarx
            pv.lvar = max(pv.lvar, 4)
        if klimx > 0:
            pv.klim = klimx
            pv.lvar = 5
        self.lrprop(0.0)

    # ----------------------------------------------------------------- qlra
    def qlra(self, kst: list[int], klimx: int, mdvarx: int) -> None:
        p, pv = self.prop, self.propv
        for j in range(2):
            if kst[j] <= 0:
                p.he[j] = p.hg[j]
            else:
                q = 4.0
                if kst[j] != 1:
                    q = 9.0
                if p.hg[j] < 5.0:
                    q *= math.sin(0.3141593 * p.hg[j])
                p.he[j] = p.hg[j] + (1.0 + q) * math.exp(
                    -min(20.0, 2.0 * p.hg[j] / max(1e-3, p.dh)))
            q = math.sqrt(2.0 * p.he[j] / p.gme)
            p.dl[j] = q * math.exp(-0.07 * math.sqrt(p.dh / max(p.he[j], 5.0)))
            p.the[j] = (0.65 * p.dh * (q / p.dl[j] - 1.0) - 2.0 * p.he[j]) / q
        p.mdp = 1
        pv.lvar = max(pv.lvar, 3)
        if mdvarx >= 0:
            pv.mdvar = mdvarx
            pv.lvar = max(pv.lvar, 4)
        if klimx > 0:
            pv.klim = klimx
            pv.lvar = 5


@dataclass
class ITMResult:
    loss_db: float          # total basic transmission loss
    free_space_db: float
    mode: str               # "Line-Of-Sight" / "Single Horizon, Diffraction" / ...
    warning: int
    warning_text: str
    dist_m: float
    dh_m: float             # terrain irregularity used
    he_m: tuple[float, float]   # effective antenna heights
    dl_m: tuple[float, float]   # horizon distances
    the_rad: tuple[float, float]  # horizon elevation angles


def point_to_point(profile: list[float], tx_height_m: float, rx_height_m: float,
                   frequency_mhz: float,
                   *, eps_dielect: float = 15.0, sgm_conductivity: float = 0.005,
                   eno_ns_surfref: float = 301.0,
                   radio_climate: int = CLIMATE_CONTINENTAL_TEMPERATE,
                   polarization: int = POL_VERTICAL,
                   conf: float = 0.5, rel: float = 0.5) -> ITMResult:
    """ITM point-to-point mode.

    `profile` is the ITM elevation array: profile[0] = number of intervals
    (points - 1), profile[1] = point spacing in metres, profile[2:] = terrain
    heights above sea level in metres.
    """
    m = ITM()
    p, pv = m.prop, m.propv
    p.hg[0] = tx_height_m
    p.hg[1] = rx_height_m
    pv.klim = radio_climate
    p.kwx = 0
    pv.lvar = 5
    p.mdp = -1
    zc = qerfi(conf)
    zr = qerfi(rel)
    np_ = int(profile[0])

    # mean terrain height of the middle of the path, for refractivity scaling
    ja = int(3.0 + 0.1 * profile[0])
    jb = np_ - ja + 6
    seg = profile[ja - 1:jb]
    zsys = sum(seg) / len(seg) if seg else 0.0

    pv.mdvar = 12
    m.qlrps(frequency_mhz, zsys, eno_ns_surfref, polarization,
            eps_dielect, sgm_conductivity)
    m.qlrpfl(profile, pv.klim, pv.mdvar)

    fs = 32.45 + 20.0 * math.log10(frequency_mhz) + 20.0 * math.log10(p.dist / 1000.0)
    q = p.dist - m.propa.dla
    if int(q) < 0:
        mode = "Line-Of-Sight"
    else:
        if int(q) == 0:
            mode = "Single Horizon"
        else:
            mode = "Double Horizon"
        if p.dist <= m.propa.dlsa or p.dist <= m.propa.dx:
            mode += ", Diffraction Dominant"
        elif p.dist > m.propa.dx:
            mode += ", Troposcatter Dominant"

    loss = m.avar(zr, 0.0, zc) + fs
    return ITMResult(
        loss_db=loss, free_space_db=fs, mode=mode,
        warning=p.kwx, warning_text=WARN_TEXT.get(p.kwx, ""),
        dist_m=p.dist, dh_m=p.dh,
        he_m=(p.he[0], p.he[1]), dl_m=(p.dl[0], p.dl[1]),
        the_rad=(p.the[0], p.the[1]),
    )


def area(distance_m: float, tx_height_m: float, rx_height_m: float,
         frequency_mhz: float, dh_m: float,
         *, tx_site_criteria: int = 1, rx_site_criteria: int = 1,
         eps_dielect: float = 15.0, sgm_conductivity: float = 0.005,
         eno_ns_surfref: float = 301.0,
         radio_climate: int = CLIMATE_CONTINENTAL_TEMPERATE,
         polarization: int = POL_VERTICAL,
         mdvar: int = 12, conf: float = 0.5, rel: float = 0.5) -> ITMResult:
    """ITM area mode - statistical terrain described only by dh.

    Site criteria: 0 = random, 1 = careful, 2 = very careful.
    """
    m = ITM()
    p, pv = m.prop, m.propv
    p.dh = dh_m
    p.hg[0] = tx_height_m
    p.hg[1] = rx_height_m
    pv.klim = radio_climate
    pv.mdvar = mdvar
    p.kwx = 0
    pv.lvar = 5
    p.mdp = 1
    zc = qerfi(conf)
    zr = qerfi(rel)
    m.qlrps(frequency_mhz, 0.0, eno_ns_surfref, polarization,
            eps_dielect, sgm_conductivity)
    m.qlra([tx_site_criteria, rx_site_criteria], pv.klim, pv.mdvar)
    if pv.lvar < 1:
        pv.lvar = 1
    m.lrprop(distance_m)
    fs = 32.45 + 20.0 * math.log10(frequency_mhz) + 20.0 * math.log10(distance_m / 1000.0)
    loss = m.avar(zr, 0.0, zc) + fs
    return ITMResult(
        loss_db=loss, free_space_db=fs, mode="Area",
        warning=p.kwx, warning_text=WARN_TEXT.get(p.kwx, ""),
        dist_m=distance_m, dh_m=p.dh,
        he_m=(p.he[0], p.he[1]), dl_m=(p.dl[0], p.dl[1]),
        the_rad=(p.the[0], p.the[1]),
    )

"""Antenna models, from an 80 m dipole in the trees to a 24 GHz dish.

Everything exposes the same two things:

    gain_at(elevation_deg, azimuth_deg) -> dBi
    peak_gain_dbi

Azimuth is relative to the antenna's boresight, elevation is above the local
horizon. For HF the elevation response is the whole story - it decides which
hop distances the station can actually work - so the wire antennas model the
ground reflection properly rather than assuming free space.
"""
from __future__ import annotations

import cmath
import math
from dataclasses import dataclass, field

import numpy as np

C = 299_792_458.0


def ground_reflection(theta_rad: float, freq_mhz: float, eps_r: float,
                      sigma: float, horizontal: bool) -> complex:
    """Fresnel reflection coefficient of the ground at elevation theta."""
    lam = C / (freq_mhz * 1e6)
    eps_c = complex(eps_r, -60.0 * lam * sigma)
    s = math.sin(theta_rad)
    c2 = math.cos(theta_rad) ** 2
    root = cmath.sqrt(eps_c - c2)
    if horizontal:
        return (s - root) / (s + root)
    return (eps_c * s - root) / (eps_c * s + root)


class Antenna:
    kind = "isotropic"
    peak_gain_dbi = 0.0
    name = "isotropic"

    def gain_at(self, elevation_deg: float, azimuth_deg: float = 0.0) -> float:
        return 0.0

    def elevation_cut(self, azimuth_deg: float = 0.0, step: float = 1.0):
        angles = np.arange(0.0, 90.0 + step, step)
        return angles, np.array([self.gain_at(float(a), azimuth_deg) for a in angles])

    def azimuth_cut(self, elevation_deg: float = 0.0, step: float = 2.0):
        angles = np.arange(0.0, 360.0, step)
        return angles, np.array([self.gain_at(elevation_deg, float(a)) for a in angles])

    def describe(self) -> dict:
        return {"kind": self.kind, "name": self.name,
                "peak_gain_dbi": round(self.peak_gain_dbi, 2)}


# --------------------------------------------------------------------- wires

@dataclass
class WireAntenna(Antenna):
    """A resonant wire above real ground: dipole, inverted-V, or vertical.

    `height_m` is the height of the feedpoint (dipole/vertical) or the apex
    (inverted-V). `orientation_deg` is the compass bearing the wire runs
    along, which sets the azimuth nulls off the ends.
    """
    freq_mhz: float = 3.65
    height_m: float = 10.0
    kind: str = "dipole"           # dipole | inverted_v | vertical | efhw | loop
    orientation_deg: float = 90.0
    eps_r: float = 15.0
    sigma: float = 0.005
    droop_deg: float = 45.0        # inverted-V leg droop
    radials: int = 4               # for verticals: affects ground loss
    name: str = "wire"
    _norm: float = field(default=0.0, repr=False)
    _peak: float = field(default=0.0, repr=False)

    def __post_init__(self):
        self.horizontal = self.kind in ("dipole", "inverted_v", "loop", "efhw")
        self._calibrate()
        if self.name == "wire":
            self.name = f"{self.kind} @ {self.height_m:g} m"

    # ------------------------------------------------------------ internals
    @property
    def wavelength_m(self) -> float:
        return C / (self.freq_mhz * 1e6)

    def _element_factor(self, theta: float, phi_from_wire: float) -> float:
        """Free-space field pattern of the element, normalised to 1 at max."""
        if self.kind == "vertical":
            # quarter-wave monopole: cos(pi/2 sin th)/cos th, zero at zenith
            s = math.sin(theta)
            c = math.cos(theta)
            if c < 1e-6:
                return 0.0
            return abs(math.cos(math.pi / 2 * s) / c)
        if self.kind == "loop":
            return 1.0  # small horizontal loop: near-isotropic in azimuth
        # half-wave dipole along a horizontal axis
        cos_psi = math.cos(theta) * math.cos(phi_from_wire)
        sin_psi = math.sqrt(max(0.0, 1.0 - cos_psi ** 2))
        if sin_psi < 1e-6:
            return 0.0
        f = abs(math.cos(math.pi / 2 * cos_psi) / sin_psi)
        if self.kind == "inverted_v":
            # drooping legs fill the ends in and lift the high-angle response
            d = math.radians(self.droop_deg)
            f = (1.0 - 0.5 * math.sin(d)) * f + 0.5 * math.sin(d) * 0.85
        if self.kind == "efhw":
            # end-fed half wave: same pattern, a little extra common-mode loss
            f *= 0.93
        return f

    def _centre_height(self) -> float:
        """Height of the phase centre used for the ground image."""
        if self.kind == "vertical":
            # base-fed radiator: its centre sits half way up
            return max(self.height_m / 2.0, 0.01 * self.wavelength_m)
        if self.kind == "inverted_v":
            return max(0.12 * self.wavelength_m,
                       self.height_m - 0.22 * self.wavelength_m *
                       math.sin(math.radians(self.droop_deg)))
        return max(self.height_m, 0.01 * self.wavelength_m)

    def _gamma(self, theta: float) -> complex:
        return ground_reflection(theta, self.freq_mhz, self.eps_r, self.sigma,
                                 self.horizontal)

    def _array_factor(self, theta: float) -> float:
        """Ground reflection interference term."""
        k = 2 * math.pi / self.wavelength_m
        h = self._centre_height()
        phase = cmath.exp(-2j * k * h * math.sin(theta))
        return abs(1.0 + self._gamma(theta) * phase)

    def _raw(self, theta: float, phi_from_wire: float) -> float:
        return self._element_factor(theta, phi_from_wire) * self._array_factor(theta)

    def _conductor_efficiency(self) -> float:
        """Loss that the reflection model does not capture."""
        if self.kind != "vertical":
            return 0.97           # wire and feedpoint loss, ~0.1 dB
        # a vertical is only as good as its radial field
        n = max(self.radials, 0)
        loss_db = {0: 8.0}.get(n, 0.0)
        if n:
            loss_db = max(0.4, 6.0 - 1.35 * math.log2(max(n, 1)))
        return 10 ** (-loss_db / 10.0)

    def _calibrate(self) -> None:
        """Convert the pattern into absolute dBi, honestly.

        Directivity comes from the pattern integral over the upper hemisphere.
        Gain then subtracts the power the ground absorbs: at each downward
        direction a fraction (1 - |Gamma|^2) of the incident power never comes
        back. That is what makes a low dipole over poor soil come out with the
        modest gain it actually has instead of a flattering normalised figure.
        """
        n_t, n_p = 90, 72
        thetas = np.linspace(0.005, math.pi / 2 - 0.005, n_t)
        phis = np.linspace(0.0, 2 * math.pi, n_p, endpoint=False)
        dt = float(thetas[1] - thetas[0])
        dp = 2 * math.pi / n_p
        radiated = 0.0
        absorbed = 0.0
        peak = 0.0
        for th in thetas:
            t = float(th)
            g2 = abs(self._gamma(t)) ** 2
            w = math.cos(t) * dt * dp
            for ph in phis:
                ef2 = self._element_factor(t, float(ph)) ** 2
                v = ef2 * self._array_factor(t) ** 2
                radiated += v * w
                absorbed += ef2 * (1.0 - g2) * w
                peak = max(peak, v)
        total_in = (radiated + absorbed) / self._conductor_efficiency()
        self._norm = total_in if total_in > 0 else 1.0
        self._peak = peak
        self.peak_gain_dbi = 10 * math.log10(max(4 * math.pi * peak / self._norm, 1e-9))

    # ---------------------------------------------------------------- public
    def gain_at(self, elevation_deg: float, azimuth_deg: float = 0.0) -> float:
        th = math.radians(max(0.05, min(89.95, elevation_deg)))
        phi = math.radians(azimuth_deg - self.orientation_deg)
        v = self._raw(th, phi) ** 2
        return 10 * math.log10(max(4 * math.pi * v / self._norm, 1e-9))

    def describe(self) -> dict:
        d = super().describe()
        d.update({
            "height_m": self.height_m,
            "height_wavelengths": round(self.height_m / self.wavelength_m, 3),
            "orientation_deg": self.orientation_deg,
            "polarisation": "horizontal" if self.horizontal else "vertical",
        })
        a, g = self.elevation_cut()
        d["takeoff_deg"] = float(a[int(np.argmax(g))])
        return d


# ------------------------------------------------------------------ beam/omni

@dataclass
class YagiAntenna(Antenna):
    """Directional beam described by its gain and front-to-back ratio."""
    peak_gain_dbi: float = 11.0
    front_to_back_db: float = 20.0
    polarisation: str = "horizontal"
    kind: str = "yagi"
    name: str = "yagi"
    electrical_downtilt_deg: float = 0.0

    def __post_init__(self):
        # beamwidths implied by the gain, using the standard
        # G ~= 41253 / (BW_az * BW_el) relation for a pencil beam
        g = 10 ** (self.peak_gain_dbi / 10.0)
        bw = math.sqrt(41253.0 / max(g, 1.5))
        self.bw_az = min(160.0, bw * 1.15)
        self.bw_el = min(160.0, bw * 0.87)

    def gain_at(self, elevation_deg: float, azimuth_deg: float = 0.0) -> float:
        az = ((azimuth_deg + 180.0) % 360.0) - 180.0
        el = elevation_deg - self.electrical_downtilt_deg
        # Gaussian main lobe with a sidelobe/backlobe floor
        la = -12.0 * (az / self.bw_az) ** 2
        le = -12.0 * (el / self.bw_el) ** 2
        floor = -self.front_to_back_db
        return self.peak_gain_dbi + max(la + le, floor)


@dataclass
class OmniAntenna(Antenna):
    """Collinear or ground-plane: omnidirectional, gain from vertical stacking."""
    peak_gain_dbi: float = 6.0
    polarisation: str = "vertical"
    kind: str = "omni"
    name: str = "omni"
    electrical_downtilt_deg: float = 0.0

    def __post_init__(self):
        g = 10 ** ((self.peak_gain_dbi - 2.15) / 10.0)
        self.bw_el = max(6.0, 78.0 / max(g, 1.0))

    def gain_at(self, elevation_deg: float, azimuth_deg: float = 0.0) -> float:
        el = elevation_deg - self.electrical_downtilt_deg
        return self.peak_gain_dbi + max(-25.0, -12.0 * (el / self.bw_el) ** 2)


@dataclass
class SectorAntenna(Antenna):
    """Panel/sector antenna as used on masts and for WiFi backhaul APs."""
    peak_gain_dbi: float = 16.0
    beamwidth_az_deg: float = 90.0
    beamwidth_el_deg: float = 9.0
    front_to_back_db: float = 25.0
    polarisation: str = "dual"
    kind: str = "sector"
    name: str = "sector"
    electrical_downtilt_deg: float = 0.0

    def gain_at(self, elevation_deg: float, azimuth_deg: float = 0.0) -> float:
        az = ((azimuth_deg + 180.0) % 360.0) - 180.0
        el = elevation_deg - self.electrical_downtilt_deg
        la = min(12.0 * (az / self.beamwidth_az_deg) ** 2, self.front_to_back_db)
        le = min(12.0 * (el / self.beamwidth_el_deg) ** 2, self.front_to_back_db)
        return self.peak_gain_dbi - min(la + le, self.front_to_back_db)


@dataclass
class ParabolicAntenna(Antenna):
    """Dish using the ITU-R F.699 reference radiation pattern.

    That is the pattern coordination calculations use, so an off-axis number
    from here is defensible when someone asks whether a link will interfere
    with a licensed user.
    """
    diameter_m: float = 0.6
    freq_mhz: float = 5760.0
    efficiency: float = 0.55
    polarisation: str = "dual"
    kind: str = "dish"
    name: str = "dish"

    def __post_init__(self):
        lam = C / (self.freq_mhz * 1e6)
        self.wavelength_m = lam
        self.d_over_lambda = self.diameter_m / lam
        self.peak_gain_dbi = 10 * math.log10(
            self.efficiency * (math.pi * self.diameter_m / lam) ** 2)
        # F.699 breakpoints
        g = self.peak_gain_dbi
        self.phi_m = 20.0 / self.d_over_lambda * math.sqrt(max(g - 7.7, 0.1))
        self.phi_r = 15.85 * self.d_over_lambda ** -0.6
        self.beamwidth_deg = 70.0 / max(self.d_over_lambda, 0.1)
        if self.name == "dish":
            self.name = f"{self.diameter_m:g} m dish"

    def _off_axis(self, phi: float) -> float:
        g = self.peak_gain_dbi
        dl = self.d_over_lambda
        phi = abs(phi)
        if dl >= 100.0:
            if phi < self.phi_m:
                return g - 2.5e-3 * (dl * phi) ** 2
            if phi < max(self.phi_r, 1.0):
                # the plateau between the main lobe and the sidelobe envelope
                return g - 2.5e-3 * (dl * self.phi_m) ** 2
            if phi < 48.0:
                return 32.0 - 25.0 * math.log10(max(phi, 1e-3))
            return -10.0
        # small dishes (D/lambda < 100)
        if phi < self.phi_m:
            return g - 2.5e-3 * (dl * phi) ** 2
        if phi < 48.0:
            return 52.0 - 10.0 * math.log10(dl) - 25.0 * math.log10(max(phi, 1e-3))
        return 10.0 - 10.0 * math.log10(dl)

    def gain_at(self, elevation_deg: float, azimuth_deg: float = 0.0) -> float:
        az = ((azimuth_deg + 180.0) % 360.0) - 180.0
        phi = math.hypot(az, elevation_deg)
        return min(self.peak_gain_dbi, self._off_axis(phi))

    def describe(self) -> dict:
        d = super().describe()
        d.update({"diameter_m": self.diameter_m,
                  "beamwidth_deg": round(self.beamwidth_deg, 2),
                  "freq_mhz": self.freq_mhz})
        return d


def isotropic(gain_dbi: float = 0.0) -> Antenna:
    a = Antenna()
    a.peak_gain_dbi = gain_dbi
    a.gain_at = lambda e, z=0.0: gain_dbi  # type: ignore[assignment]
    return a

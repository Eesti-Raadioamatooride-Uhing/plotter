"""HF: ground wave, NVIS and skywave hop geometry.

ITM is only valid from 20 MHz up, so 160-10 m needs its own treatment. What
this module answers is the question an amateur actually asks: "if I hang an
80 m dipole at 12 m, who can hear me?"

Three mechanisms, computed separately and then combined:

ground wave   Norton flat-earth surface-wave attenuation over the real
              ground constants of the path. Dominant to 100-300 km on 160/80
              and the thing that decides local daytime coverage.
NVIS          near-vertical incidence skywave, 0-400 km, the mode that fills
              in the ground-wave skip zone on 80/60/40 by day and 160/80 by
              night. Works only below foF2 scaled for near-vertical incidence.
skywave hop   E and F2 layer hops for DX, with the takeoff angle each hop
              distance needs, checked against the antenna's own elevation
              pattern and against the path MUF.

The ionosphere is modelled, not measured. Feed it a real foF2 from the
Sodankyla or Juliusruh ionosonde (see plotter.core.data.ionosphere) and the
numbers get considerably better.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

C = 299_792_458.0
EARTH_R = 6_371_008.8

# Typical layer heights, km
H_E = 110.0
H_F2_DAY = 300.0
H_F2_NIGHT = 350.0


# --------------------------------------------------------------- ground wave

def norton_ground_wave_field_db(distance_km: float, freq_mhz: float,
                                eps_r: float = 15.0, sigma: float = 0.005,
                                tx_power_w: float = 100.0,
                                gain_dbi: float = 0.0) -> tuple[float, float]:
    """Return (field strength dB(uV/m), path loss dB) for the surface wave.

    Norton's flat-earth approximation with the numerical distance p. Valid
    for vertical polarisation and antennas close to the ground, which is the
    situation for HF ground wave. Horizontal polarisation is attenuated far
    more strongly and is handled by the caller.
    """
    if distance_km <= 0:
        return 0.0, 0.0
    lam = C / (freq_mhz * 1e6)
    d = distance_km * 1000.0
    x = 18000.0 * sigma / freq_mhz
    eps_c = complex(eps_r, -x)
    b = math.atan2(eps_r + 1.0, x)
    p = (math.pi * d / lam) * math.cos(b) / abs(eps_c)
    a = ((2.0 + 0.3 * p) / (2.0 + p + 0.6 * p * p)
         - math.sin(b) * math.sqrt(p / 2.0) * math.exp(-0.625 * p))
    a = max(abs(a), 1e-9)

    # Unattenuated field of a short vertical over perfect ground, mV/m at d
    eirp_w = tx_power_w * (10 ** (gain_dbi / 10.0))
    e0_mv = 300.0 * math.sqrt(eirp_w / 1000.0) / distance_km
    e_mv = e0_mv * a
    e_dbuv = 20.0 * math.log10(max(e_mv * 1000.0, 1e-9))

    # Convert field strength to basic transmission loss
    # Prx(dBm) = E(dBuV/m) - 20log10(f_MHz) - 77.2  (for an isotropic receiver)
    prx_dbm = e_dbuv - 20.0 * math.log10(freq_mhz) - 77.2
    ptx_dbm = 10.0 * math.log10(tx_power_w * 1000.0) + gain_dbi
    return e_dbuv, ptx_dbm - prx_dbm


def ground_wave_range_km(freq_mhz: float, tx_power_w: float, gain_dbi: float,
                         sensitivity_dbm: float, eps_r: float, sigma: float,
                         max_km: float = 1500.0) -> float:
    """Distance at which the surface wave drops to the receiver's sensitivity."""
    lo, hi = 0.5, max_km
    ptx_dbm = 10.0 * math.log10(max(tx_power_w, 1e-3) * 1000.0) + gain_dbi
    for _ in range(60):
        mid = math.sqrt(lo * hi)
        _, loss = norton_ground_wave_field_db(mid, freq_mhz, eps_r, sigma,
                                              tx_power_w, gain_dbi)
        if ptx_dbm - loss > sensitivity_dbm:
            lo = mid
        else:
            hi = mid
        if hi / lo < 1.001:
            break
    return lo


# --------------------------------------------------------------- ionosphere

@dataclass
class Ionosphere:
    """The handful of numbers the hop model needs."""
    fo_f2_mhz: float = 6.0        # F2 critical frequency
    fo_e_mhz: float = 2.6         # E layer critical frequency
    h_f2_km: float = 300.0
    h_e_km: float = 110.0
    absorption_index: float = 1.0  # scales D-layer absorption, 0 at night
    source: str = "model"
    is_day: bool = True

    @classmethod
    def model(cls, lat: float, lon: float, when, ssn: float = 60.0) -> "Ionosphere":
        """A crude but not silly diurnal/seasonal/solar model for 57-70 N.

        Not IRI. Enough to tell a user that 80 m NVIS closes at 03:00 in
        January and that 20 m opens in the afternoon in June.
        """
        doy = when.timetuple().tm_yday
        # solar zenith angle
        decl = math.radians(23.44) * math.sin(2 * math.pi * (doy - 81) / 365.25)
        hour_utc = when.hour + when.minute / 60.0
        ha = math.radians(15.0 * (hour_utc + lon / 15.0 - 12.0))
        phi = math.radians(lat)
        cos_chi = (math.sin(phi) * math.sin(decl) +
                   math.cos(phi) * math.cos(decl) * math.cos(ha))
        chi = math.acos(max(-1.0, min(1.0, cos_chi)))
        is_day = cos_chi > -0.1

        # foF2 grows with solar zenith and sunspot number
        solar = max(0.0, cos_chi)
        base_day = 3.6 + 5.4 * solar ** 0.55
        base_night = 2.6 + 1.4 * solar
        fo_f2 = base_night + (base_day - base_night) * (1.0 if is_day else 0.25)
        fo_f2 *= (1.0 + 0.0055 * ssn)
        # winter anomaly: daytime foF2 is higher in winter at these latitudes
        seasonal = 1.0 + 0.12 * math.cos(2 * math.pi * (doy - 15) / 365.25)
        fo_f2 *= seasonal

        fo_e = 0.9 * (1.0 + 0.007 * ssn) ** 0.5 * max(solar, 0.02) ** 0.25 * 3.4
        h_f2 = H_F2_DAY if is_day else H_F2_NIGHT
        # ITU-R P.533 absorption factor: (1 + 0.0037 R12) (cos 0.881 chi)^1.3
        chi_eff = min(chi, math.pi / 2)
        absorption = ((1.0 + 0.0037 * ssn) *
                      max(0.0, math.cos(0.881 * chi_eff)) ** 1.3)
        if cos_chi < 0:                      # night: the D layer recombines away
            absorption *= max(0.0, 1.0 + cos_chi * 6.0)
        return cls(fo_f2_mhz=max(1.8, fo_f2), fo_e_mhz=max(0.4, fo_e),
                   h_f2_km=h_f2, h_e_km=H_E,
                   absorption_index=max(0.0, absorption), source="model",
                   is_day=is_day)


def obliquity_factor(distance_km: float, layer_height_km: float) -> float:
    """secant of the incidence angle at the layer - the MUF multiplier."""
    if distance_km <= 0:
        return 1.0
    half = distance_km / 2.0
    gamma = half / (EARTH_R / 1000.0)            # half the great-circle angle
    h = layer_height_km
    re = EARTH_R / 1000.0
    # elevation angle of the ray at the ground
    beta = math.atan2(math.cos(gamma) - re / (re + h), math.sin(gamma))
    # angle of incidence at the layer
    phi = math.asin(max(-1.0, min(1.0, re * math.cos(beta) / (re + h))))
    return 1.0 / max(math.cos(phi), 1e-6)


def takeoff_angle_deg(distance_km: float, layer_height_km: float,
                      hops: int = 1) -> float:
    """Elevation angle needed to reach `distance_km` in `hops` hops."""
    if distance_km <= 0:
        return 90.0
    d = distance_km / max(1, hops)
    re = EARTH_R / 1000.0
    gamma = (d / 2.0) / re
    h = layer_height_km
    beta = math.atan2(math.cos(gamma) - re / (re + h), math.sin(gamma))
    return math.degrees(max(0.0, beta))


def max_hop_distance_km(layer_height_km: float, min_takeoff_deg: float = 1.0) -> float:
    """Longest single hop from a layer, limited by the lowest usable angle."""
    re = EARTH_R / 1000.0
    b = math.radians(min_takeoff_deg)
    h = layer_height_km
    # solve for gamma given beta
    phi = math.asin(max(-1.0, min(1.0, re * math.cos(b) / (re + h))))
    gamma = math.pi / 2 - b - phi
    return 2.0 * gamma * re


# --------------------------------------------------------------- absorption

def d_layer_absorption_db(freq_mhz: float, takeoff_deg: float, hops: int,
                          iono: Ionosphere) -> float:
    """Non-deviative D-layer absorption, ITU-R P.533 form.

    Li = 677.2 * sec(i) * AT / ((f + fL)^1.98 + 10.2) per hop, where AT
    carries the solar-zenith and solar-activity dependence and fL is the
    electron gyrofrequency component along the path (about 1.4 MHz at
    Baltic latitudes). This is the term that makes 80 m a night band: at
    local noon it costs about 20 dB a hop, at midnight under 2 dB.
    """
    sec_i = 1.0 / max(math.sin(math.radians(max(takeoff_deg, 2.0))), 0.03)
    sec_i = min(sec_i, 3.5)           # the D layer is thin; cap the obliquity
    gyro = 1.4
    per_hop = (677.2 * max(iono.absorption_index, 0.0) * sec_i /
               ((freq_mhz + gyro) ** 1.98 + 10.2))
    return hops * (per_hop + 1.0)     # 1 dB a hop of residual/deviative loss


def excess_system_loss_db(hops: int) -> float:
    """ITU-R P.533 excess system loss: the gap between theory and reality."""
    return 9.9 + 2.7 * max(0, hops - 1)


# --------------------------------------------------------------- the model

@dataclass
class HFMode:
    name: str                    # "ground wave" | "NVIS" | "1F2" ...
    distance_km: float
    takeoff_deg: float
    hops: int
    layer: str
    path_loss_db: float
    antenna_gain_dbi: float
    rx_level_dbm: float
    s_meter: str
    muf_mhz: float
    usable: bool
    note: str = ""


@dataclass
class HFPrediction:
    freq_mhz: float
    ionosphere: Ionosphere
    ground_wave_range_km: float
    nvis_ok: bool
    nvis_range_km: float
    muf_3000_mhz: float
    modes: list[HFMode] = field(default_factory=list)
    rings_km: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# IARU Region 1 Technical Recommendation R.1. S9 is 50 uV at the receiver
# input on HF and 5 uV above 30 MHz, which is 20 dB apart, and one S unit is
# 6 dB on both. Reading a 2 m signal against the HF reference reports it a
# full 20 dB (more than three S units) low.
S9_HF_DBM = -73.0
S9_VHF_DBM = -93.0
HF_LIMIT_MHZ = 30.0


def s9_reference_dbm(freq_mhz: float) -> float:
    """The dBm that reads S9 on a correctly calibrated meter at this frequency."""
    return S9_HF_DBM if freq_mhz < HF_LIMIT_MHZ else S9_VHF_DBM


def s_meter_label(dbm: float, freq_mhz: float | None = None) -> str:
    """S-meter reading, e.g. "S7" or "S9+20 dB".

    Without a frequency this keeps the HF reference, which is what the HF
    predictor wants and what every existing caller assumed.
    """
    ref = S9_HF_DBM if freq_mhz is None else s9_reference_dbm(freq_mhz)
    if dbm >= ref:
        over = dbm - ref
        return f"S9+{int(round(over))} dB" if over >= 1 else "S9"
    s = 9.0 + (dbm - ref) / 6.0
    if s < 0.5:
        return "below S1"
    return f"S{int(round(max(1.0, s)))}"


def predict(freq_mhz: float, distances_km: list[float], *,
            iono: Ionosphere, tx_power_w: float = 100.0,
            antenna=None, eps_r: float = 15.0, sigma: float = 0.005,
            rx_sensitivity_dbm: float = -127.0,
            polarisation: str = "horizontal") -> HFPrediction:
    """Predict coverage on one band for a list of distances.

    `antenna` is anything with `.gain_at(elevation_deg, azimuth_deg) -> dBi`;
    pass an HF dipole model and its takeoff-angle response is what decides
    which hops are actually usable.
    """
    def gain(elev_deg: float) -> float:
        if antenna is None:
            return 2.15
        return antenna.gain_at(elev_deg, 0.0)

    ptx_dbm = 10.0 * math.log10(max(tx_power_w, 1e-3) * 1000.0)

    # ground wave (vertical polarisation only; horizontal dies fast)
    if polarisation.startswith("v"):
        gw_range = ground_wave_range_km(freq_mhz, tx_power_w, gain(3.0),
                                        rx_sensitivity_dbm, eps_r, sigma)
    else:
        # a horizontal antenna a fraction of a wavelength up radiates almost
        # no surface wave; keep a token range so the map is not misleading
        gw_range = min(40.0, ground_wave_range_km(freq_mhz, tx_power_w,
                                                  gain(3.0) - 20.0,
                                                  rx_sensitivity_dbm,
                                                  eps_r, sigma))

    # NVIS: usable when the vertical-incidence critical frequency is above f
    nvis_ok = freq_mhz <= iono.fo_f2_mhz * 1.05
    nvis_range = 0.0
    modes: list[HFMode] = []
    notes: list[str] = []

    if nvis_ok:
        nvis_range = 350.0
    elif freq_mhz <= iono.fo_f2_mhz * 1.3:
        nvis_range = 250.0
        notes.append("NVIS marginal - f is just above foF2, only the shallower "
                     "angles reflect")
    else:
        notes.append(f"No NVIS: {freq_mhz:.3f} MHz is well above foF2 "
                     f"({iono.fo_f2_mhz:.1f} MHz), signals go straight through")

    muf3000 = iono.fo_f2_mhz * obliquity_factor(3000.0, iono.h_f2_km)

    for dkm in distances_km:
        best: HFMode | None = None
        # ground wave
        if dkm <= gw_range * 3:
            _, gwl = norton_ground_wave_field_db(dkm, freq_mhz, eps_r, sigma,
                                                 tx_power_w, 0.0)
            if polarisation.startswith("h"):
                gwl += 20.0
            g = gain(3.0)
            rx = ptx_dbm + g - gwl
            best = HFMode(name="ground wave", distance_km=dkm, takeoff_deg=0.0,
                          hops=0, layer="surface", path_loss_db=gwl,
                          antenna_gain_dbi=g, rx_level_dbm=rx,
                          s_meter=s_meter_label(rx), muf_mhz=float("inf"),
                          usable=rx >= rx_sensitivity_dbm)

        # skywave hops off E and F2
        for layer, h, fo in (("E", iono.h_e_km, iono.fo_e_mhz),
                             ("F2", iono.h_f2_km, iono.fo_f2_mhz)):
            max_hop = max_hop_distance_km(h, 1.0)
            for hops in range(1, 6):
                if dkm / hops > max_hop:
                    continue
                toa = takeoff_angle_deg(dkm, h, hops)
                if toa <= 0.5 and dkm > 100:
                    continue
                muf = fo * obliquity_factor(dkm / hops, h)
                if freq_mhz > muf:
                    continue
                # free space over the actual ray path, plus absorption and
                # ground-reflection loss at each intermediate bounce
                ray_km = hop_path_length_km(dkm, h, hops)
                fsl = 32.44778 + 20 * math.log10(freq_mhz) + 20 * math.log10(ray_km)
                absorb = d_layer_absorption_db(freq_mhz, toa, hops, iono)
                refl = 2.5 * (hops - 1) + excess_system_loss_db(hops)
                # deviative loss rises sharply as f approaches the MUF
                ratio = freq_mhz / max(muf, 1e-6)
                dev = 0.0 if ratio < 0.85 else 15.0 * (ratio - 0.85) / 0.15
                loss = fsl + absorb + refl + dev
                g = gain(toa)
                rx = ptx_dbm + g - loss
                m = HFMode(name=f"{hops}{layer}", distance_km=dkm,
                           takeoff_deg=toa, hops=hops, layer=layer,
                           path_loss_db=loss, antenna_gain_dbi=g,
                           rx_level_dbm=rx, s_meter=s_meter_label(rx),
                           muf_mhz=muf, usable=rx >= rx_sensitivity_dbm)
                if best is None or m.rx_level_dbm > best.rx_level_dbm:
                    best = m

        if best is None:
            best = HFMode(name="none", distance_km=dkm, takeoff_deg=0.0, hops=0,
                          layer="-", path_loss_db=999.0, antenna_gain_dbi=0.0,
                          rx_level_dbm=-999.0, s_meter="-", muf_mhz=0.0,
                          usable=False, note="above the MUF for every hop count")
        modes.append(best)

    return HFPrediction(freq_mhz=freq_mhz, ionosphere=iono,
                        ground_wave_range_km=gw_range, nvis_ok=nvis_ok,
                        nvis_range_km=nvis_range, muf_3000_mhz=muf3000,
                        modes=modes, rings_km=list(distances_km), notes=notes)


def hop_path_length_km(distance_km: float, layer_height_km: float,
                       hops: int) -> float:
    """Slant length of a multi-hop ray, which is what free-space loss sees."""
    d = distance_km / max(1, hops)
    re = EARTH_R / 1000.0
    gamma = (d / 2.0) / re
    h = layer_height_km
    # law of cosines on the triangle ground - reflection point - ground
    leg = math.sqrt(re ** 2 + (re + h) ** 2 -
                    2 * re * (re + h) * math.cos(gamma))
    return max(1.0, 2.0 * leg * hops)

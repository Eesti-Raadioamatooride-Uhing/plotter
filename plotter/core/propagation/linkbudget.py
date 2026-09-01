"""Point-to-point link budget: everything from an 80 m NVIS shot to 24 GHz."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

from ..antennas.patterns import Antenna
from ..terrain.profile import (LinkGeometry, analyse_link, free_space_loss_db,
                               horizon_distance_km,
                               required_height_for_clearance)
from . import atmosphere as atm
from . import diffraction as dif
from . import itm

K_BOLTZMANN_DBM = -198.6  # dBm/Hz/K


@dataclass
class Endpoint:
    name: str
    lat: float
    lon: float
    height_agl_m: float
    antenna: Antenna
    tx_power_w: float = 25.0
    feedline_loss_db: float = 1.0
    rx_noise_figure_db: float = 6.0
    ground_amsl_m: float = 0.0


@dataclass
class LinkResult:
    # geometry
    distance_km: float
    azimuth_deg: float
    reverse_azimuth_deg: float
    tx_elevation_deg: float
    rx_elevation_deg: float
    tx_amsl_m: float
    rx_amsl_m: float
    is_los: bool
    worst_clearance_m: float
    worst_clearance_at_km: float
    min_fresnel_fraction: float
    first_fresnel_radius_m: float
    k_factor: float
    terrain_source: str
    terrain_resolution_m: float

    # losses
    free_space_db: float
    itm_loss_db: float
    itm_mode: str
    itm_warning: str
    diffraction_db: float
    diffraction_edges: list[dict]
    gas_loss_db: float
    rain_loss_db: float
    total_path_loss_db: float

    # budget
    tx_power_dbm: float
    eirp_dbm: float
    tx_gain_dbi: float
    rx_gain_dbi: float
    rx_level_dbm: float
    noise_floor_dbm: float
    snr_db: float
    sensitivity_dbm: float
    link_margin_db: float
    multipath_fade_margin_db: float
    availability_pct: float
    verdict: str

    # aiming
    tx_bearing_true_deg: float
    rx_bearing_true_deg: float
    tx_tilt_deg: float
    rx_tilt_deg: float

    # advice
    recommended_tx_height_m: float | None = None
    recommended_rx_height_m: float | None = None
    notes: list[str] = field(default_factory=list)
    profile: dict | None = None


def _verdict(margin: float) -> str:
    if margin >= 20:
        return "solid"
    if margin >= 10:
        return "workable"
    if margin >= 3:
        return "marginal"
    if margin >= 0:
        return "on the edge"
    return "will not work"


def evaluate(profile, tx: Endpoint, rx: Endpoint, freq_mhz: float, *,
             mode_sensitivity_dbm: float = -95.0, bandwidth_hz: float = 20e6,
             k_factor: float = 4.0 / 3.0, clutter_m: float = 0.0,
             climate: int = itm.CLIMATE_MARITIME_TEMPERATE_LAND,
             polarisation: str = "vertical",
             ground: str = "average",
             target_availability_pct: float = 99.9,
             reliability: float = 0.9, confidence: float = 0.5,
             include_profile: bool = True,
             use_itm: bool | None = None) -> LinkResult:
    """Run the whole chain over an already-built terrain profile."""
    eps, sigma = itm.GROUND_TYPES.get(ground, (15.0, 0.005))
    # Use the surface (terrain + obstruction) whenever the profile carries any
    # clutter, whether that is a flat clutter height or per-point building data
    # baked into profile.clutter_m.
    use_clutter = clutter_m > 0 or bool(np.any(profile.clutter_m > 0))
    geo: LinkGeometry = analyse_link(profile, tx.height_agl_m, rx.height_agl_m,
                                     freq_mhz, k_factor, use_clutter=use_clutter)
    d_km = profile.total_distance_m / 1000.0
    fsl = free_space_loss_db(profile.total_distance_m, freq_mhz)

    notes: list[str] = []

    # --- ITM ------------------------------------------------------------
    if use_itm is None:
        use_itm = 20.0 <= freq_mhz <= 20000.0
    if use_itm:
        r = itm.point_to_point(
            profile.itm_array(use_clutter=use_clutter),
            max(tx.height_agl_m, 0.5), max(rx.height_agl_m, 0.5), freq_mhz,
            eps_dielect=eps, sgm_conductivity=sigma,
            radio_climate=climate,
            polarization=(itm.POL_VERTICAL if polarisation.startswith("v")
                          else itm.POL_HORIZONTAL),
            conf=confidence, rel=reliability)
        itm_loss, itm_mode, itm_warn = r.loss_db, r.mode, r.warning_text
        if r.warning >= 3:
            notes.append(f"ITM: {r.warning_text}")
    else:
        itm_loss, itm_mode, itm_warn = float("nan"), "not applicable", (
            "ITM is only valid 20 MHz - 20 GHz; using explicit diffraction")

    # --- explicit diffraction -------------------------------------------
    surf = profile.surface_m if use_clutter else profile.elevations_m
    dif_db, edges = dif.deygout_db(profile.distances_m, surf,
                                   geo.tx_amsl_m, geo.rx_amsl_m, freq_mhz,
                                   k_factor)
    sph = dif.spherical_earth_db(profile.total_distance_m,
                                 tx.height_agl_m, rx.height_agl_m, freq_mhz,
                                 k_factor, polarisation, eps, sigma)
    dif_db = max(dif_db, sph)

    # --- atmosphere ------------------------------------------------------
    f_ghz = freq_mhz / 1000.0
    gas = 0.0
    rain = 0.0
    if f_ghz >= 1.0:
        gas = atm.gaseous_attenuation_db_per_km(f_ghz) * d_km
        rr = atm.rain_rate_for(profile.lats[0], profile.lons[0])
        rain = atm.rain_attenuation_db(d_km, f_ghz, rr, polarisation,
                                       percent=100.0 - target_availability_pct)

    # --- pick the governing path loss ------------------------------------
    deterministic = fsl + dif_db + gas + rain
    if use_itm and math.isfinite(itm_loss):
        total = itm_loss + gas + rain
        # On a clear LOS microwave hop ITM's statistical spread is pessimistic;
        # the deterministic budget is what the link will actually see.
        if geo.is_los and geo.min_fresnel_fraction > 0.6 and f_ghz >= 1.0:
            total = deterministic
            notes.append("Clear Fresnel zone: using the deterministic budget "
                         "(free space + gas + rain) rather than ITM statistics")
    else:
        total = deterministic

    # --- budget ----------------------------------------------------------
    ptx_dbm = 10 * math.log10(max(tx.tx_power_w, 1e-4) * 1000.0)
    gt = tx.antenna.gain_at(geo.tx_tilt_deg, 0.0)
    gr = rx.antenna.gain_at(geo.rx_tilt_deg, 0.0)
    eirp = ptx_dbm - tx.feedline_loss_db + gt
    prx = eirp - total + gr - rx.feedline_loss_db
    # kTB at 290 K is -174 dBm/Hz
    noise = -174.0 + 10 * math.log10(max(bandwidth_hz, 1.0)) + rx.rx_noise_figure_db
    snr = prx - noise
    margin = prx - mode_sensitivity_dbm

    # multipath, only meaningful for microwave LOS hops
    inclination_mrad = abs((geo.rx_amsl_m - geo.tx_amsl_m) /
                           max(profile.total_distance_m, 1.0)) * 1000.0
    mp = 0.0
    if f_ghz >= 1.0 and geo.is_los:
        mp = atm.multipath_fade_margin_db(d_km, f_ghz, inclination_mrad,
                                          target_availability_pct)

    # --- advice ----------------------------------------------------------
    rec_tx = rec_rx = None
    if geo.min_fresnel_fraction < 0.6:
        rec_tx = required_height_for_clearance(profile, rx.height_agl_m,
                                               freq_mhz, at_tx=True,
                                               k_factor=k_factor)
        rec_rx = required_height_for_clearance(profile, tx.height_agl_m,
                                               freq_mhz, at_tx=False,
                                               k_factor=k_factor)
        if rec_tx > 290 and rec_rx > 290:
            notes.append("No practical antenna height clears the path - this "
                         "one needs a relay site")
            rec_tx = rec_rx = None
        else:
            notes.append(
                f"Path is {geo.min_fresnel_fraction * 100:.0f}% of the first "
                f"Fresnel zone at {geo.worst.distance_m / 1000:.1f} km. "
                f"Raising one end to about "
                f"{min(x for x in (rec_tx, rec_rx) if x is not None):.0f} m would "
                "restore 60% clearance.")

    if f_ghz >= 10.0 and rain > 3.0:
        notes.append(f"Rain fade of {rain:.1f} dB at {target_availability_pct}% "
                     "availability dominates this budget - size the margin for it")
    if not geo.is_los and f_ghz >= 5.0:
        notes.append("Obstructed path above 5 GHz: diffraction loss here is "
                     "severe and highly frequency dependent. Treat the number "
                     "as a lower bound on loss.")
    hor = horizon_distance_km(tx.height_agl_m + 0.0, k_factor) + \
        horizon_distance_km(rx.height_agl_m, k_factor)
    if d_km > hor * 1.5 and f_ghz >= 0.1:
        notes.append(f"Well beyond the {hor:.0f} km combined radio horizon - "
                     "this is a diffraction or troposcatter path, expect it to "
                     "be unreliable")

    prof_payload = None
    if include_profile:
        step = max(1, len(profile.distances_m) // 600)
        prof_payload = {
            "distance_km": [round(float(v) / 1000.0, 4)
                            for v in profile.distances_m[::step]],
            "terrain_m": [round(float(v), 1) for v in profile.elevations_m[::step]],
            "surface_m": [round(float(v), 1) for v in profile.surface_m[::step]],
            "los_m": [round(float(v), 1) for v in geo.los_line_m[::step]],
            "effective_terrain_m": [round(float(v), 1)
                                    for v in geo.effective_terrain_m[::step]],
            "fresnel_upper_m": [round(float(a + b), 1) for a, b in
                                zip(geo.los_line_m[::step], geo.f1_m[::step])],
            "fresnel_lower_m": [round(float(a - b), 1) for a, b in
                                zip(geo.los_line_m[::step], geo.f1_m[::step])],
            "fresnel_60_lower_m": [round(float(a - 0.6 * b), 1) for a, b in
                                   zip(geo.los_line_m[::step], geo.f1_m[::step])],
            "lat": [round(float(v), 6) for v in profile.lats[::step]],
            "lon": [round(float(v), 6) for v in profile.lons[::step]],
        }

    return LinkResult(
        distance_km=round(d_km, 4),
        azimuth_deg=round(profile.bearing_deg, 3),
        reverse_azimuth_deg=round(profile.reverse_bearing_deg, 3),
        tx_elevation_deg=round(geo.tx_tilt_deg, 4),
        rx_elevation_deg=round(geo.rx_tilt_deg, 4),
        tx_amsl_m=round(geo.tx_amsl_m, 1), rx_amsl_m=round(geo.rx_amsl_m, 1),
        is_los=geo.is_los,
        worst_clearance_m=round(geo.worst.clearance_m, 1),
        worst_clearance_at_km=round(geo.worst.distance_m / 1000.0, 3),
        min_fresnel_fraction=round(geo.min_fresnel_fraction, 3),
        first_fresnel_radius_m=round(float(np.max(geo.f1_m)), 2),
        k_factor=k_factor, terrain_source=profile.source,
        terrain_resolution_m=profile.resolution_m,
        free_space_db=round(fsl, 2),
        itm_loss_db=(round(itm_loss, 2) if math.isfinite(itm_loss) else None),
        itm_mode=itm_mode, itm_warning=itm_warn,
        diffraction_db=round(dif_db, 2), diffraction_edges=edges,
        gas_loss_db=round(gas, 3), rain_loss_db=round(rain, 2),
        total_path_loss_db=round(total, 2),
        tx_power_dbm=round(ptx_dbm, 2), eirp_dbm=round(eirp, 2),
        tx_gain_dbi=round(gt, 2), rx_gain_dbi=round(gr, 2),
        rx_level_dbm=round(prx, 2), noise_floor_dbm=round(noise, 2),
        snr_db=round(snr, 2), sensitivity_dbm=mode_sensitivity_dbm,
        link_margin_db=round(margin, 2),
        multipath_fade_margin_db=round(mp, 2),
        availability_pct=target_availability_pct,
        verdict=_verdict(margin),
        tx_bearing_true_deg=round(profile.bearing_deg, 2),
        rx_bearing_true_deg=round(profile.reverse_bearing_deg, 2),
        tx_tilt_deg=round(geo.tx_tilt_deg, 3),
        rx_tilt_deg=round(geo.rx_tilt_deg, 3),
        recommended_tx_height_m=(round(rec_tx, 1) if rec_tx else None),
        recommended_rx_height_m=(round(rec_rx, 1) if rec_rx else None),
        notes=notes, profile=prof_payload,
    )


def to_dict(r: LinkResult) -> dict:
    return asdict(r)

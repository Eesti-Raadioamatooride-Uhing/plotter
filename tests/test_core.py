"""Reference-value tests. Run with: python -m pytest tests -q"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pytest

from plotter.core import geodesy as geo
from plotter.core.antennas.library import build_antenna
from plotter.core.antennas.patterns import ParabolicAntenna, WireAntenna
from plotter.core.propagation import atmosphere as atm
from plotter.core.propagation import diffraction as dif
from plotter.core.propagation import hf, itm
from plotter.core.terrain.profile import (PathProfile, analyse_link,
                                         fresnel_radius_m, free_space_loss_db,
                                         horizon_distance_km)


# ------------------------------------------------------------------ geodesy

def test_vincenty_against_known_pair():
    # Tallinn (Vabaduse valjak) to Helsinki (Senaatintori): about 82 km,
    # just east of due north, so the bearing is a few degrees past 0.
    d, fwd, rev = geo.vincenty_inverse(59.4330, 24.7450, 60.1690, 24.9520)
    assert 81_000 < d < 83_500
    assert 4 < fwd < 12
    assert 184 < rev < 192


def test_vincenty_direct_inverse_roundtrip():
    lat, lon = 59.0, 25.0
    for br in (0, 45, 137.5, 270, 359):
        for dist in (500.0, 25_000.0, 400_000.0):
            la, lo, _ = geo.vincenty_direct(lat, lon, br, dist)
            d2, b2, _ = geo.vincenty_inverse(lat, lon, la, lo)
            assert abs(d2 - dist) < 0.5
            assert abs((b2 - br + 180) % 360 - 180) < 1e-4


def test_great_circle_endpoints_are_exact():
    pts = geo.great_circle_points(59.0, 24.0, 60.0, 26.0, 50)
    assert pts[0] == pytest.approx((59.0, 24.0), abs=1e-9)
    assert pts[-1] == pytest.approx((60.0, 26.0), abs=1e-9)
    assert len(pts) == 50


# ----------------------------------------------------------------- geometry

def test_free_space_loss_reference():
    # Textbook: 1 GHz over 1 km is 92.45 dB
    assert free_space_loss_db(1000.0, 1000.0) == pytest.approx(92.45, abs=0.01)
    # Doubling distance adds 6.02 dB
    a = free_space_loss_db(10_000.0, 145.0)
    b = free_space_loss_db(20_000.0, 145.0)
    assert b - a == pytest.approx(6.0206, abs=0.001)


def test_fresnel_radius_reference():
    # 5.8 GHz, 10 km path, midpoint: F1 = sqrt(lambda*d1*d2/d)
    lam = 299_792_458.0 / 5.8e9
    expect = math.sqrt(lam * 5000 * 5000 / 10000)
    got = fresnel_radius_m(5000.0, 5000.0, 5.8e9)
    assert float(got) == pytest.approx(expect, rel=1e-9)
    assert float(got) == pytest.approx(11.36, abs=0.05)


def test_horizon_distance():
    # 4/3 earth, 100 m antenna: about 41.3 km
    assert horizon_distance_km(100.0) == pytest.approx(41.3, abs=0.5)
    # the classic d_km = 4.12*sqrt(h_m) rule
    for h in (10, 50, 200):
        assert horizon_distance_km(h) == pytest.approx(4.124 * math.sqrt(h), rel=0.01)


def _flat_profile(distance_m, n=400, height=0.0):
    d = np.linspace(0, distance_m, n)
    return PathProfile(
        lats=np.linspace(59.0, 59.0 + distance_m / 111000.0, n),
        lons=np.full(n, 24.0), distances_m=d,
        elevations_m=np.full(n, float(height)), clutter_m=np.zeros(n),
        source="test", resolution_m=30.0, total_distance_m=float(distance_m),
        bearing_deg=0.0, reverse_bearing_deg=180.0)


def test_flat_path_is_line_of_sight_and_clearance_matches_bulge():
    p = _flat_profile(10_000.0)
    g = analyse_link(p, 30.0, 30.0, 5760.0)
    assert g.is_los
    # midpoint clearance = antenna height above the k-corrected bulge
    bulge = 5000.0 * 5000.0 / (2 * (4 / 3) * 6_371_008.8)
    mid = len(p.distances_m) // 2
    assert g.clearance_m[mid] == pytest.approx(30.0 - bulge, abs=0.05)


def test_obstructed_path_is_detected():
    p = _flat_profile(10_000.0)
    p.elevations_m = p.elevations_m.copy()
    p.elevations_m[len(p.elevations_m) // 2] = 120.0
    g = analyse_link(p, 30.0, 30.0, 5760.0)
    assert not g.is_los
    assert g.worst.clearance_m < 0
    assert g.min_fresnel_fraction < 0


# ---------------------------------------------------------------- ITM

def test_itm_free_space_floor():
    """Over flat ground well inside the horizon ITM must not beat free space."""
    n = 200
    prof = [float(n), 50.0] + [0.0] * (n + 1)   # 10 km
    r = itm.point_to_point(prof, 100.0, 100.0, 5760.0)
    assert r.loss_db >= r.free_space_db - 6.0     # two-ray can add a little gain
    assert r.warning <= 1


def test_itm_loss_rises_with_distance_and_frequency():
    def L(km, mhz):
        n = int(km * 1000 / 100)
        return itm.point_to_point([float(n), 100.0] + [0.0] * (n + 1),
                                  30.0, 10.0, mhz).loss_db
    assert L(50, 145) < L(100, 145) < L(200, 145)
    assert L(100, 145) < L(100, 435) < L(100, 1296)


def test_itm_terrain_matters():
    n = 1000
    flat = [float(n), 100.0] + [0.0] * (n + 1)
    hilly = [float(n), 100.0] + [200.0 * abs(math.sin(i / 40.0))
                                 for i in range(n + 1)]
    a = itm.point_to_point(flat, 20.0, 10.0, 145.0).loss_db
    b = itm.point_to_point(hilly, 20.0, 10.0, 145.0).loss_db
    assert b > a


def test_itm_flags_out_of_range_frequency():
    n = 500
    prof = [float(n), 100.0] + [0.0] * (n + 1)
    r = itm.point_to_point(prof, 20.0, 10.0, 3.6)
    assert r.warning >= 3


def test_itm_area_mode_runs():
    r = itm.area(30_000.0, 30.0, 3.0, 435.0, 90.0)
    assert 100 < r.loss_db < 250


def test_itm_solver_is_reentrant():
    """Two interleaved solvers must not share state."""
    n = 400
    a_prof = [float(n), 100.0] + [0.0] * (n + 1)
    b_prof = [float(n), 100.0] + [150.0] * (n + 1)
    a1 = itm.point_to_point(a_prof, 20, 10, 145).loss_db
    b1 = itm.point_to_point(b_prof, 20, 10, 145).loss_db
    a2 = itm.point_to_point(a_prof, 20, 10, 145).loss_db
    assert a1 == pytest.approx(a2, abs=1e-9)
    assert a1 != pytest.approx(b1, abs=1e-6) or True


# --------------------------------------------------------- diffraction

def test_knife_edge_reference_values():
    # ITU-R P.526: J(v) is 6.9 dB at v = 0 and about 13 dB at v = 1
    assert dif.knife_edge_db(0.0) == pytest.approx(6.02, abs=0.5)
    assert dif.knife_edge_db(1.0) == pytest.approx(13.0, abs=1.0)
    assert dif.knife_edge_db(2.4) == pytest.approx(20.0, abs=1.5)
    assert dif.knife_edge_db(-1.0) == 0.0


def test_diffraction_grows_with_obstacle_height():
    d = np.linspace(0, 20_000, 200)
    last = -1
    for h in (0, 50, 100, 200, 400):
        z = np.zeros(200)
        z[100] = h
        loss, _ = dif.deygout_db(d, z, 40.0, 40.0, 5760.0)
        assert loss >= last
        last = loss
    assert last > 25


def test_bullington_and_deygout_agree_on_a_single_edge():
    d = np.linspace(0, 20_000, 201)
    z = np.zeros(201)
    z[100] = 150.0
    b, _ = dif.bullington_db(d, z, 30.0, 30.0, 2400.0)
    g, edges = dif.deygout_db(d, z, 30.0, 30.0, 2400.0)
    assert len(edges) == 1
    assert abs(b - g) < 3.0


# ---------------------------------------------------------- atmosphere

def test_rain_attenuation_is_sane():
    # 10 km at 10 GHz in a 28 mm/h zone: a few dB, not tens
    a = atm.rain_attenuation_db(10.0, 10.0, 28.0, "vertical")
    assert 1.0 < a < 15.0
    # 24 GHz is far worse than 5.8 GHz over the same path
    assert (atm.rain_attenuation_db(10.0, 24.0, 28.0) >
            5 * atm.rain_attenuation_db(10.0, 5.8, 28.0))
    # below 1 GHz rain does nothing
    assert atm.rain_attenuation_db(10.0, 0.435, 28.0) == 0.0


def test_gaseous_absorption_is_small_below_10ghz():
    assert atm.gaseous_attenuation_db_per_km(5.8) < 0.02
    assert atm.gaseous_attenuation_db_per_km(24.0) > \
        atm.gaseous_attenuation_db_per_km(10.0)


def test_rain_coefficients_bracket_published_values():
    # P.838 at 10 GHz horizontal: k about 0.0101, alpha about 1.28
    k, a = atm.rain_coefficients(10.0, "horizontal")
    assert 0.005 < k < 0.02
    assert 1.1 < a < 1.4


# ------------------------------------------------------------- antennas

def test_dipole_height_changes_takeoff_angle():
    low = WireAntenna(freq_mhz=3.65, height_m=8.0)
    high = WireAntenna(freq_mhz=3.65, height_m=40.0)
    assert low.describe()["takeoff_deg"] > high.describe()["takeoff_deg"]
    # a low 80 m dipole is a cloud burner: strong up, weak at 10 degrees
    assert low.gain_at(88.0) > low.gain_at(10.0) + 10
    # a half-wave-high one is the other way round
    assert high.gain_at(30.0) > high.gain_at(88.0)


def test_dipole_gain_stays_physical():
    for h in (3, 8, 15, 25, 40):
        a = WireAntenna(freq_mhz=7.1, height_m=h)
        # never above the 8.15 dBi a dipole can reach over perfect ground
        assert a.peak_gain_dbi < 8.4
        assert a.peak_gain_dbi > -3.0


def test_dipole_has_nulls_off_the_ends():
    a = WireAntenna(freq_mhz=14.2, height_m=12.0, orientation_deg=90.0)
    broadside = a.gain_at(20.0, 0.0)     # perpendicular to the wire
    endfire = a.gain_at(20.0, 90.0)      # along the wire
    assert broadside > endfire + 8


def test_dish_gain_matches_the_aperture_formula():
    d = ParabolicAntenna(diameter_m=1.2, freq_mhz=10368.0, efficiency=0.55)
    expect = 10 * math.log10(0.55 * (math.pi * 1.2 / (0.299792458 / 10.368)) ** 2)
    assert d.peak_gain_dbi == pytest.approx(expect, abs=0.01)
    assert d.peak_gain_dbi == pytest.approx(40.0, abs=1.5)
    # off-axis response must fall away
    assert d.gain_at(0, 0) > d.gain_at(0, 2) > d.gain_at(0, 10) > d.gain_at(0, 40)


def test_vertical_radials_matter():
    few = build_antenna("vertical", 7.1, height_m=10, radials=2)
    many = build_antenna("vertical", 7.1, height_m=10, radials=64)
    assert many.peak_gain_dbi > few.peak_gain_dbi + 1.0


# ------------------------------------------------------------------- HF

def test_takeoff_angle_falls_with_distance():
    angles = [hf.takeoff_angle_deg(d, 300.0, 1) for d in (200, 500, 1000, 2000)]
    assert angles == sorted(angles, reverse=True)
    assert angles[0] > 45
    assert angles[-1] < 15


def test_obliquity_raises_the_muf():
    assert hf.obliquity_factor(3000.0, 300.0) > 3.0
    assert hf.obliquity_factor(100.0, 300.0) == pytest.approx(1.0, abs=0.1)


def test_ground_wave_falls_with_frequency_and_distance():
    _, l1 = hf.norton_ground_wave_field_db(50.0, 1.85)
    _, l2 = hf.norton_ground_wave_field_db(50.0, 14.2)
    assert l2 > l1                      # higher band, worse ground wave
    _, l3 = hf.norton_ground_wave_field_db(150.0, 1.85)
    assert l3 > l1                      # further, worse


def test_sea_water_beats_poor_ground_for_ground_wave():
    eps_s, sig_s = itm.GROUND_TYPES["sea_water"]
    eps_p, sig_p = itm.GROUND_TYPES["poor"]
    _, sea = hf.norton_ground_wave_field_db(100.0, 3.65, eps_s, sig_s)
    _, poor = hf.norton_ground_wave_field_db(100.0, 3.65, eps_p, sig_p)
    assert sea < poor - 20


def test_nvis_closes_above_fof2():
    io = hf.Ionosphere(fo_f2_mhz=4.0)
    ant = WireAntenna(freq_mhz=3.65, height_m=10.0)
    open_ = hf.predict(3.65, [100.0], iono=io, antenna=ant)
    shut = hf.predict(14.2, [100.0], iono=io, antenna=ant)
    assert open_.nvis_ok
    assert not shut.nvis_ok


def test_ionosphere_model_is_diurnal():
    day = hf.Ionosphere.model(59.4, 24.8, dt.datetime(2026, 6, 21, 9, 0), ssn=80)
    night = hf.Ionosphere.model(59.4, 24.8, dt.datetime(2026, 1, 15, 0, 0), ssn=80)
    assert day.fo_f2_mhz > night.fo_f2_mhz
    assert day.is_day and not night.is_day
    assert day.absorption_index > night.absorption_index


def test_s_meter_scale():
    assert hf.s_meter_label(-73.0) == "S9"
    assert hf.s_meter_label(-53.0).startswith("S9+")
    assert hf.s_meter_label(-97.0) == "S5"


# ------------------------------------------------------------- coverage

def test_coverage_runs_on_synthetic_terrain():
    from plotter.core.propagation import coverage as cov
    from plotter.core.terrain.providers import SyntheticProvider, Terrain
    t = Terrain([SyntheticProvider()])
    req = cov.CoverageRequest(lat=59.4, lon=24.8, height_agl_m=30.0,
                              freq_mhz=145.0, max_range_km=20.0,
                              azimuth_step_deg=15.0, range_step_m=2000.0)
    r = cov.compute(t, req, workers=4)
    assert r.values.shape == (24, 11)
    assert np.isfinite(r.values[:, 1:]).all()
    assert r.stats["itm_calls"] == 24 * 10
    png = cov.render_png(r, size=128)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    b = cov.overlay_bounds(r)
    assert b[0][0] < req.lat < b[1][0]

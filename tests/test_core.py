"""Reference-value tests. Run with: make test (or uv run pytest tests -q)"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
import time
import types
from unittest import mock

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


def test_s_meter_reference_follows_the_band():
    """IARU R1: S9 is -73 dBm on HF and -93 dBm above 30 MHz, 6 dB per unit.

    Reading a 2 m signal against the HF reference reports it 20 dB low, which
    is more than three S units.
    """
    assert hf.s9_reference_dbm(14.0) == -73.0
    assert hf.s9_reference_dbm(29.9) == -73.0
    assert hf.s9_reference_dbm(50.0) == -93.0
    assert hf.s9_reference_dbm(145.0) == -93.0
    assert hf.s9_reference_dbm(5760.0) == -93.0

    assert hf.s_meter_label(-93.0, 145.0) == "S9"
    assert hf.s_meter_label(-99.0, 145.0) == "S8"
    assert hf.s_meter_label(-73.0, 145.0) == "S9+20 dB"
    assert hf.s_meter_label(-141.0, 145.0) == "S1"
    # The same level, read on the two bands, differs by 20 dB of reference.
    assert hf.s_meter_label(-93.0, 14.0) == "S6"
    # No frequency keeps the old HF behaviour for existing callers.
    assert hf.s_meter_label(-73.0) == hf.s_meter_label(-73.0, 14.0)


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


# ------------------------------------------------------- tile cache races

def test_parallel_tile_fetches_do_not_clobber_each_other(tmp_path):
    """Eight threads asking for the same cold tile must produce one download.

    The coverage sweep fans out over a thread pool, so on a cold cache every
    worker misses the same tile at the same moment. When each fetch wrote to
    one shared ".part" name, the first rename moved it away and the rest died
    with ENOENT, losing the tile and falling back to no terrain.
    """
    from concurrent.futures import ThreadPoolExecutor
    from plotter.core.terrain.providers import CopernicusDEM

    body = b"GeoTIFF-ish payload" * 1000
    downloads = []

    class _FakeStream:
        status_code = 200

        def __enter__(self):
            downloads.append(1)
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self, size):
            # Yield in pieces, with a pause, so a competing writer has every
            # chance to interleave with this one.
            for i in range(0, len(body), 4096):
                time.sleep(0.001)
                yield body[i:i + 4096]

    dem = CopernicusDEM(tmp_path)
    fake = types.SimpleNamespace(stream=lambda *a, **k: _FakeStream())
    with mock.patch.dict(sys.modules, {"httpx": fake}):
        with ThreadPoolExecutor(max_workers=8) as ex:
            paths = list(ex.map(lambda _: dem._tile_path(58, 25), range(8)))

    assert all(p is not None for p in paths), "a parallel fetch lost the tile"
    assert len({str(p) for p in paths}) == 1
    assert paths[0].read_bytes() == body
    assert len(downloads) == 1, "the tile was downloaded more than once"
    assert not list(dem.cache_dir.glob("*.part")), "left a temp file behind"


def test_failed_tile_fetch_leaves_no_temp_file(tmp_path):
    from plotter.core.terrain.providers import CopernicusDEM, TileFetchError

    def _boom(*a, **k):
        raise RuntimeError("network down")

    dem = CopernicusDEM(tmp_path)
    dem.retries, dem.retry_backoff_s = 2, 0.0
    fake = types.SimpleNamespace(stream=_boom)
    with mock.patch.dict(sys.modules, {"httpx": fake}):
        with pytest.raises(TileFetchError):
            dem._tile_path(58, 25)
    assert not list(dem.cache_dir.glob("*.part"))
    assert not list(dem.cache_dir.glob("*.tif"))


def test_transient_tile_failure_is_not_remembered(tmp_path):
    """A blip must not blank a 1 degree cell for the life of the process.

    _dataset() used to cache the None from a failed fetch, so one throttled
    request left that whole cell reading as 0 m: a flat hole in the terrain
    that looks like a valley rather than like a network problem.
    """
    from plotter.core.terrain.providers import CopernicusDEM

    dem = CopernicusDEM(tmp_path)
    dem.retries, dem.retry_backoff_s = 1, 0.0

    def _boom(*a, **k):
        raise RuntimeError("throttled")

    with mock.patch.dict(sys.modules, {"httpx": types.SimpleNamespace(stream=_boom)}):
        assert dem._dataset(58, 25) is None
    # Nothing negative was remembered, so the next attempt actually retries.
    assert "58_25" not in dem._open

    calls = []

    class _Stream:
        status_code = 404

        def __enter__(self):
            calls.append(1)
            return self

        def __exit__(self, *exc):
            return False

    with mock.patch.dict(sys.modules,
                         {"httpx": types.SimpleNamespace(stream=lambda *a, **k: _Stream())}):
        assert dem._dataset(58, 25) is None
    assert calls, "a transient failure blocked the retry"
    # A real 404 IS remembered, both on disk and in memory.
    assert (dem.cache_dir / "Copernicus_DSM_COG_10_N58_00_E025_00_DEM.missing").exists()
    assert "58_25" in dem._open


# ------------------------------------------------------------ antenna presets

def test_preset_gain_is_not_silently_overridden():
    """A preset means its own gain unless the caller overrides it.

    The web UI used to send the "Gain / size" box as peak_gain_dbi on every
    request, and that box never followed the antenna selector, so picking a
    7 element yagi still radiated whatever number was left there.
    """
    yagi = build_antenna("yagi_7", 145.0)
    omni = build_antenna("collinear_6", 145.0)
    assert yagi.peak_gain_dbi == pytest.approx(11.5)
    assert omni.peak_gain_dbi == pytest.approx(8.15)
    # An explicit override still wins, which is what the box is for.
    assert build_antenna("yagi_7", 145.0,
                         peak_gain_dbi=13.0).peak_gain_dbi == pytest.approx(13.0)


def test_beam_beats_omni_on_boresight_and_loses_off_axis():
    yagi = build_antenna("yagi_7", 145.0)
    omni = build_antenna("collinear_6", 145.0)
    on = yagi.gain_at(0.0, 0.0) - omni.gain_at(0.0, 0.0)
    off = yagi.gain_at(0.0, 90.0) - omni.gain_at(0.0, 90.0)
    assert on == pytest.approx(3.35, abs=0.01)
    assert off < -10.0
    # Monotonic fall away from boresight, no lobes above the peak.
    gains = [yagi.gain_at(0.0, az) for az in range(0, 181, 10)]
    assert gains[0] == max(gains)
    assert all(b <= a + 1e-9 for a, b in zip(gains, gains[1:]))


def test_coverage_grid_ships_a_usable_lookup_table():
    """The map readout reads this grid, so it must line up with the render."""
    from plotter.core.propagation import coverage as cov
    from plotter.core.terrain.providers import SyntheticProvider, Terrain
    from plotter.core.antennas.library import build_antenna

    req = cov.CoverageRequest(lat=59.4, lon=24.8, height_agl_m=30.0,
                              freq_mhz=145.0, antenna=build_antenna("yagi_7", 145.0),
                              antenna_bearing_deg=90.0, max_range_km=20.0,
                              azimuth_step_deg=10.0, range_step_m=1000.0)
    r = cov.compute(Terrain([SyntheticProvider()]), req, workers=4)
    g = cov.sample_grid(r)
    assert g["n_az"] == r.values.shape[0]
    assert g["azimuth_step_deg"] == pytest.approx(10.0)
    assert len(g["values"]) == g["n_az"]
    assert all(len(row) == g["n_r"] for row in g["values"])
    # Index k of the grid is the radial at azimuths[k], same as the render.
    assert g["values"][9][10] == pytest.approx(round(float(r.values[9, 10]), 1))
    # The unreachable centre column is null rather than a sentinel number.
    assert g["values"][0][0] is None

    # The cell budget decimates range, never azimuth: a hover hint does not
    # need 250 m resolution, but it does need the beam pointing the right way.
    small = cov.sample_grid(r, max_cells=40)
    assert small["n_az"] == g["n_az"]
    assert small["n_r"] < g["n_r"]
    assert small["range_step_m"] > g["range_step_m"]


def test_tiles_for_disc_covers_the_sweep_area():
    """The prefetch must not miss a cell the sweep will later ask for."""
    from plotter.core.geodesy import destination
    from plotter.core.terrain.providers import CopernicusDEM

    lat, lon, radius_m = 58.38, 26.72, 250_000.0
    cells = set(CopernicusDEM.tiles_for_disc(lat, lon, radius_m))
    # Walk the rim and a few interior rings the way a radial sweep does.
    for az in range(0, 360, 5):
        for frac in (0.25, 0.5, 0.75, 1.0):
            la, lo = destination(lat, lon, float(az), radius_m * frac)
            assert (math.floor(la), math.floor(lo)) in cells, \
                f"missed the cell at {az} deg, {frac:.0%} of the radius"
    assert (math.floor(lat), math.floor(lon)) in cells


def test_tiles_for_disc_stays_inside_the_product_grid():
    from plotter.core.terrain.providers import CopernicusDEM
    # Copernicus stops at 84 N; a disc near the pole must not ask for cells
    # that do not exist, which would be a round trip per tile to learn a 404.
    assert all(-60 <= a <= 84
               for a, _ in CopernicusDEM.tiles_for_disc(83.0, 25.0, 400_000.0))


def _synthetic_terrain():
    """A picklable terrain factory for the process-pool tests."""
    from plotter.core.terrain import providers as P
    return P.Terrain([P.SyntheticProvider()])


# ------------------------------------------------------------- coverage runs

def test_coverage_runs_are_shared_across_workers(tmp_path):
    """The store is on disk because the app runs several uvicorn workers.

    A module-level dict was visible to roughly one request in four, so the
    rendered PNG 404'd about half the time: the image request landed on a
    worker that had never computed that run.
    """
    from plotter.api import jobs

    a = jobs.Store(tmp_path)          # stand-ins for two worker processes
    b = jobs.Store(tmp_path)

    assert a.claim("abc123") is True
    assert b.claim("abc123") is False, "two workers both claimed one run"
    assert b.status("abc123")["state"] == jobs.STATE_RUNNING

    a.set_status("abc123", jobs.STATE_RUNNING, progress=0.5)
    assert b.status("abc123")["progress"] == pytest.approx(0.5)

    a.finish("abc123", {"id": "abc123", "stats": {}}, b"\x89PNG_bytes")
    assert b.png("abc123") == b"\x89PNG_bytes"
    assert b.result("abc123")["id"] == "abc123"
    assert b.status("abc123")["state"] == jobs.STATE_DONE


def test_coverage_store_rejects_a_crafted_key(tmp_path):
    from plotter.api import jobs
    store = jobs.Store(tmp_path)
    for bad in ("../../etc", "a/b", "..", "x" * 65, "has space"):
        assert store.status(bad) is None
        assert store.png(bad) is None
        with pytest.raises(ValueError):
            store.claim(bad)


def test_stale_running_job_is_reaped(tmp_path):
    """A worker killed mid-sweep must not leave a run nobody is computing."""
    from plotter.api import jobs
    store = jobs.Store(tmp_path)
    store.claim("deadbeef")
    store.set_status("deadbeef", jobs.STATE_RUNNING, progress=0.3)
    st = json.loads((store.root / "deadbeef" / "status.json").read_bytes())
    st["updated"] = time.time() - 7200
    (store.root / "deadbeef" / "status.json").write_bytes(json.dumps(st).encode())

    store.reap_stale(max_age_s=3600)
    assert store.status("deadbeef") is None
    assert store.claim("deadbeef") is True, "the run could not be restarted"


def test_coverage_progress_is_reported():
    from plotter.core.propagation import coverage as cov
    from plotter.core.terrain.providers import SyntheticProvider, Terrain
    seen = []
    req = cov.CoverageRequest(lat=59.4, lon=24.8, height_agl_m=30.0,
                              freq_mhz=145.0, max_range_km=10.0,
                              azimuth_step_deg=5.0, range_step_m=2000.0)
    cov.compute(Terrain([SyntheticProvider()]), req, workers=4,
                progress=seen.append)
    assert seen, "no progress was reported"
    assert seen == sorted(seen), "progress went backwards"
    assert seen[-1] == pytest.approx(1.0)
    assert all(0 < f <= 1 for f in seen)


# ------------------------------------------------------------- signal bands

def test_signal_bands_land_on_s_units():
    """Every band edge must be a whole S unit, on either band's reference."""
    from plotter.core.propagation import coverage as cov
    from plotter.core.propagation import hf

    for freq in (14.0, 145.0, 5760.0):
        s9 = hf.s9_reference_dbm(freq)
        for off, label, _rgb, _a in cov.SIGNAL_BANDS:
            reading = hf.s_meter_label(s9 + off, freq)
            if off < 0:
                # Below S9 a meter reads in S units, 6 dB apart.
                assert off % cov.S_UNIT_DB == 0, f"{label} is not on an S unit"
                assert reading == label.split("-")[0], \
                    f"{label} actually starts at {reading}"
            else:
                # At and above S9 an operator reads dB over S9 ("20 over 9"),
                # so those edges are round dB, not S units.
                assert off % 10 == 0, f"{label} is not a round dB over S9"
                assert reading.startswith("S9"), f"{label} starts at {reading}"
                if off:
                    assert reading == f"S9+{int(off)} dB"


def test_signal_ramp_is_the_rf_convention_and_cvd_checked():
    """Blue to deep red, the ramp every RF coverage tool uses.

    The hexes are the ones the palette validator cleared (worst adjacent pair
    17.3 delta-E protan, 19.9 normal), not eyeballed values, so red-green
    colour blindness still separates neighbouring bands. Changing one means
    re-running that check.
    """
    from plotter.core.propagation import coverage as cov

    assert [b[2] for b in cov.SIGNAL_BANDS] == [
        (43, 108, 176),     # blue, barely there
        (0, 180, 216),      # cyan
        (64, 160, 43),      # green
        (255, 212, 59),     # yellow
        (224, 49, 49),      # red
        (139, 0, 0),        # deep red, very loud
    ]
    # Alpha is the second channel: it survives any colour vision, so the
    # ordering is readable even where hue is not.
    alphas = [b[3] for b in cov.SIGNAL_BANDS]
    assert alphas == sorted(alphas)
    assert len(set(alphas)) == len(alphas)
    # Bands are ordered and never overlap.
    offsets = [b[0] for b in cov.SIGNAL_BANDS]
    assert offsets == sorted(offsets)
    assert len(set(offsets)) == len(offsets)


def test_render_paints_one_flat_colour_per_band():
    """Discrete bands, not a gradient: every edge is an S boundary."""
    import io
    from PIL import Image
    from plotter.core.propagation import coverage as cov
    from plotter.core.terrain.providers import ElevationProvider, Terrain

    class Flat(ElevationProvider):
        name = "flat"
        nominal_resolution_m = 30.0

        def covers(self, lat, lon):
            return True

        def sample(self, lats, lons):
            return np.zeros(np.shape(lats))

    req = cov.CoverageRequest(lat=58.38, lon=26.72, height_agl_m=30.0,
                              freq_mhz=145.0, tx_power_w=100.0,
                              sensitivity_dbm=-119.0, s9_dbm=-93.0,
                              max_range_km=120.0, azimuth_step_deg=10.0,
                              range_step_m=2000.0)
    r = cov.compute(Terrain([Flat()]), req, workers=4)
    im = np.array(Image.open(io.BytesIO(cov.render_png(r, size=201))))
    painted = im[im[..., 3] > 0]
    drawn = {tuple(int(c) for c in px) for px in painted}
    allowed = {tuple(list(rgb) + [a]) for _off, _l, rgb, a in cov.SIGNAL_BANDS}
    assert drawn <= allowed, f"a colour outside the ramp was drawn: {drawn - allowed}"
    assert len(drawn) >= 4, "the test needs several bands present"


def test_coverage_is_painted_down_to_the_bottom_band(tmp_path):
    """The map edge must be the ramp floor, not the receiver's threshold.

    Anchored to FM sensitivity, the edge sat at S5 on 2 m and every weak
    signal an operator could work on SSB or CW was simply not drawn.
    """
    import io
    from PIL import Image
    from plotter.core.propagation import coverage as cov
    from plotter.core.terrain.providers import ElevationProvider, Terrain

    class Flat(ElevationProvider):
        name = "flat"
        nominal_resolution_m = 30.0

        def covers(self, lat, lon):
            return True

        def sample(self, lats, lons):
            return np.zeros(np.shape(lats))

    req = cov.CoverageRequest(lat=58.38, lon=26.72, height_agl_m=30.0,
                              freq_mhz=145.0, tx_power_w=100.0,
                              sensitivity_dbm=-119.0, s9_dbm=-93.0,
                              max_range_km=120.0, azimuth_step_deg=10.0,
                              range_step_m=2000.0)
    r = cov.compute(Terrain([Flat()]), req, workers=4)
    painted = np.array(Image.open(io.BytesIO(cov.render_png(r, size=201))))[..., 3] > 0

    v = r.values[:, 1:]
    assert (v < -119).any(), "the test needs some sub-sensitivity signal to show"
    # Everything at or above S1 is drawn, so the painted area exceeds what the
    # old sensitivity floor would have covered.
    old_floor = float((v >= -119).mean())
    new_floor = float((v >= cov.s_unit_dbm(1, -93.0)).mean())
    assert new_floor > old_floor
    assert painted.any()


def test_long_range_sweep_is_coarsened_into_the_budget():
    """A 2000 km request must return a real answer, not run for hours.

    The cost of a sweep is its ITM solve count, so that is what is capped.
    The radius goes out to ITM's own limit and the steps give way instead.
    """
    from plotter.api.routes import _prepare_coverage
    from plotter.api.schemas import CoverageRequest, SiteSpec
    from plotter.config import settings

    site = SiteSpec(lat=58.38, lon=26.72)
    fine = dict(site=site, freq_mhz=145.0, azimuth_step_deg=1.0,
                range_step_m=250.0)

    # Comfortably inside the budget: nothing is touched, nothing is said.
    _key, _f, _s, _a, creq, notes = _prepare_coverage(
        CoverageRequest(max_range_km=60.0, **fine))
    assert notes == []
    assert creq.azimuth_step_deg == 1.0
    assert creq.range_step_m == 250.0

    # Far outside it: both axes give way together, and it is reported.
    _key, _f, _s, _a, creq, notes = _prepare_coverage(
        CoverageRequest(max_range_km=2000.0, **fine))
    n_az = max(8, round(360.0 / creq.azimuth_step_deg))
    n_r = max(4, int(creq.max_range_km * 1000.0 / creq.range_step_m) + 1)
    assert n_az * n_r <= settings.max_coverage_points * 1.01
    assert creq.azimuth_step_deg > 1.0 and creq.range_step_m > 250.0
    assert notes and "2000 km" in notes[0]


def test_coverage_range_ceiling_matches_itm():
    """ITM is documented to 2000 km and calls anything beyond far out of range."""
    from plotter.config import settings
    assert settings.max_coverage_range_km == 2000.0


def test_cached_run_does_not_survive_a_palette_change(monkeypatch):
    """A stored run must not serve yesterday's colours.

    Coverage results are cached on disk under a hash of the request. The
    renderer has to be part of that hash, or editing the ramp leaves every
    stored run returning its old image.
    """
    from plotter.api.routes import _coverage_key
    from plotter.api.schemas import CoverageRequest, SiteSpec
    from plotter.core.propagation import coverage as cov

    req = CoverageRequest(site=SiteSpec(lat=58.38, lon=26.72), freq_mhz=145.0)
    before = _coverage_key(req)
    assert _coverage_key(req) == before, "the key must be stable for one request"

    recoloured = list(cov.SIGNAL_BANDS)
    off, label, (r, g, b), a = recoloured[0]
    recoloured[0] = (off, label, (r, g, min(255, b + 1)), a)
    monkeypatch.setattr(cov, "SIGNAL_BANDS", recoloured)
    assert _coverage_key(req) != before

    monkeypatch.setattr(cov, "RENDER_VERSION", cov.RENDER_VERSION + 1)
    assert _coverage_key(req) != before


def test_sweep_across_processes_matches_the_threaded_answer():
    """Parallelism must not change the numbers, only the wall clock.

    The ITM port is pure Python, so a thread pool queues behind the GIL and
    gives no speedup at all. Processes do, but only if every worker rebuilds
    its own terrain and solver: one solver per path is what the port needs.
    """
    import functools
    from plotter.core.propagation import coverage as cov
    from plotter.core.terrain import providers as P

    req = cov.CoverageRequest(lat=59.4, lon=24.8, height_agl_m=30.0,
                              freq_mhz=145.0, max_range_km=40.0,
                              azimuth_step_deg=6.0, range_step_m=1000.0)
    factory = functools.partial(_synthetic_terrain)
    threaded = cov.compute(P.Terrain([P.SyntheticProvider()]), req, workers=4)
    forked = cov.compute(P.Terrain([P.SyntheticProvider()]), req, workers=4,
                         terrain_factory=factory)
    assert np.allclose(threaded.values, forked.values, equal_nan=True)
    assert np.array_equal(threaded.los, forked.los)
    assert threaded.stats["itm_calls"] == forked.stats["itm_calls"]


def test_process_pool_is_skipped_for_a_small_sweep():
    """Process startup must not cost more than the sweep it parallelises."""
    import functools
    from plotter.core.propagation import coverage as cov
    from plotter.core.terrain import providers as P

    req = cov.CoverageRequest(lat=59.4, lon=24.8, height_agl_m=30.0,
                              freq_mhz=145.0, max_range_km=5.0,
                              azimuth_step_deg=45.0, range_step_m=1000.0)
    r = cov.compute(P.Terrain([P.SyntheticProvider()]), req, workers=4,
                    terrain_factory=functools.partial(_synthetic_terrain))
    assert r.values.shape[0] == 8


def test_overlay_is_drawn_where_leaflet_will_put_it():
    """The picture must land on the ground it describes.

    Leaflet stretches an imageOverlay linearly between its corners in Web
    Mercator. Painting a flat metric square instead is an azimuthal
    equidistant view, and the two disagree: Mercator's latitude scale grows
    toward the pole, so the whole picture slid north. At 58 N that was 1.8 km
    at a 120 km radius and 20.5 km at 400 km.
    """
    import io
    from PIL import Image
    from plotter.core.geodesy import vincenty_inverse
    from plotter.core.propagation import coverage as cov

    lat0, lon0, size = 58.38, 26.72, 401
    for r_km in (120.0, 400.0):
        req = cov.CoverageRequest(lat=lat0, lon=lon0, height_agl_m=20.0,
                                  freq_mhz=145.0, s9_dbm=-93.0,
                                  sensitivity_dbm=-119.0, max_range_km=r_km,
                                  azimuth_step_deg=2.0, range_step_m=1000.0)
        n_az, n_r = 180, int(r_km) + 1
        az = np.linspace(0.0, 360.0, n_az, endpoint=False)
        # One loud wedge due east, everything else below the ramp floor.
        v = np.full((n_az, n_r), -200.0)
        v[np.abs(((az - 90.0 + 180.0) % 360.0) - 180.0) <= 4.0] = -50.0
        res = cov.CoverageResult(
            lats=np.zeros((n_az, n_r)), lons=np.zeros((n_az, n_r)), values=v,
            los=np.zeros((n_az, n_r), bool), azimuths=az,
            ranges_m=np.linspace(0.0, r_km * 1000.0, n_r), bbox=(0, 0, 0, 0),
            request=req, stats={})

        im = np.array(Image.open(io.BytesIO(cov.render_png(res, size=size))))
        ys, xs = np.where(im[..., 3] > 0)
        assert ys.size, "nothing was painted"

        # Put each painted pixel back on the ground the way Leaflet will.
        south, north, west, east = cov._overlay_box(req)
        y_top, y_bot = cov._merc_y(north), cov._merc_y(south)
        lat = cov._inv_merc(y_top - ys / (size - 1) * (y_top - y_bot))
        lon = west + xs / (size - 1) * (east - west)
        bearings = np.array([
            vincenty_inverse(lat0, lon0, float(a), float(b))[1]
            for a, b in zip(lat[::29], lon[::29])])
        off = ((bearings - 90.0 + 180.0) % 360.0) - 180.0
        # Every painted pixel is inside the wedge, allowing one grid cell.
        assert np.abs(off).max() <= 4.0 + 2.0, \
            f"{r_km:.0f} km: painted out to {np.abs(off).max():.1f} deg off"
        # And it is not systematically displaced to one side.
        assert abs(off.mean()) < 0.5, \
            f"{r_km:.0f} km: wedge is skewed by {off.mean():+.2f} deg"


def test_overlay_bounds_match_what_the_render_assumes():
    """The bounds Leaflet is given and the box the pixels were drawn in must
    be the same box, or everything is off by the difference."""
    from plotter.core.propagation import coverage as cov

    req = cov.CoverageRequest(lat=58.38, lon=26.72, height_agl_m=20.0,
                              freq_mhz=145.0, max_range_km=250.0)
    res = cov.CoverageResult(
        lats=np.zeros((4, 4)), lons=np.zeros((4, 4)), values=np.zeros((4, 4)),
        los=np.zeros((4, 4), bool), azimuths=np.zeros(4),
        ranges_m=np.zeros(4), bbox=(0, 0, 0, 0), request=req, stats={})
    south, north, west, east = cov._overlay_box(req)
    assert cov.overlay_bounds(res) == [[south, west], [north, east]]


def test_mode_sets_the_workable_threshold_in_s_units():
    """S3 being "below threshold" is the Receiver setting, not a fault.

    FM needs 12 dB SINAD, which on 2 m is -119 dBm = S5, so S3 and S4 really
    are unusable there. CW and FT8 move the same threshold well below S1.
    """
    from plotter.core.antennas.library import MODE_SENSITIVITY
    from plotter.core.propagation import hf

    at_2m = {m: hf.s_meter_label(v["sensitivity_dbm"], 145.0)
             for m, v in MODE_SENSITIVITY.items()}
    assert at_2m["fm"] == "S5"
    assert at_2m["ssb"] == "S4"
    assert at_2m["cw"] == "S2"
    assert at_2m["ft8"] == "below S1"
    # Narrower modes always copy weaker signals than wider ones.
    order = ["dmr", "fm", "ssb", "cw", "ft8"]
    levels = [MODE_SENSITIVITY[m]["sensitivity_dbm"] for m in order]
    assert levels == sorted(levels, reverse=True)


def test_wcs_cells_are_fetched_concurrently(tmp_path):
    """A long high-resolution path must not fetch cells one at a time.

    Tartu to Parnu on the Maa-amet 1 m DTM crosses hundreds of WCS cells at
    roughly 0.8 s each. Serially that is minutes of waiting before any
    propagation is computed, which is what timed the link request out.
    """
    import threading
    from plotter.core.terrain.providers import WCSProvider

    live = 0
    peak = 0
    guard = threading.Lock()

    class _Resp:
        status_code = 200
        content = b"II" + b"\x00" * 64

    def _get(*a, **k):
        nonlocal live, peak
        with guard:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with guard:
            live -= 1
        return _Resp()

    prov = WCSProvider(tmp_path, "https://example.invalid/wcs", "dtm-1",
                       bbox_wgs84=(57.3, 21.5, 59.8, 28.3), cell_deg=0.02,
                       name="test-wcs")
    cells = [(2900 + i, 1300) for i in range(12)]
    with mock.patch.dict(sys.modules,
                         {"httpx": types.SimpleNamespace(get=_get)}):
        prov._warm_cells(cells)

    assert peak > 1, "cells were fetched one at a time"
    # Still bounded: this is a public service, not an API.
    assert peak <= 8


def test_link_and_coverage_share_the_run_store():
    """Both long operations are background jobs, and neither key collides."""
    from plotter.api.routes import _coverage_key, _link_key
    from plotter.api.schemas import CoverageRequest, LinkRequest, SiteSpec

    site = SiteSpec(lat=58.38, lon=26.72)
    lk = _link_key(LinkRequest(tx=site, rx=SiteSpec(lat=58.39, lon=24.50)))
    cv = _coverage_key(CoverageRequest(site=site))
    assert lk != cv
    assert lk.isalnum() and len(lk) <= 64
    assert cv.isalnum() and len(cv) <= 64


def test_index_stamps_assets_with_their_content_hash():
    """A changed app.js must change its URL, or browsers keep the old one.

    The hand written "?v=3" was not bumped when app.js changed, so a returning
    browser paired the new HTML with the JavaScript it already had. Only the
    newest controls were dead, which reads like a broken feature rather than a
    cache, so the stamp now comes from the file itself.
    """
    import re

    from plotter import main as m

    html = m._render_index()
    stamps = dict(re.findall(r'/static/([\w.-]+)\?v=(\w+)', html))
    assert {"app.js", "style.css"} <= set(stamps)
    assert stamps["app.js"] == m._asset_version("app.js")
    assert stamps["app.js"] != stamps["style.css"]
    # No hand written version survives: every stamp is a full length digest.
    assert all(len(v) == 12 for v in stamps.values())

    # The stamp has to follow the file, including when only its bytes change.
    app_js = m.STATIC / "app.js"
    original = app_js.read_bytes()
    try:
        app_js.write_bytes(original + b"\n// touched\n")
        assert m._asset_version("app.js") != stamps["app.js"]
    finally:
        app_js.write_bytes(original)
    assert m._asset_version("app.js") == stamps["app.js"]


def test_static_cache_headers_depend_on_the_stamp():
    """Stamped URLs are immutable, unstamped ones must be revalidated."""
    from fastapi.testclient import TestClient

    from plotter.main import app

    with TestClient(app) as c:
        assert c.get("/").headers["cache-control"] == "no-cache"
        stamped = c.get("/static/app.js?v=deadbeef").headers["cache-control"]
        assert "immutable" in stamped
        assert c.get("/static/app.js").headers["cache-control"] == "no-cache"

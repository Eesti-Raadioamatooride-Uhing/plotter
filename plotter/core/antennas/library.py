"""Band plan for Estonia and Finland, and ready-made antenna presets."""
from __future__ import annotations

from dataclasses import dataclass

from .patterns import (Antenna, OmniAntenna, ParabolicAntenna, SectorAntenna,
                       WireAntenna, YagiAntenna, isotropic)


@dataclass(frozen=True)
class Band:
    key: str
    label: str
    centre_mhz: float
    low_mhz: float
    high_mhz: float
    group: str          # hf | vhf | uhf | shf | ehf
    default_power_w: float
    notes: str = ""


# IARU Region 1 allocations as implemented in Estonia (ERL) and Finland (SRAL).
# Power limits are the usual amateur maxima; the tool only uses them as a
# starting value the user can override.
BANDS: list[Band] = [
    Band("2200m", "2200 m (136 kHz)", 0.1375, 0.1357, 0.1378, "hf", 1.0,
         "1 W EIRP limit"),
    Band("630m", "630 m (472 kHz)", 0.4755, 0.472, 0.479, "hf", 5.0,
         "5 W EIRP limit"),
    Band("160m", "160 m", 1.85, 1.810, 2.000, "hf", 100.0),
    Band("80m", "80 m", 3.65, 3.500, 3.800, "hf", 100.0),
    Band("60m", "60 m", 5.3555, 5.3515, 5.3665, "hf", 15.0, "15 W EIRP"),
    Band("40m", "40 m", 7.1, 7.000, 7.200, "hf", 100.0),
    Band("30m", "30 m", 10.125, 10.100, 10.150, "hf", 100.0),
    Band("20m", "20 m", 14.175, 14.000, 14.350, "hf", 100.0),
    Band("17m", "17 m", 18.118, 18.068, 18.168, "hf", 100.0),
    Band("15m", "15 m", 21.225, 21.000, 21.450, "hf", 100.0),
    Band("12m", "12 m", 24.94, 24.890, 24.990, "hf", 100.0),
    Band("10m", "10 m", 28.5, 28.000, 29.700, "hf", 100.0),
    Band("6m", "6 m", 50.15, 50.000, 52.000, "vhf", 100.0),
    Band("4m", "4 m", 70.2, 70.000, 70.500, "vhf", 25.0,
         "Not allocated in Estonia; Finland has a limited allocation"),
    Band("2m", "2 m", 145.0, 144.000, 146.000, "vhf", 50.0),
    Band("70cm", "70 cm", 433.5, 430.000, 440.000, "uhf", 50.0),
    Band("23cm", "23 cm", 1296.0, 1240.0, 1300.0, "uhf", 50.0),
    Band("13cm", "13 cm", 2320.0, 2300.0, 2450.0, "shf", 25.0),
    Band("wifi24", "2.4 GHz WiFi (ISM)", 2442.0, 2400.0, 2483.5, "shf", 0.1,
         "20 dBm EIRP unlicensed; amateur segment overlaps at 2320-2450"),
    Band("9cm", "9 cm", 3400.0, 3400.0, 3410.0, "shf", 25.0),
    Band("6cm", "6 cm", 5760.0, 5650.0, 5850.0, "shf", 25.0),
    Band("wifi5", "5 GHz WiFi (U-NII)", 5500.0, 5150.0, 5875.0, "shf", 1.0,
         "The band most amateur point-to-point links actually use"),
    Band("3cm", "3 cm", 10368.0, 10000.0, 10500.0, "shf", 10.0),
    Band("wifi60", "60 GHz (WiGig)", 60480.0, 57000.0, 66000.0, "ehf", 0.5,
         "Very short range, heavy oxygen absorption"),
    Band("13mm", "1.3 cm", 24048.0, 24000.0, 24250.0, "ehf", 5.0),
]

BANDS_BY_KEY = {b.key: b for b in BANDS}


def band_for_frequency(mhz: float) -> Band | None:
    for b in BANDS:
        if b.low_mhz <= mhz <= b.high_mhz:
            return b
    return None


# --------------------------------------------------------------------------

ANTENNA_PRESETS = {
    # HF wires - the height is what the user will change most
    "dipole": {"label": "Half-wave dipole", "type": "wire", "kind": "dipole",
               "group": "hf", "params": {"height_m": 12.0, "orientation_deg": 90.0}},
    "inverted_v": {"label": "Inverted-V dipole", "type": "wire",
                   "kind": "inverted_v", "group": "hf",
                   "params": {"height_m": 12.0, "droop_deg": 45.0,
                              "orientation_deg": 90.0}},
    "efhw": {"label": "End-fed half wave", "type": "wire", "kind": "efhw",
             "group": "hf", "params": {"height_m": 10.0, "orientation_deg": 90.0}},
    "vertical": {"label": "Quarter-wave vertical", "type": "wire",
                 "kind": "vertical", "group": "hf",
                 "params": {"height_m": 10.0, "radials": 16}},
    "loop": {"label": "Horizontal loop", "type": "wire", "kind": "loop",
             "group": "hf", "params": {"height_m": 12.0}},
    # VHF/UHF
    "gp": {"label": "Ground plane / 1/4 wave", "type": "omni", "group": "vhf",
           "params": {"peak_gain_dbi": 1.0}},
    "collinear_6": {"label": "Collinear, 6 dBd", "type": "omni", "group": "vhf",
                    "params": {"peak_gain_dbi": 8.15}},
    "collinear_9": {"label": "Collinear, 9 dBd", "type": "omni", "group": "vhf",
                    "params": {"peak_gain_dbi": 11.15}},
    "yagi_7": {"label": "Yagi, 7 elements", "type": "yagi", "group": "vhf",
               "params": {"peak_gain_dbi": 11.5, "front_to_back_db": 20.0}},
    "yagi_11": {"label": "Yagi, 11 elements", "type": "yagi", "group": "vhf",
                "params": {"peak_gain_dbi": 13.5, "front_to_back_db": 22.0}},
    "yagi_17": {"label": "Yagi, 17 elements", "type": "yagi", "group": "uhf",
                "params": {"peak_gain_dbi": 15.5, "front_to_back_db": 25.0}},
    "yagi_hf": {"label": "HF tribander (3 el)", "type": "yagi", "group": "hf",
                "params": {"peak_gain_dbi": 8.0, "front_to_back_db": 20.0}},
    # Microwave
    "sector_90": {"label": "Sector panel, 90 deg", "type": "sector", "group": "shf",
                  "params": {"peak_gain_dbi": 16.0, "beamwidth_az_deg": 90.0,
                             "beamwidth_el_deg": 9.0}},
    "sector_120": {"label": "Sector panel, 120 deg", "type": "sector",
                   "group": "shf",
                   "params": {"peak_gain_dbi": 14.0, "beamwidth_az_deg": 120.0,
                              "beamwidth_el_deg": 10.0}},
    "panel_23": {"label": "Flat panel, 23 dBi", "type": "sector", "group": "shf",
                 "params": {"peak_gain_dbi": 23.0, "beamwidth_az_deg": 11.0,
                            "beamwidth_el_deg": 11.0, "front_to_back_db": 30.0}},
    "dish_300": {"label": "Dish, 30 cm", "type": "dish", "group": "shf",
                 "params": {"diameter_m": 0.30}},
    "dish_600": {"label": "Dish, 60 cm", "type": "dish", "group": "shf",
                 "params": {"diameter_m": 0.60}},
    "dish_900": {"label": "Dish, 90 cm", "type": "dish", "group": "shf",
                 "params": {"diameter_m": 0.90}},
    "dish_1200": {"label": "Dish, 1.2 m", "type": "dish", "group": "shf",
                  "params": {"diameter_m": 1.20}},
    "dish_1800": {"label": "Dish, 1.8 m", "type": "dish", "group": "shf",
                  "params": {"diameter_m": 1.80}},
    "isotropic": {"label": "Isotropic (0 dBi)", "type": "isotropic",
                  "group": "any", "params": {}},
    "custom": {"label": "Custom gain", "type": "isotropic", "group": "any",
               "params": {"gain_dbi": 6.0}},
}


def build_antenna(preset: str, freq_mhz: float, *, eps_r: float = 15.0,
                  sigma: float = 0.005, **overrides) -> Antenna:
    """Instantiate a preset at a given frequency, with per-request overrides."""
    spec = ANTENNA_PRESETS.get(preset)
    if spec is None:
        return isotropic(float(overrides.get("gain_dbi", 0.0)))
    params = dict(spec["params"])
    params.update({k: v for k, v in overrides.items() if v is not None})
    t = spec["type"]

    if t == "wire":
        return WireAntenna(freq_mhz=freq_mhz, kind=spec["kind"],
                           eps_r=eps_r, sigma=sigma,
                           height_m=float(params.get("height_m", 10.0)),
                           orientation_deg=float(params.get("orientation_deg", 90.0)),
                           droop_deg=float(params.get("droop_deg", 45.0)),
                           radials=int(params.get("radials", 16)),
                           name=spec["label"])
    if t == "omni":
        return OmniAntenna(peak_gain_dbi=float(params.get("peak_gain_dbi", 6.0)),
                           electrical_downtilt_deg=float(
                               params.get("downtilt_deg", 0.0)),
                           name=spec["label"])
    if t == "yagi":
        return YagiAntenna(peak_gain_dbi=float(params.get("peak_gain_dbi", 11.0)),
                           front_to_back_db=float(
                               params.get("front_to_back_db", 20.0)),
                           electrical_downtilt_deg=float(
                               params.get("downtilt_deg", 0.0)),
                           name=spec["label"])
    if t == "sector":
        return SectorAntenna(
            peak_gain_dbi=float(params.get("peak_gain_dbi", 16.0)),
            beamwidth_az_deg=float(params.get("beamwidth_az_deg", 90.0)),
            beamwidth_el_deg=float(params.get("beamwidth_el_deg", 9.0)),
            front_to_back_db=float(params.get("front_to_back_db", 25.0)),
            electrical_downtilt_deg=float(params.get("downtilt_deg", 0.0)),
            name=spec["label"])
    if t == "dish":
        return ParabolicAntenna(diameter_m=float(params.get("diameter_m", 0.6)),
                                freq_mhz=freq_mhz,
                                efficiency=float(params.get("efficiency", 0.55)),
                                name=spec["label"])
    return isotropic(float(params.get("gain_dbi", overrides.get("gain_dbi", 0.0))))


# Typical receiver sensitivities, dBm, for the mode selector
MODE_SENSITIVITY = {
    "cw": {"label": "CW (500 Hz)", "sensitivity_dbm": -132.0, "bandwidth_hz": 500},
    "ssb": {"label": "SSB (2.4 kHz)", "sensitivity_dbm": -125.0,
            "bandwidth_hz": 2400},
    "ft8": {"label": "FT8 / weak signal", "sensitivity_dbm": -145.0,
            "bandwidth_hz": 50},
    "fm": {"label": "FM (12 kHz)", "sensitivity_dbm": -119.0,
           "bandwidth_hz": 12000},
    "dmr": {"label": "DMR / C4FM", "sensitivity_dbm": -116.0,
            "bandwidth_hz": 7000},
    "aprs": {"label": "APRS / packet 1k2", "sensitivity_dbm": -118.0,
             "bandwidth_hz": 12000},
    "atv": {"label": "ATV / DATV", "sensitivity_dbm": -95.0,
            "bandwidth_hz": 2000000},
    "wifi_11n_20": {"label": "802.11n MCS0, 20 MHz", "sensitivity_dbm": -95.0,
                    "bandwidth_hz": 20000000},
    "wifi_11n_20_mcs7": {"label": "802.11n MCS7, 20 MHz",
                         "sensitivity_dbm": -74.0, "bandwidth_hz": 20000000},
    "wifi_11ac_40": {"label": "802.11ac MCS9, 40 MHz", "sensitivity_dbm": -69.0,
                     "bandwidth_hz": 40000000},
    "ax25_9k6": {"label": "9k6 packet", "sensitivity_dbm": -112.0,
                 "bandwidth_hz": 25000},
}

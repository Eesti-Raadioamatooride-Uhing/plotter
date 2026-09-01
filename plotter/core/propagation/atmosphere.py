"""Atmospheric effects that matter above roughly 1 GHz.

ITU-R P.676 (gaseous absorption), P.838 (rain specific attenuation),
P.837 rain rate, and P.530 (multipath fading, rain outage) - the pieces a
5-10 GHz or 24 GHz amateur link stands or falls on in the Baltic.
"""
from __future__ import annotations

import math

# Rain rate exceeded 0.01% of an average year, mm/h.
# ITU-R P.837 rain zone E covers Estonia and southern Finland; northern
# Finland is zone E/C. These are the numbers actually used for link design
# here, so they are worth having explicit rather than buried in a grid.
RAIN_RATE_R001 = {
    "estonia": 28.0,
    "finland_south": 26.0,
    "finland_north": 20.0,
    "itu_zone_e": 22.0,
    "itu_zone_h": 32.0,
}


def rain_rate_for(lat: float, lon: float) -> float:
    """Rough R0.01 lookup for the coverage area, mm/h."""
    if lat < 60.0:
        return RAIN_RATE_R001["estonia"]
    if lat < 64.0:
        return RAIN_RATE_R001["finland_south"]
    return RAIN_RATE_R001["finland_north"]


# ITU-R P.838-3 coefficients, log-log interpolated from the published table.
_P838 = [
    # f_GHz, k_h, a_h, k_v, a_v
    (1, 0.0000259, 0.9691, 0.0000308, 0.8592),
    (2, 0.0000847, 1.0664, 0.0000998, 0.9490),
    (4, 0.0001071, 1.6009, 0.0002461, 1.2476),
    (6, 0.0007056, 1.5900, 0.0004878, 1.5728),
    (7, 0.001915, 1.4810, 0.001425, 1.4745),
    (8, 0.004115, 1.3905, 0.003450, 1.3797),
    (10, 0.01217, 1.2571, 0.01129, 1.2156),
    (12, 0.02386, 1.1825, 0.02176, 1.1216),
    (15, 0.04481, 1.1233, 0.03738, 1.0440),
    (20, 0.09164, 1.0568, 0.09611, 0.9847),
    (24, 0.1571, 1.0568, 0.1425, 0.9491),
    (25, 0.1724, 0.9930, 0.1571, 0.9491),
    (30, 0.2403, 0.9485, 0.2291, 0.9129),
    (35, 0.3374, 0.9047, 0.3224, 0.8761),
    (40, 0.4431, 0.8673, 0.4274, 0.8421),
    (47, 0.5806, 0.8292, 0.5661, 0.8083),
    (50, 0.6347, 0.8158, 0.6208, 0.7967),
    (60, 0.8036, 0.7828, 0.7900, 0.7697),
    (70, 0.9384, 0.7620, 0.9218, 0.7558),
    (80, 1.0568, 0.7462, 1.0329, 0.7444),
    (100, 1.2516, 0.7209, 1.2191, 0.7262),
]


def rain_coefficients(freq_ghz: float, polarisation: str = "vertical",
                      tilt_deg: float | None = None) -> tuple[float, float]:
    """k and alpha for the given frequency and polarisation."""
    f = max(1.0, min(freq_ghz, 100.0))
    lo = _P838[0]
    hi = _P838[-1]
    for i in range(len(_P838) - 1):
        if _P838[i][0] <= f <= _P838[i + 1][0]:
            lo, hi = _P838[i], _P838[i + 1]
            break
    if hi[0] == lo[0]:
        t = 0.0
    else:
        t = (math.log(f) - math.log(lo[0])) / (math.log(hi[0]) - math.log(lo[0]))

    def interp(a, b, logk=False):
        if logk:
            return math.exp(math.log(a) + t * (math.log(b) - math.log(a)))
        return a + t * (b - a)

    kh = interp(lo[1], hi[1], True)
    ah = interp(lo[2], hi[2])
    kv = interp(lo[3], hi[3], True)
    av = interp(lo[4], hi[4])

    if tilt_deg is None:
        tilt_deg = 0.0 if polarisation.startswith("h") else 90.0
    tau = math.radians(tilt_deg)
    theta = 0.0  # path elevation angle, negligible for terrestrial links
    k = (kh + kv + (kh - kv) * math.cos(theta) ** 2 * math.cos(2 * tau)) / 2.0
    a = (kh * ah + kv * av + (kh * ah - kv * av) *
         math.cos(theta) ** 2 * math.cos(2 * tau)) / (2.0 * k)
    return k, a


def rain_attenuation_db(distance_km: float, freq_ghz: float, rain_rate: float,
                        polarisation: str = "vertical",
                        percent: float = 0.01) -> float:
    """ITU-R P.530-17 §2.4 rain attenuation exceeded for `percent` of time."""
    if freq_ghz < 1.0 or distance_km <= 0:
        return 0.0
    k, a = rain_coefficients(freq_ghz, polarisation)
    gamma_r = k * (rain_rate ** a)                      # dB/km
    r = 1.0 / (0.477 * distance_km ** 0.633 * rain_rate ** (0.073 * a) *
               freq_ghz ** 0.123 - 10.579 * (1.0 - math.exp(-0.024 * distance_km)))
    r = min(max(r, 0.0), 2.5)
    a001 = gamma_r * r * distance_km
    if abs(percent - 0.01) < 1e-9:
        return a001
    # P.530-17 eq. 34: scale A(0.01%) to other time percentages
    p = min(max(percent, 0.001), 5.0)
    exponent = -(0.546 + 0.043 * math.log10(p))
    return a001 * 0.12 * (p ** exponent)


def gaseous_attenuation_db_per_km(freq_ghz: float, temp_c: float = 5.0,
                                  pressure_hpa: float = 1013.0,
                                  water_vapour_g_m3: float = 7.5) -> float:
    """ITU-R P.676-12 Annex 2 simplified line-by-line, dry air + water vapour.

    Good to a few percent below 60 GHz, which covers everything an amateur
    is going to build in Estonia or Finland.
    """
    f = max(freq_ghz, 0.1)
    rp = pressure_hpa / 1013.0
    t = 288.0 / (273.0 + temp_c)
    rho = water_vapour_g_m3

    # Dry air (oxygen)
    if f <= 54:
        go = ((7.2 * t ** 2.8) / (f ** 2 + 0.34 * rp ** 2 * t ** 1.6) +
              (0.62 * (3.02e-4 * rp ** 3.5 + 1.0)) /
              ((54 - f) ** 1.16 + 0.83 * (3.02e-4 * rp ** 3.5 + 1.0))) \
             * f ** 2 * rp ** 2 * 1e-3
    else:
        go = 15.0 * f ** 2 * rp ** 2 * 1e-3  # crude above 54 GHz

    # Water vapour
    def g(fi):
        return 1.0 + ((f - fi) ** 2) / ((f + fi) ** 2)

    eta = 0.955 * rp * t ** 0.68 + 0.006 * rho
    gw = (3.98 * eta * math.exp(2.23 * (1 - t)) / ((f - 22.235) ** 2 + 9.42 * eta ** 2) * g(22.0)
          + 11.96 * eta * math.exp(0.7 * (1 - t)) / ((f - 183.31) ** 2 + 11.14 * eta ** 2)
          + 0.081 * eta * math.exp(6.44 * (1 - t)) / ((f - 321.226) ** 2 + 6.29 * eta ** 2)
          + 3.66 * eta * math.exp(1.6 * (1 - t)) / ((f - 325.153) ** 2 + 9.22 * eta ** 2)
          + 25.37 * eta * math.exp(1.09 * (1 - t)) / ((f - 380) ** 2)
          + 17.4 * eta * math.exp(1.46 * (1 - t)) / ((f - 448) ** 2)
          ) * f ** 2 * t ** 2.5 * rho * 1e-4
    return max(0.0, go + gw)


def multipath_fade_margin_db(distance_km: float, freq_ghz: float,
                             path_inclination_mrad: float,
                             availability_pct: float = 99.99,
                             geoclimatic_k: float = 1e-4) -> float:
    """ITU-R P.530-17 §2.3.1 multipath outage, inverted for a fade margin.

    geoclimatic_k of 1e-4 suits the flat, lake- and sea-adjacent terrain of
    Estonia and coastal Finland; drier inland terrain is nearer 3e-5.
    """
    if freq_ghz < 1.0 or distance_km <= 0:
        return 0.0
    p_out = max(1e-9, (100.0 - availability_pct) / 100.0)
    ep = abs(path_inclination_mrad)
    # p_w = K d^3.4 (1+|ep|)^-1.03 f^0.8 10^(-0.00076 hL - A/10)
    base = (geoclimatic_k * distance_km ** 3.4 * (1 + ep) ** -1.03 *
            freq_ghz ** 0.8)
    if base <= 0:
        return 0.0
    return max(0.0, 10.0 * math.log10(base / p_out))

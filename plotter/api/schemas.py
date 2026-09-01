"""Request models for the API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class AntennaSpec(BaseModel):
    preset: str = "isotropic"
    gain_dbi: float | None = None
    height_m: float | None = None          # for HF wires: height above ground
    orientation_deg: float | None = None
    droop_deg: float | None = None
    radials: int | None = None
    diameter_m: float | None = None
    peak_gain_dbi: float | None = None
    front_to_back_db: float | None = None
    beamwidth_az_deg: float | None = None
    beamwidth_el_deg: float | None = None
    downtilt_deg: float | None = None
    efficiency: float | None = None


class SiteSpec(BaseModel):
    name: str = "Site"
    lat: float
    lon: float
    height_agl_m: float = 10.0
    tx_power_w: float = 25.0
    feedline_loss_db: float = 1.0
    rx_noise_figure_db: float = 6.0
    antenna: AntennaSpec = Field(default_factory=AntennaSpec)


class LinkRequest(BaseModel):
    tx: SiteSpec
    rx: SiteSpec
    freq_mhz: float = 5760.0
    band: str | None = None
    mode: str = "wifi_11n_20"
    sensitivity_dbm: float | None = None
    bandwidth_hz: float | None = None
    k_factor: float = 4.0 / 3.0
    clutter_m: float = 0.0
    use_buildings: bool = False        # fold ETAK building heights into the path
    ground: str = "average"
    climate: int = 6
    polarisation: str = "vertical"
    availability_pct: float = 99.9
    # ITM time availability and confidence. 0.9 is the conservative "works on
    # 90 % of days" answer. Over-horizon paths (troposcatter, tropo ducting)
    # live at the other end of this: ask for 0.1 to see an enhanced day.
    reliability: float = 0.9
    confidence: float = 0.5
    high_res_terrain: bool = True
    points: int | None = None
    include_profile: bool = True


class CoverageRequest(BaseModel):
    site: SiteSpec
    freq_mhz: float = 145.0
    band: str | None = None
    mode: str = "fm"
    sensitivity_dbm: float | None = None
    rx_height_agl_m: float = 2.0
    rx_gain_dbi: float = 2.15
    max_range_km: float = 60.0
    azimuth_step_deg: float = 2.0
    range_step_m: float = 500.0
    antenna_bearing_deg: float = 0.0
    ground: str = "average"
    climate: int = 6
    polarisation: str = "vertical"
    reliability: float = 0.9
    confidence: float = 0.5
    k_factor: float = 4.0 / 3.0
    clutter_m: float = 0.0
    metric: str = "signal"
    image_size: int = 900


class HFRequest(BaseModel):
    site: SiteSpec
    band: str = "80m"
    freq_mhz: float | None = None
    mode: str = "ssb"
    sensitivity_dbm: float | None = None
    ground: str = "average"
    when: str | None = None            # ISO 8601 UTC; defaults to now
    use_live_ionosphere: bool = True
    distances_km: list[float] | None = None
    azimuths_deg: list[float] | None = None


class ProfileRequest(BaseModel):
    lat1: float
    lon1: float
    lat2: float
    lon2: float
    points: int | None = None
    high_res: bool = False
    use_buildings: bool = False

"""Configuration. Everything is overridable from the environment or .env."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLOTTER_", env_file=".env",
                                      extra="ignore")

    # --- server
    host: str = "127.0.0.1"
    port: int = 8088
    workers: int = 4
    log_level: str = "info"
    base_path: str = ""
    cors_origins: str = ""

    # --- web app hardening
    # Scrape/admin endpoints (registry refresh + JVIS discover) require this
    # key in an X-Admin-Key header. When empty, those endpoints are reachable
    # only from localhost, never from the public internet.
    admin_key: str = ""
    enable_docs: bool = False        # expose /docs, /redoc, /openapi.json
    max_body_bytes: int = 262144     # reject request bodies larger than 256 KiB
    # Cost ceilings so a single coverage request cannot exhaust the CPU.
    min_azimuth_step_deg: float = 0.5
    min_range_step_m: float = 100.0
    max_image_size: int = 2000

    # --- storage
    data_dir: Path = Path("./data")
    cache_dir: Path = Path("./data/cache")
    database_url: str = "sqlite:///./data/plotter.sqlite"

    # --- elevation
    copernicus_bucket: str = "https://copernicus-dem-30m.s3.amazonaws.com"
    lidar_ee_dir: str = ""          # local Maa-amet 1 m tiles, if downloaded
    lidar_fi_dir: str = ""          # local MML 2 m tiles, if downloaded
    # Maa-amet elevation WCS: on-demand 1 m LiDAR DTM for Estonia, cached per
    # cell. dtm-1 = 1 m, dtm-10 = 10 m, dtm-25 = 25 m. Enabled by default so
    # high-resolution terrain works out of the box over Estonia.
    maaamet_wcs_url: str = "https://teenus.maaamet.ee/ows/wcs-dtm"
    maaamet_wcs_coverage: str = "dtm-1"
    mml_wcs_url: str = ""
    mml_wcs_coverage: str = "korkeusmalli_2m"
    mml_api_key: str = ""
    allow_synthetic_terrain: bool = False

    # --- basemaps (all keyless unless noted)
    tile_sources: str = "osm,opentopo,maaamet_kaart,maaamet_reljeef,maaamet_foto,esri_sat"

    # --- registry scraping
    jvis_base_url: str = "https://jvis.ttja.ee"
    # The public register is served as an AJAX fragment (see jvis.py); a plain
    # GET 404s, so the scraper sends X-Requested-With. This is the current path.
    jvis_module_path: str = "/modules/sideluba/avalik"
    jvis_user_agent: str = ("Plotter/1.0 (amateur radio antenna planning; "
                            "contact the operator of this instance)")
    jvis_request_delay_s: float = 1.5
    jvis_max_pages: int = 400
    jvis_enabled: bool = True
    # The portal's robots.txt disallows everything but the homepage. Honour it
    # by default; only the operator of an instance who has cleared access with
    # TTJA should set this false.
    jvis_respect_robots: bool = True

    traficom_masts_url: str = ""
    osm_overpass_url: str = "https://overpass-api.de/api/interpreter"
    etak_wfs_url: str = "https://gsavalik.envir.ee/geoserver/etak/wfs"
    # ETAK building footprints carry surveyed heights (korgus_m); used to add
    # real building obstruction to link profiles over Estonia.
    etak_buildings_enabled: bool = True
    refresh_on_start: bool = False

    # --- propagation defaults
    default_climate: int = 6        # maritime temperate, over land
    default_ground: str = "average"
    default_k_factor: float = 4.0 / 3.0
    max_coverage_range_km: float = 400.0
    max_link_length_km: float = 1200.0
    coverage_workers: int = 8

    @property
    def tile_source_list(self) -> list[str]:
        return [s.strip() for s in self.tile_sources.split(",") if s.strip()]

    def ensure_dirs(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()

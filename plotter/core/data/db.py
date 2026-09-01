"""Local store for everything scraped: sites, masts, transmitters, repeaters."""
from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager

from sqlalchemy import (Boolean, Column, DateTime, Float, Index, Integer,
                        String, Text, create_engine, func, select)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

Base = declarative_base()


class Site(Base):
    """A physical structure you could put an antenna on."""
    __tablename__ = "sites"
    id = Column(Integer, primary_key=True)
    source = Column(String(40), index=True)       # osm | etak | jvis | manual | traficom
    source_id = Column(String(120), index=True)
    name = Column(String(255))
    kind = Column(String(60), index=True)         # mast | tower | water_tower | chimney | building | silo
    lat = Column(Float, index=True)
    lon = Column(Float, index=True)
    height_m = Column(Float)                      # structure height above ground
    ground_amsl_m = Column(Float)
    operator = Column(String(255))
    country = Column(String(2), index=True)
    address = Column(String(255))
    tags = Column(Text)                           # JSON blob of the raw record
    updated_at = Column(DateTime, default=dt.datetime.utcnow)

    __table_args__ = (
        Index("ix_sites_source_sourceid", "source", "source_id", unique=True),
        Index("ix_sites_bbox", "lat", "lon"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "source": self.source, "source_id": self.source_id,
            "name": self.name, "kind": self.kind, "lat": self.lat,
            "lon": self.lon, "height_m": self.height_m,
            "ground_amsl_m": self.ground_amsl_m, "operator": self.operator,
            "country": self.country, "address": self.address,
            "tags": json.loads(self.tags) if self.tags else {},
        }


class Transmitter(Base):
    """A licensed or registered emission: repeater, base station, link end."""
    __tablename__ = "transmitters"
    id = Column(Integer, primary_key=True)
    source = Column(String(40), index=True)       # jvis | traficom | eraru | sral | manual
    source_id = Column(String(160), index=True)
    callsign = Column(String(40), index=True)
    name = Column(String(255))
    service = Column(String(120), index=True)     # amateur | pmr | broadcast | fixed_link | maritime ...
    is_amateur = Column(Boolean, default=False, index=True)
    tx_mhz = Column(Float, index=True)
    rx_mhz = Column(Float)
    shift_mhz = Column(Float)
    ctcss_hz = Column(Float)
    mode = Column(String(60))                     # FM | DMR | D-STAR | C4FM | APRS | ATV | link
    bandwidth_khz = Column(Float)
    power_w = Column(Float)
    erp_w = Column(Float)
    antenna_height_m = Column(Float)
    antenna_pattern = Column(String(120))
    azimuth_deg = Column(Float)
    polarisation = Column(String(20))
    lat = Column(Float, index=True)
    lon = Column(Float, index=True)
    ground_amsl_m = Column(Float)
    locator = Column(String(12), index=True)
    licensee = Column(String(255))
    licence_no = Column(String(80))
    valid_from = Column(DateTime)
    valid_until = Column(DateTime)
    country = Column(String(2), index=True)
    site_id = Column(Integer, index=True)
    raw = Column(Text)
    updated_at = Column(DateTime, default=dt.datetime.utcnow)

    __table_args__ = (
        Index("ix_tx_source_sourceid", "source", "source_id", unique=True),
        Index("ix_tx_bbox", "lat", "lon"),
        Index("ix_tx_freq_service", "tx_mhz", "service"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "source": self.source, "callsign": self.callsign,
            "name": self.name, "service": self.service,
            "is_amateur": bool(self.is_amateur),
            "tx_mhz": self.tx_mhz, "rx_mhz": self.rx_mhz,
            "shift_mhz": self.shift_mhz, "ctcss_hz": self.ctcss_hz,
            "mode": self.mode, "bandwidth_khz": self.bandwidth_khz,
            "power_w": self.power_w, "erp_w": self.erp_w,
            "antenna_height_m": self.antenna_height_m,
            "azimuth_deg": self.azimuth_deg, "polarisation": self.polarisation,
            "lat": self.lat, "lon": self.lon,
            "ground_amsl_m": self.ground_amsl_m, "locator": self.locator,
            "licensee": self.licensee, "licence_no": self.licence_no,
            "country": self.country,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
        }


class ScrapeRun(Base):
    """Provenance: what ran, when, and what it got. Shown in the UI."""
    __tablename__ = "scrape_runs"
    id = Column(Integer, primary_key=True)
    source = Column(String(40), index=True)
    started_at = Column(DateTime, default=dt.datetime.utcnow)
    finished_at = Column(DateTime)
    ok = Column(Boolean, default=False)
    records = Column(Integer, default=0)
    message = Column(Text)


_engine = None
_Session = None


def init(database_url: str):
    global _engine, _Session
    _engine = create_engine(database_url, future=True,
                            connect_args={"check_same_thread": False}
                            if database_url.startswith("sqlite") else {})
    Base.metadata.create_all(_engine)
    _Session = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
    return _engine


@contextmanager
def session() -> Session:
    if _Session is None:
        raise RuntimeError("db.init() has not been called")
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def upsert_sites(records: list[dict]) -> int:
    n = 0
    with session() as s:
        for r in records:
            existing = s.execute(
                select(Site).where(Site.source == r["source"],
                                   Site.source_id == r["source_id"])
            ).scalar_one_or_none()
            if existing is None:
                s.add(Site(**r, updated_at=dt.datetime.utcnow()))
            else:
                for k, v in r.items():
                    setattr(existing, k, v)
                existing.updated_at = dt.datetime.utcnow()
            n += 1
    return n


def upsert_transmitters(records: list[dict]) -> int:
    n = 0
    with session() as s:
        for r in records:
            existing = s.execute(
                select(Transmitter).where(Transmitter.source == r["source"],
                                          Transmitter.source_id == r["source_id"])
            ).scalar_one_or_none()
            if existing is None:
                s.add(Transmitter(**r, updated_at=dt.datetime.utcnow()))
            else:
                for k, v in r.items():
                    setattr(existing, k, v)
                existing.updated_at = dt.datetime.utcnow()
            n += 1
    return n


def record_run(source: str, ok: bool, records: int, message: str = "",
               started: dt.datetime | None = None) -> None:
    with session() as s:
        s.add(ScrapeRun(source=source, ok=ok, records=records, message=message,
                        started_at=started or dt.datetime.utcnow(),
                        finished_at=dt.datetime.utcnow()))


def query_sites(bbox=None, kinds=None, limit: int = 5000) -> list[dict]:
    with session() as s:
        q = select(Site)
        if bbox:
            a, b, c, d = bbox
            q = q.where(Site.lat >= a, Site.lat <= c, Site.lon >= b, Site.lon <= d)
        if kinds:
            q = q.where(Site.kind.in_(kinds))
        return [r.to_dict() for r in s.execute(q.limit(limit)).scalars()]


def query_transmitters(bbox=None, amateur_only: bool | None = None,
                       freq_min: float | None = None,
                       freq_max: float | None = None,
                       service: str | None = None,
                       search: str | None = None,
                       limit: int = 5000) -> list[dict]:
    with session() as s:
        q = select(Transmitter)
        if bbox:
            a, b, c, d = bbox
            q = q.where(Transmitter.lat >= a, Transmitter.lat <= c,
                        Transmitter.lon >= b, Transmitter.lon <= d)
        if amateur_only is not None:
            q = q.where(Transmitter.is_amateur == amateur_only)
        if freq_min is not None:
            q = q.where(Transmitter.tx_mhz >= freq_min)
        if freq_max is not None:
            q = q.where(Transmitter.tx_mhz <= freq_max)
        if service:
            q = q.where(Transmitter.service == service)
        if search:
            like = f"%{search}%"
            q = q.where(Transmitter.callsign.ilike(like) |
                        Transmitter.name.ilike(like) |
                        Transmitter.licensee.ilike(like))
        return [r.to_dict() for r in s.execute(q.limit(limit)).scalars()]


def stats() -> dict:
    with session() as s:
        out = {
            "sites": s.execute(select(func.count(Site.id))).scalar_one(),
            "transmitters": s.execute(
                select(func.count(Transmitter.id))).scalar_one(),
            "amateur": s.execute(
                select(func.count(Transmitter.id)).where(
                    Transmitter.is_amateur.is_(True))).scalar_one(),
            "by_source": {},
            "last_runs": [],
        }
        for src, cnt in s.execute(
                select(Transmitter.source, func.count(Transmitter.id))
                .group_by(Transmitter.source)):
            out["by_source"][src] = cnt
        for src, cnt in s.execute(
                select(Site.source, func.count(Site.id)).group_by(Site.source)):
            out["by_source"][f"sites:{src}"] = cnt
        for r in s.execute(select(ScrapeRun).order_by(
                ScrapeRun.started_at.desc()).limit(12)).scalars():
            out["last_runs"].append({
                "source": r.source, "ok": bool(r.ok), "records": r.records,
                "message": r.message,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            })
        return out

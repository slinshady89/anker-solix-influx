"""
config.py – Gemeinsame Konfiguration und InfluxDB-Hilfsfunktionen.
Wird von legacy_import.py und live_service.py genutzt.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date

from dotenv import load_dotenv
from influxdb import InfluxDBClient

load_dotenv()

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Settings (aus Umgebungsvariablen / .env)
# ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Settings:
    anker_user: str
    anker_password: str
    anker_country: str

    influx_host: str
    influx_port: int
    influx_db: str
    influx_measurement: str
    influx_user: str
    influx_pass: str

    legacy_start_date: date


def load_settings() -> Settings:
    """Liest alle Einstellungen aus Umgebungsvariablen / .env."""
    return Settings(
        anker_user=_require("ANKER_USER"),
        anker_password=_require("ANKER_PASSWORD"),
        anker_country=os.getenv("ANKER_COUNTRY", "de"),
        influx_host=os.getenv("INFLUX_HOST", "localhost"),
        influx_port=int(os.getenv("INFLUX_PORT", "8086")),
        influx_db=os.getenv("INFLUX_DB", "solar"),
        influx_measurement=os.getenv("INFLUX_MEASUREMENT", "anker_solar"),
        influx_user=os.getenv("INFLUX_USER", ""),
        influx_pass=os.getenv("INFLUX_PASS", ""),
        legacy_start_date=date.fromisoformat(
            os.getenv("LEGACY_START_DATE", "2025-08-28")
        ),
    )


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(
            f"Pflichtumgebungsvariable '{key}' fehlt. Bitte in .env eintragen."
        )
    return val


# ──────────────────────────────────────────────────────────────
# InfluxDB
# ──────────────────────────────────────────────────────────────

def get_influx_client(cfg: Settings) -> InfluxDBClient:
    """Verbindet zur InfluxDB und legt die Datenbank an falls nötig."""
    client = InfluxDBClient(
        host=cfg.influx_host,
        port=cfg.influx_port,
        username=cfg.influx_user or None,
        password=cfg.influx_pass or None,
        database=cfg.influx_db,
    )
    existing = {d["name"] for d in client.get_list_database()}
    if cfg.influx_db not in existing:
        log.info("InfluxDB-Datenbank '%s' wird angelegt …", cfg.influx_db)
        client.create_database(cfg.influx_db)
    client.switch_database(cfg.influx_db)
    log.info(
        "InfluxDB verbunden: %s:%d  DB=%s",
        cfg.influx_host, cfg.influx_port, cfg.influx_db,
    )
    return client

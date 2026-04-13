"""
legacy_import.py
================
Einmaliger historischer Import der Anker MI80 Solar-Produktionsdaten
ab LEGACY_START_DATE in InfluxDB v1.8.

Auflösung:
  - Versucht zuerst Intraday-Daten per energy_analysis(rangeType="day")
    → liefert ~5–10-Minuten-Slots (tatsächliche Granularität ist Cloud-seitig
      fest; beim MI80 typischerweise 5-Minuten-Slots).
  - Fallback: Tages-Total via device_pv_energy_daily() wenn Intraday leer.

Ratenlimit-Hinweis:
  Anker Cloud: ~10–12 Requests/Minute seit Feb 2025.
  Bei ~600 Tagen × 1–3 Requests/Tag = 600–1800 Requests → 60–180 Minuten.
  Das Skript zeigt einen Fortschrittsbalken und eine ETA-Schätzung.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import date, datetime, timedelta

from aiohttp import ClientSession
from aiohttp.client_exceptions import ClientError

from .config import Settings, get_influx_client, load_settings

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Anker-API Login-Hilfsfunktion
# ──────────────────────────────────────────────────────────────

async def _create_api(cfg: Settings, websession: ClientSession):
    """Importiert und initialisiert AnkerSolixApi."""
    try:
        from api.api import AnkerSolixApi  # type: ignore[import]
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Anker Solix API nicht gefunden. Stelle sicher, dass das Repo "
            "geklont ist und du im richtigen Verzeichnis arbeitest:\n"
            "  git clone https://github.com/thomluther/anker-solix-api.git\n"
            "  cd anker-solix-api\n"
            "  uv sync"
        ) from exc

    api = AnkerSolixApi(
        cfg.anker_user,
        cfg.anker_password,
        cfg.anker_country,
        websession,
        log,
    )
    log.info("Authentifiziere bei Anker Cloud …")
    if await api.async_authenticate():
        log.info("Login erfolgreich.")
    else:
        log.info("Token aus Cache genutzt.")
    return api


# ──────────────────────────────────────────────────────────────
# Intraday-Daten per energy_analysis
# ──────────────────────────────────────────────────────────────

async def _fetch_intraday(api, site_id: str, device_sn: str, day: date) -> list[dict]:
    """
    Ruft Intraday-Slots für einen Tag ab (rangeType="day").
    Gibt eine Liste von InfluxDB-Points zurück oder [] bei Fehler/kein Ergebnis.

    Rückgabe der API: dict mit Zeitstempel-Keys und Leistungs-/Energie-Werten.
    Typische Struktur (kann je nach MI80-FW variieren):
      {
        "2025-08-28 08:00": {"time": "...", "value": 123.4, ...},
        ...
      }
    """
    try:
        data = await api.energy_analysis(
            siteId=site_id,
            deviceSn=device_sn,
            rangeType="day",
            startDay=datetime.combine(day, datetime.min.time()),
            endDay=datetime.combine(day, datetime.min.time()),
            devType="solar_production",
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("energy_analysis für %s fehlgeschlagen: %s", day, exc)
        return []

    if not data:
        return []

    # Die API gibt entweder eine Liste oder ein Dict zurück – beides abfangen
    items = data if isinstance(data, list) else list(data.values())
    if not items:
        return []

    points = []
    for item in items:
        if not isinstance(item, dict):
            continue

        # Zeitstempel parsen – mögliche Key-Namen
        ts = None
        for tk in ("time", "datetime", "date", "timestamp"):
            raw_ts = item.get(tk)
            if raw_ts:
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                    try:
                        ts = datetime.strptime(str(raw_ts), fmt)
                        break
                    except ValueError:
                        continue
            if ts:
                break

        if ts is None:
            # Kein Zeitstempel → überspringen
            continue

        # Energiewert extrahieren
        fields: dict[str, float] = {}
        for vk in ("value", "solar_production_wh", "energy_wh", "pv_energy_wh", "wh"):
            v = item.get(vk)
            if v is not None:
                try:
                    fields["solar_production_wh"] = float(v)
                    break
                except (TypeError, ValueError):
                    pass

        # Alle weiteren numerischen Felder
        for k, v in item.items():
            if k in ("time", "datetime", "date", "timestamp") or k in fields:
                continue
            if isinstance(v, (int, float)):
                fields[k] = float(v)
            elif isinstance(v, str):
                try:
                    fields[k] = float(v)
                except ValueError:
                    pass

        if not fields:
            continue

        points.append({
            "measurement": "",  # wird beim Schreiben gesetzt
            "tags": {
                "device_sn": device_sn,
                "source": "legacy_import",
                "granularity": "intraday",
            },
            "time": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fields": fields,
        })

    return points


# ──────────────────────────────────────────────────────────────
# Tages-Total-Fallback
# ──────────────────────────────────────────────────────────────

async def _fetch_daily_total(api, device_sn: str, day: date) -> list[dict]:
    """Tages-Total via device_pv_energy_daily (1 Punkt pro Tag)."""
    try:
        data = await api.device_pv_energy_daily(
            deviceSn=device_sn,
            startDay=datetime.combine(day, datetime.min.time()),
            numDays=1,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("device_pv_energy_daily für %s fehlgeschlagen: %s", day, exc)
        return []

    if not data:
        return []

    day_str = day.isoformat()
    values = data.get(day_str) or next(iter(data.values()), {})
    fields: dict[str, float] = {}

    for ck in ("pv_total_energy_wh", "solar_energy_wh", "energy_wh", "total_energy"):
        v = values.get(ck)
        if v is not None:
            try:
                fields["solar_production_wh"] = float(v)
                break
            except (TypeError, ValueError):
                pass

    for i in range(1, 5):
        v = values.get(f"pv{i}_energy_wh")
        if v is not None:
            try:
                fields[f"pv{i}_wh"] = float(v)
            except (TypeError, ValueError):
                pass

    for k, v in values.items():
        if k in ("date",) or k in fields:
            continue
        if isinstance(v, (int, float)):
            fields[k] = float(v)
        elif isinstance(v, str):
            try:
                fields[k] = float(v)
            except ValueError:
                pass

    if not fields:
        return []

    return [{
        "measurement": "",
        "tags": {
            "device_sn": device_sn,
            "source": "legacy_import",
            "granularity": "daily",
        },
        "time": datetime.combine(day, datetime.min.time()).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fields": fields,
    }]


# ──────────────────────────────────────────────────────────────
# Hauptroutine
# ──────────────────────────────────────────────────────────────

async def run_import(cfg: Settings) -> None:
    today = date.today()
    start = cfg.legacy_start_date
    total_days = (today - start).days + 1

    if total_days <= 0:
        log.error("LEGACY_START_DATE liegt in der Zukunft. Abgebrochen.")
        return

    log.info("Historischer Import: %s → %s  (%d Tage)", start, today, total_days)

    influx = get_influx_client(cfg)
    measurement = cfg.influx_measurement

    # Rate limiting: mindestens 5–6 Sekunden zwischen Anfragen
    # (10–12 Anfragen/Minute ab Feb 2025)
    MIN_REQUEST_DELAY = 6.0  # Sekunden
    last_request_time = 0.0
    request_count = 0
    minute_start = 0.0

    async def rate_limited_call(coro):
        """Wrapper für Rate-Limited API-Aufrufe."""
        nonlocal last_request_time, request_count, minute_start
        
        now = time.time()
        
        # Minute zurücksetzen wenn nötig
        if now - minute_start > 60:
            request_count = 0
            minute_start = now
        
        # Zu viele Anfragen in dieser Minute?
        if request_count >= 10:
            wait_time = 60 - (now - minute_start)
            if wait_time > 0:
                log.warning("Rate-Limit erreicht (10 Anfragen/Min). Warte %.1f s …", wait_time)
                await asyncio.sleep(wait_time)
                request_count = 0
                minute_start = time.time()
        
        # Mindestabstand seit letzter Anfrage?
        elapsed = now - last_request_time
        if elapsed < MIN_REQUEST_DELAY:
            sleep_time = MIN_REQUEST_DELAY - elapsed
            await asyncio.sleep(sleep_time)
        
        last_request_time = time.time()
        request_count += 1
        log.debug("API-Anfrage #%d in dieser Minute", request_count)
        
        return await coro

    async with ClientSession() as websession:
        api = await _create_api(cfg, websession)

        log.info("Lade Site-Informationen …")
        await rate_limited_call(api.update_sites())
        await rate_limited_call(api.update_device_details())

        if not api.sites:
            log.error("Keine Sites gefunden – Zugangsdaten prüfen!")
            influx.close()
            return

        for site_id, site in api.sites.items():
            site_name = (site.get("site_info") or {}).get("site_name", site_id)

            is_inverter = (
                str(site_id).startswith("virtual-") and site.get("solar_list")
            )
            if not is_inverter:
                log.debug("Site '%s' ist kein Standalone-Inverter → übersprungen.", site_name)
                continue

            device_sn = site_id.split("-", 1)[1] if "-" in site_id else site_id
            log.info("Inverter '%s'  SN: %s", site_name, device_sn)

            # Rate-Limit: max. 10 Anfragen/Min → min. 6 Sekunden zwischen Anfragen
            # 2 Anfragen am Anfang (update_sites, update_device_details)
            # + 1–2 Anfragen pro Tag (intraday, ggf. fallback daily)
            requests_init = 2
            requests_per_day_avg = 1.5  # Intraday + ggf. Daily-Fallback
            total_requests_est = requests_init + (total_days * requests_per_day_avg)
            eta_min = (total_requests_est * MIN_REQUEST_DELAY) / 60
            log.info(
                "Rate-Limit: 10 Anfragen/Minute (min. %.1f s zwischen Anfragen). "
                "Schätzung: ~%.0f Anfragen × %.1f s = ~%.0f min.",
                MIN_REQUEST_DELAY, total_requests_est, MIN_REQUEST_DELAY, eta_min,
            )

            total_written = 0
            intraday_days = 0
            daily_days = 0

            for i, n in enumerate(range(total_days)):
                current_day = start + timedelta(days=n)
                pct = (i + 1) / total_days * 100

                # Fortschritt
                if i % 10 == 0 or i == total_days - 1:
                    log.info(
                        "  [%5.1f%%]  %s  (intraday=%d, daily=%d, punkte=%d)",
                        pct, current_day, intraday_days, daily_days, total_written,
                    )

                # 1. Versuch: Intraday (mit Rate-Limit)
                points = await rate_limited_call(_fetch_intraday(api, site_id, device_sn, current_day))

                if points:
                    intraday_days += 1
                else:
                    # 2. Fallback: Tages-Total (mit Rate-Limit)
                    log.debug("Kein Intraday für %s → Daily-Fallback.", current_day)
                    points = await rate_limited_call(_fetch_daily_total(api, device_sn, current_day))
                    if points:
                        daily_days += 1

                if points:
                    for p in points:
                        p["measurement"] = measurement
                    influx.write_points(points, time_precision="s")
                    total_written += len(points)
                else:
                    log.debug("Keine Daten für %s.", current_day)

            log.info(
                "Fertig: %d Punkte geschrieben  "
                "(intraday: %d Tage, daily-fallback: %d Tage, ohne Daten: %d Tage)",
                total_written,
                intraday_days,
                daily_days,
                total_days - intraday_days - daily_days,
            )

    influx.close()


def main_cli() -> None:
    logging.basicConfig(
        level=logging.DEBUG if "--debug" in sys.argv else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    cfg = load_settings()
    try:
        asyncio.run(run_import(cfg))
    except KeyboardInterrupt:
        log.warning("Abgebrochen.")
    except ClientError as exc:
        log.error("Netzwerkfehler: %s", exc)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        log.exception("Unerwarteter Fehler: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main_cli()

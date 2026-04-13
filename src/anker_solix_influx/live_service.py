"""
live_service.py
===============
Dauerhafter Service: Pollt die Anker Cloud alle 60 Sekunden und schreibt
Echtzeit-Leistungs- und Energiedaten in InfluxDB v1.8.

Measurement: anker_solar_live
Fields:
  solar_power_w      – Momentanleistung in Watt
  today_energy_wh    – Energie heute (Wh, Tageszähler)
  <ggf. weitere>     – alle numerischen Felder die die API liefert

Tags:
  device_sn          – Seriennummer des MI80
  source             – "live_service"

Hinweis zum Poll-Intervall:
  Die Anker Cloud aktualisiert Gerätedaten ca. alle 60 Sekunden.
  Minütliches Polling macht daher Sinn – kürzere Intervalle liefern
  identische Werte und erhöhen nur das API-Ratenlimit-Risiko.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime, timezone

from aiohttp import ClientSession
from aiohttp.client_exceptions import ClientError

from .config import Settings, get_influx_client, load_settings

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60
# Nach diesem vielfachen schlägt ein Re-Login fehl → neu initialisieren
REAUTH_INTERVAL = 60 * 60  # jede Stunde Token erneuern


# ──────────────────────────────────────────────────────────────
# Daten-Extraktion aus api.sites / api.devices
# ──────────────────────────────────────────────────────────────

def _extract_live_fields(api, device_sn: str) -> dict[str, float]:
    """
    Liest Momentan-Werte aus dem API-Cache nach update_sites().
    Die genauen Keys hängen von der MI80-Firmware ab – robustes Mapping.
    """
    fields: dict[str, float] = {}

    # Gerät aus dem devices-Cache
    dev = (api.devices or {}).get(device_sn, {})

    # Bekannte Keys für Momentanleistung (Watt)
    for pk in (
        "solar_power_w",
        "generate_power",
        "solar_power",
        "pv_power",
        "ac_power",
        "power_w",
    ):
        v = dev.get(pk)
        if v is not None:
            try:
                fields["solar_power_w"] = float(v)
                break
            except (TypeError, ValueError):
                pass

    # Bekannte Keys für Tagesenergie (Wh oder kWh)
    for ek, scale in (
        ("today_solar_energy_kwh", 1000.0),
        ("today_solar_energy", 1000.0),
        ("solar_energy_today_kwh", 1000.0),
        ("today_energy_kwh", 1000.0),
        ("today_solar_energy_wh", 1.0),
        ("today_energy_wh", 1.0),
        ("generate_energy", 1.0),
    ):
        v = dev.get(ek)
        if v is not None:
            try:
                fields["today_energy_wh"] = float(v) * scale
                break
            except (TypeError, ValueError):
                pass

    # Status als numerischen Code speichern (1=online, 0=offline)
    online = dev.get("wifi_online") or dev.get("is_online") or dev.get("status")
    if online is not None:
        try:
            fields["online"] = float(bool(online))
        except (TypeError, ValueError):
            pass

    # Alle weiteren numerischen Felder generisch übernehmen
    skip = {"device_sn", "type", "name", "alias", "model", "version"}
    for k, v in dev.items():
        if k in skip or k in fields:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            fields[k] = float(v)
        elif isinstance(v, str):
            try:
                fields[k] = float(v)
            except ValueError:
                pass

    return fields


# ──────────────────────────────────────────────────────────────
# Service-Schleife
# ──────────────────────────────────────────────────────────────

async def run_service(cfg: Settings) -> None:
    log.info("Live-Service gestartet. Poll-Intervall: %d s.", POLL_INTERVAL_SECONDS)

    influx = get_influx_client(cfg)
    measurement = cfg.influx_measurement + "_live"

    # Mögliche Sites und Devices werden beim ersten Poll ermittelt
    inverter_map: dict[str, str] = {}  # site_id → device_sn

    async with ClientSession() as websession:
        try:
            from api.api import AnkerSolixApi  # type: ignore[import]
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Anker Solix API nicht gefunden. Bitte README beachten."
            ) from exc

        api = AnkerSolixApi(
            cfg.anker_user,
            cfg.anker_password,
            cfg.anker_country,
            websession,
            log,
        )

        log.info("Erster Login …")
        await api.async_authenticate()
        await api.update_sites()
        await api.update_device_details()

        # Inverter-Map aufbauen
        for site_id, site in (api.sites or {}).items():
            if str(site_id).startswith("virtual-") and site.get("solar_list"):
                device_sn = site_id.split("-", 1)[1] if "-" in site_id else site_id
                inverter_map[site_id] = device_sn
                log.info("Gefundener Inverter: SN=%s  site=%s", device_sn, site_id)

        if not inverter_map:
            log.error("Keine Standalone-Inverter gefunden – Service beendet.")
            influx.close()
            return

        last_reauth = time.monotonic()
        poll_count = 0

        while True:
            loop_start = time.monotonic()

            # Stündlicher Re-Auth (Token-Erneuerung)
            if loop_start - last_reauth > REAUTH_INTERVAL:
                log.debug("Token-Erneuerung …")
                try:
                    await api.async_authenticate()
                    last_reauth = loop_start
                except Exception as exc:  # noqa: BLE001
                    log.warning("Token-Erneuerung fehlgeschlagen: %s", exc)

            try:
                await api.update_sites()
            except Exception as exc:  # noqa: BLE001
                log.warning("update_sites() fehlgeschlagen: %s", exc)

            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            points = []

            for site_id, device_sn in inverter_map.items():
                fields = _extract_live_fields(api, device_sn)

                if not fields:
                    log.debug(
                        "Keine Felder für SN=%s – API-Cache leer oder Gerät offline.",
                        device_sn,
                    )
                    # Trotzdem einen Punkt mit online=0 schreiben
                    fields = {"online": 0.0}

                points.append({
                    "measurement": measurement,
                    "tags": {
                        "device_sn": device_sn,
                        "source": "live_service",
                    },
                    "time": now_utc,
                    "fields": fields,
                })

            if points:
                try:
                    influx.write_points(points, time_precision="s")
                except Exception as exc:  # noqa: BLE001
                    log.error("InfluxDB write fehlgeschlagen: %s", exc)

            poll_count += 1
            if poll_count % 10 == 0:
                log.info(
                    "Poll #%d abgeschlossen. Felder: %s",
                    poll_count,
                    list(points[0]["fields"].keys()) if points else "–",
                )
            else:
                log.debug("Poll #%d → %d Punkte geschrieben.", poll_count, len(points))

            # Wartezeit bis zum nächsten Poll (genau 60 s von Loop-Start)
            elapsed = time.monotonic() - loop_start
            sleep_for = max(0.0, POLL_INTERVAL_SECONDS - elapsed)
            await asyncio.sleep(sleep_for)


def main_cli() -> None:
    logging.basicConfig(
        level=logging.DEBUG if "--debug" in sys.argv else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    cfg = load_settings()
    try:
        asyncio.run(run_service(cfg))
    except KeyboardInterrupt:
        log.info("Service durch Benutzer beendet.")
    except ClientError as exc:
        log.error("Netzwerkfehler: %s", exc)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        log.exception("Unerwarteter Fehler: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main_cli()

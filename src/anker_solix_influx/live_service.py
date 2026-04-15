"""
Persistent service polling Anker Cloud and writing real-time power & status
data to InfluxDB v1.8.

Measurement: power
Tags:
  device_id          – Device identifier (anker-pv)
  phase              – Phase (blank for total)
Fields (float):
  active             – Active power in Watts (from generate_power)

Measurement: status
Tags:
  device_id          – Device identifier (anker-pv)
Fields:
  online             – Online status (1.0=online, 0.0=offline)

Note: Polls Anker Cloud at configured interval. Anker API updates ~60s cadence.
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

POLL_INTERVAL_SECONDS = 30  # twice per minute polling
REAUTH_INTERVAL = 60 * 60  # Hourly token refresh


def _extract_power_fields(device: dict) -> dict[str, float] | None:
    """Extract active power from generate_power. Returns None if unavailable."""
    try:
        power_val = float(device.get("generate_power", 0))
        return {"active": power_val}
    except (TypeError, ValueError):
        return None


def _extract_status_fields(device: dict) -> dict[str, float] | None:
    """Extract status as boolean (1.0 online, 0.0 offline). Returns None if unavailable."""
    status_val = device.get("status")
    if status_val is not None:
        try:
            status_bool = bool(int(status_val) if isinstance(status_val, str) else status_val)
            return {"online": 1.0 if status_bool else 0.0}
        except (TypeError, ValueError):
            return None
    return None


async def run_service(cfg: Settings) -> None:
    log.info(f"Live service started. Poll interval: {POLL_INTERVAL_SECONDS}s")

    influx = get_influx_client(cfg)

    # Track site → inverter device mapping
    inverter_map: dict[str, str] = {}  # site_id → device_sn

    async with ClientSession() as websession:
        try:
            from api.api import AnkerSolixApi  # type: ignore[import]
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Anker Solix API not found. See README for setup instructions."
            ) from exc

        api = AnkerSolixApi(
            cfg.anker_user,
            cfg.anker_password,
            cfg.anker_country,
            websession,
            log,
        )

        log.info("Initial authentication...")
        await api.async_authenticate()
        await api.update_sites()
        await api.update_device_details()

        # Build inverter mapping
        for site_id, site in (api.sites or {}).items():
            if str(site_id).startswith("virtual-") and site.get("solar_list"):
                device_sn = site_id.split("-", 1)[1] if "-" in site_id else site_id
                inverter_map[site_id] = device_sn
                log.info(f"Found inverter: SN={device_sn} site={site_id}")

        if not inverter_map:
            log.error("No standalone inverters found. Service exiting.")
            influx.close()
            return

        last_reauth = time.monotonic()
        poll_count = 0

        while True:
            loop_start = time.monotonic()

            # Hourly token refresh
            if loop_start - last_reauth > REAUTH_INTERVAL:
                log.debug("Refreshing authentication token...")
                try:
                    await api.async_authenticate()
                    last_reauth = loop_start
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"Token refresh failed: {exc}")

            try:
                await api.update_sites()
            except Exception as exc:  # noqa: BLE001
                log.warning(f"update_sites() failed: {exc}")

            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            power_points = []
            status_points = []

            for site_id, device_sn in inverter_map.items():
                device = (api.devices or {}).get(device_sn, {})

                # Power measurement: active, apparent, factor (all floats, no strings)
                power_fields = _extract_power_fields(device)
                if power_fields:
                    power_points.append({
                        "measurement": "power",
                        "tags": {"device_id": "anker-pv", "phase": ""},
                        "time": now_utc,
                        "fields": power_fields,
                    })
                else:
                    log.debug(f"No power fields extracted for SN={device_sn}")

                # Status measurement: online, energy, other numeric fields
                status_fields = _extract_status_fields(device)
                if status_fields:
                    status_points.append({
                        "measurement": "status",
                        "tags": {"device_id": "anker-pv"},
                        "time": now_utc,
                        "fields": status_fields,
                    })
                else:
                    log.debug(f"No status fields extracted for SN={device_sn}")

            # Write power points (active/apparent/factor all floats with f suffix)
            if power_points:
                try:
                    influx.write_points(power_points, time_precision="s")
                except Exception as exc:  # noqa: BLE001
                    log.error(f"InfluxDB power write failed: {exc}")

            # Write status points
            if status_points:
                try:
                    influx.write_points(status_points, time_precision="s")
                except Exception as exc:  # noqa: BLE001
                    log.error(f"InfluxDB status write failed: {exc}")

            poll_count += 1
            if poll_count % 10 == 0:
                fields_count = len(power_points[0]["fields"]) if power_points else 0
                log.info(f"Poll #{poll_count} completed. Power fields: {fields_count}")
            else:
                log.debug(f"Poll #{poll_count} → {len(power_points)} power + {len(status_points)} status points written")

            # Wait until next poll cycle
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
        log.info("Service terminated by user.")
    except ClientError as exc:
        log.error(f"Network error: {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        log.exception(f"Unexpected error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main_cli()

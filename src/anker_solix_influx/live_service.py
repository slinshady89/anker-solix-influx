"""
Persistent service polling Anker Cloud and writing real-time power & status
data to InfluxDB v1.8.

Measurement: power
Tags:
  device_id          – Device identifier (anker-pv)
  phase              – Phase (blank for total)
  interpolated       – 0=real poll, 1=interpolated
Fields (float):
  active             – Active power in Watts (from generate_power)

Measurement: status
Tags:
  device_id          – Device identifier (anker-pv)
  interpolated       – 0=real poll, 1=interpolated
Fields:
  online             – Online status (1.0=online, 0.0=offline)

Data alignment strategy: Polls every 30s but writes 1Hz interpolated data.
Actual polls marked interpolated=0, synthetic data marked interpolated=1.
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
        last_poll_time = time.monotonic()
        
        # Cache last polled values for interpolation
        last_power_fields: dict[str, dict[str, float]] = {}  # device_sn -> fields
        last_status_fields: dict[str, dict[str, float]] = {}  # device_sn -> fields

        while True:
            loop_start = time.monotonic()
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            
            # Check if it's time for actual API poll (every 30 seconds)
            time_since_poll = loop_start - last_poll_time
            is_poll_time = time_since_poll >= POLL_INTERVAL_SECONDS
            
            power_points = []
            status_points = []

            # Hourly token refresh
            if loop_start - last_reauth > REAUTH_INTERVAL:
                log.debug("Refreshing authentication token...")
                try:
                    await api.async_authenticate()
                    last_reauth = loop_start
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"Token refresh failed: {exc}")

            # Perform actual API poll if it's time
            if is_poll_time:
                try:
                    await api.update_sites()
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"update_sites() failed: {exc}")
                
                poll_count += 1
                last_poll_time = loop_start

                for site_id, device_sn in inverter_map.items():
                    device = (api.devices or {}).get(device_sn, {})

                    # Poll and cache power data
                    power_fields = _extract_power_fields(device)
                    if power_fields:
                        last_power_fields[device_sn] = power_fields
                        power_points.append({
                            "measurement": "power",
                            "tags": {"device_id": "anker-pv", "phase": "", "interpolated": "0"},
                            "time": now_utc,
                            "fields": power_fields,
                        })
                    
                    # Poll and cache status data
                    status_fields = _extract_status_fields(device)
                    if status_fields:
                        last_status_fields[device_sn] = status_fields
                        status_points.append({
                            "measurement": "status",
                            "tags": {"device_id": "anker-pv", "interpolated": "0"},
                            "time": now_utc,
                            "fields": status_fields,
                        })
                
                if poll_count % 10 == 0:
                    log.info(f"Poll #{poll_count} completed at {now_utc}")
                else:
                    log.debug(f"Poll #{poll_count} completed")
            
            else:
                # Generate interpolated data between polls
                for device_sn in inverter_map.values():
                    if device_sn in last_power_fields:
                        power_points.append({
                            "measurement": "power",
                            "tags": {"device_id": "anker-pv", "phase": "", "interpolated": "1"},
                            "time": now_utc,
                            "fields": last_power_fields[device_sn],
                        })
                    
                    if device_sn in last_status_fields:
                        status_points.append({
                            "measurement": "status",
                            "tags": {"device_id": "anker-pv", "interpolated": "1"},
                            "time": now_utc,
                            "fields": last_status_fields[device_sn],
                        })

            # Write all points to InfluxDB
            if power_points:
                try:
                    influx.write_points(power_points, time_precision="s")
                except Exception as exc:  # noqa: BLE001
                    log.error(f"InfluxDB power write failed: {exc}")

            if status_points:
                try:
                    influx.write_points(status_points, time_precision="s")
                except Exception as exc:  # noqa: BLE001
                    log.error(f"InfluxDB status write failed: {exc}")

            # Sleep until next 1Hz tick
            elapsed = time.monotonic() - loop_start
            sleep_for = max(0.0, 1.0 - elapsed)
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

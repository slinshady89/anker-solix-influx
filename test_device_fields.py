#!/usr/bin/env python3
"""
Quick test to extract and display all device fields from Anker API.
Run: uv run python test_device_fields.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

from aiohttp import ClientSession

# Add api package to path
sys.path.insert(0, str(Path(__file__).parent.parent / "anker-solix-api"))

from src.anker_solix_influx.config import load_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


async def main() -> None:
    cfg = load_settings()
    log.info("Testing Anker API device field extraction...")

    async with ClientSession() as session:
        try:
            from api.api import AnkerSolixApi  # type: ignore[import]
        except ModuleNotFoundError as exc:
            log.error(f"Anker API not found: {exc}")
            sys.exit(1)

        api = AnkerSolixApi(
            cfg.anker_user,
            cfg.anker_password,
            cfg.anker_country,
            session,
            log,
        )

        log.info("Authenticating...")
        await api.async_authenticate()

        log.info("Updating sites...")
        await api.update_sites()

        log.info("Updating device details...")
        await api.update_device_details()

        # Display all devices and their fields
        if not api.devices:
            log.warning("No devices found")
            return

        for device_sn, device_data in api.devices.items():
            log.info(f"\n{'='*80}")
            log.info(f"Device: {device_sn}")
            log.info(f"{'='*80}")

            # Categorize fields by type
            float_fields: dict[str, float] = {}
            int_fields: dict[str, int] = {}
            bool_fields: dict[str, bool] = {}
            str_fields: dict[str, str] = {}
            other_fields: dict[str, object] = {}

            for key, val in device_data.items():
                if isinstance(val, float):
                    float_fields[key] = val
                elif isinstance(val, int) and not isinstance(val, bool):
                    int_fields[key] = val
                elif isinstance(val, bool):
                    bool_fields[key] = val
                elif isinstance(val, str):
                    str_fields[key] = val
                else:
                    other_fields[key] = val

            # Print by category
            if float_fields:
                log.info("\n📊 FLOAT fields:")
                for k, v in sorted(float_fields.items()):
                    log.info(f"  {k:40s} = {v}")

            if int_fields:
                log.info("\n🔢 INT fields:")
                for k, v in sorted(int_fields.items()):
                    log.info(f"  {k:40s} = {v}")

            if bool_fields:
                log.info("\n✅ BOOL fields:")
                for k, v in sorted(bool_fields.items()):
                    log.info(f"  {k:40s} = {v}")

            if str_fields:
                log.info("\n📝 STRING fields:")
                for k, v in sorted(str_fields.items()):
                    log.info(f"  {k:40s} = {v[:50]}")

            if other_fields:
                log.info("\n❓ OTHER fields:")
                for k, v in sorted(other_fields.items()):
                    log.info(f"  {k:40s} = {type(v).__name__}: {str(v)[:50]}")

            # JSON dump for reference
            log.info("\n📋 Full JSON dump:")
            log.info(json.dumps(device_data, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())

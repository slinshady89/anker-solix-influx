#!/usr/bin/env python3
"""Test field extraction functions with real device data."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.anker_solix_influx.live_service import _extract_power_fields, _extract_status_fields

# Real device data from test run
device_data = {
    "device_sn": "E07100000199",
    "is_admin": True,
    "owner_user_id": "37577eb50e7768f646636cacfb619b53913a6aef",
    "device_pn": "A5143",
    "mqtt_supported": True,
    "mqtt_overlay": False,
    "mqtt_status_request": False,
    "type": "inverter",
    "bt_ble_mac": "80646F52F1E2",
    "name": "MI80 Microinverter(BLE)",
    "alias": "MI80 Microinverter(BLE)",
    "wifi_online": True,
    "wifi_name": "SlowWatchdog",
    "charge": False,
    "bws_surplus": "0",
    "sw_version": "v0.0.1",
    "site_id": "virtual-E07100000199",
    "generate_power": "58",  # String type!
    "status": "1",  # String type!
    "status_desc": "online",
}

print("=" * 80)
print("Testing Simplified Power Field Extraction")
print("=" * 80)
power_fields = _extract_power_fields(device_data)
if power_fields:
    print(f"✅ Extracted power fields: {power_fields}")
    for field, value in power_fields.items():
        print(f"  {field}: {value} (type: {type(value).__name__})")
else:
    print("❌ No power fields extracted")

print("\n" + "=" * 80)
print("Testing Simplified Status Field Extraction")
print("=" * 80)
status_fields = _extract_status_fields(device_data)
if status_fields:
    print(f"✅ Extracted status fields: {status_fields}")
    for field, value in status_fields.items():
        print(f"  {field}: {value} (type: {type(value).__name__})")
else:
    print("❌ No status fields extracted")

print("\n" + "=" * 80)
print("InfluxDB write format:")
print("=" * 80)
if power_fields:
    influx_line = f"power,device_id=anker-pv,phase= active={power_fields['active']}"
    print(f"Power: {influx_line}")
if status_fields:
    influx_line = f"status,device_id=anker-pv online={status_fields['online']}"
    print(f"Status: {influx_line}")


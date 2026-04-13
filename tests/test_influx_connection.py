#!/usr/bin/env python3
"""Test InfluxDB connection using config.py settings."""

import sys
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s'
)
log = logging.getLogger(__name__)

try:
    from src.anker_solix_influx.config import load_settings, get_influx_client
    
    log.info("📋 Loading settings from .env...")
    settings = load_settings()
    
    log.info(f"🔧 InfluxDB Configuration:")
    log.info(f"   Host:        {settings.influx_host}")
    log.info(f"   Port:        {settings.influx_port}")
    log.info(f"   Database:    {settings.influx_db}")
    log.info(f"   User:        {settings.influx_user if settings.influx_user else '(none)'}")
    log.info(f"   Measurement: {settings.influx_measurement}")
    
    log.info("\n🔗 Attempting connection...")
    client = get_influx_client(settings)
    
    # Test basic operations
    log.info("✅ Connection successful!")
    
    # List databases
    dbs = client.get_list_database()
    db_names = [d['name'] for d in dbs]
    log.info(f"📊 Available databases: {db_names}")
    
    # Check if measurement exists
    query = f'SELECT count(*) FROM "{settings.influx_measurement}"'
    result = client.query(query)
    points = list(result.get_points())
    
    if points:
        count = points[0].get('count', 0)
        log.info(f"📈 Found {count} data points in measurement '{settings.influx_measurement}'")
    else:
        log.warning(f"⚠️  Measurement '{settings.influx_measurement}' is empty or doesn't exist yet")
    
    log.info("\n✨ All tests passed!")
    sys.exit(0)
    
except Exception as e:
    log.error(f"\n❌ Error: {e}", exc_info=True)
    sys.exit(1)

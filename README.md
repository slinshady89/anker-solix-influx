# anker-solix-influx

Exportiert Anker Solix MI80 Solar-Produktionsdaten in InfluxDB v1.8.

## Zwei Skripte

| Skript | Zweck | Auflösung |
|---|---|---|
| `solix-legacy-import` | Einmaliger historischer Import ab 28.08.2025 | ~10 min (Intraday via `energy_analysis`) + Tages-Totals als Fallback |
| `solix-live-service` | Dauerhafter Service, pollt minütlich | ~60 s (Cloud-Update-Intervall) |

## Auflösung – was die API liefert

- **Rückwirkend (Legacy):** Die Cloud speichert Intraday-Daten via `energy_analysis(rangeType="day")`.
  Die tatsächliche Auflösung sind **~5–10 Minuten-Slots** (device-abhängig, muss live geprüft werden).
  Falls kein Intraday verfügbar: Fallback auf ein Tages-Total-Datenpunkt.
- **Live:** `update_sites()` liefert Echtzeit-Leistungswerte. Die Cloud aktualisiert ca. alle **60 Sekunden**.
  Der Service pollt im 60-Sekunden-Takt und schreibt `solar_power_w` (Watt, Momentanwert) sowie
  den laufenden Tageszähler `today_energy_wh`.

## Setup

```bash
# 1. uv installieren (falls noch nicht vorhanden)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Repo klonen und Umgebung einrichten
git clone <dieses-repo>
cd anker-solix-influx
uv sync

# 3. Konfiguration anlegen
cp .env.example .env
# .env mit Editor öffnen und Zugangsdaten eintragen
```

## Konfiguration (.env)

```ini
ANKER_USER=deine@email.de
ANKER_PASSWORD=dein_passwort
ANKER_COUNTRY=de

INFLUX_HOST=localhost
INFLUX_PORT=8086
INFLUX_DB=solar
INFLUX_MEASUREMENT=anker_solar
# INFLUX_USER=          # nur bei aktivierter InfluxDB-Auth
# INFLUX_PASS=

# Legacy Import: Startdatum
LEGACY_START_DATE=2025-08-28
```

## Ausführen

```bash
# Historischer Import (einmalig)
uv run solix-legacy-import

# Live-Service (läuft dauerhaft)
uv run solix-live-service
```

## Als systemd-Service einrichten

```bash
sudo cp scripts/solix-live.service /etc/systemd/system/
# Pfade in der .service-Datei anpassen
sudo systemctl daemon-reload
sudo systemctl enable --now solix-live.service
sudo journalctl -fu solix-live.service
```

## InfluxDB-Measurements

### `anker_solar` (Legacy – historische Tages-/Intraday-Daten)

| Field | Typ | Beschreibung |
|---|---|---|
| `solar_production_wh` | float | Kumulierte Energie im Slot/Tag (Wh) |
| `pv1_wh` / `pv2_wh` | float | PV-String-Einzelwerte (falls von API geliefert) |

Tags: `device_sn`, `source=legacy_import`, `granularity=intraday\|daily`

### `anker_solar_live` (Live-Service)

| Field | Typ | Beschreibung |
|---|---|---|
| `solar_power_w` | float | Momentanleistung (W) |
| `today_energy_wh` | float | Energie heute (Wh, läuft täglich zurück) |
| `status` | string | Gerätestatus |

Tags: `device_sn`, `source=live_service`
# anker-solix-influx
# anker-solix-influx

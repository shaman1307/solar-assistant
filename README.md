# Solar Smart

Web app for energy planning and SRNE inverter control via [SolarAssistant](https://solarassistant.com/) on a Raspberry Pi.

Runs alongside SolarAssistant (port **80**) on port **8000** and reads live metrics plus InfluxDB history. When **Smart mode** is enabled, it pushes the next-hour plan to the SA timer schedule.

Tested with **SRNE 3-phase** hybrid inverter and Energa **G12** tariff (Poland).

## Features

- Live dashboard: PV, load, battery, grid, energy overview
- Hourly energy accruals and charts (InfluxDB)
- PV forecast (Open-Meteo) and RCE sell prices
- Energy arbitrage simulation and monthly cost history
- Timer schedule read/write through SolarAssistant REST API

## Requirements

- Raspberry Pi with SolarAssistant installed (InfluxDB on `localhost:8086`)
- Python 3.11+ (3.12 recommended)
- SRNE inverter exposed through SolarAssistant discovery API
- SA web password configured (Configuration → Security)

## Install on Pi

```bash
git clone https://github.com/shaman1307/solar-assistant.git
cd solar-assistant
cp sa-config.yaml.example sa-config.yaml
# Edit sa-config.yaml — set sa.password and your site parameters

bash install.sh
```

Solar Smart: `http://<pi-ip>:8000/`

Service management:

```bash
sudo systemctl status smart
bash scripts/reload-smart.sh
```

## Deploy updates from Windows

```powershell
.\sync-to-pi.ps1
```

`sa-config.yaml` on the Pi is **not** overwritten by deploy (local site config).

## Local development (Windows)

```powershell
py -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy sa-config.local.yaml.example sa-config.local.yaml
# Set sa.host to your Pi IP and sa.password

.\scripts\run-local.ps1
```

Uses SSH tunnel to Pi InfluxDB and SA API. Open `http://127.0.0.1:8000/`.

## Configuration

| File | Purpose |
|------|---------|
| `sa-config.yaml` | Pi production config (gitignored) |
| `sa-config.yaml.example` | Template for new installs |
| `sa-config.local.yaml` | Windows dev config (gitignored) |

Key flags in config:

- `smart_mode_enabled` — when `true`, hourly job writes the plan to SA timer slot 1
- `debug_tab_enabled` — show Debug tab in UI

## License

MIT — see [LICENSE](LICENSE).

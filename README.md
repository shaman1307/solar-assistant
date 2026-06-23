# Solar Smart

Web app for energy planning and SRNE inverter control via [SolarAssistant](https://solarassistant.com/) on a Raspberry Pi.

Runs alongside SolarAssistant (port **80**) on port **8000** and reads live metrics plus InfluxDB history. When **Smart mode** is enabled, it pushes the next-hour plan to the SA timer schedule.

Tested with **SRNE 3-phase** hybrid inverter and Energa **G12** tariff (Poland).

## Features

- Live dashboard: PV, load, battery, grid, energy overview
- Hourly energy accruals and charts (InfluxDB)
- PV forecast (Open-Meteo) and RCE sell prices
- **Energy arbitrage simulation and monthly cost history** — rolling 24-hour plan that minimises your G12 electricity bill; hourly table with planned actions, import/export, and cash balance; closed-month totals from Influx actuals
- **Timer schedule read/write through SolarAssistant REST API** — view and edit the SRNE timed charge/discharge slots in the UI; with Smart mode on, the backend writes slot 1 automatically each hour

## Smart energy planning

Solar Smart does not talk to the inverter directly. It plans energy flows and, when enabled, programs the **Timer Schedule** that SolarAssistant already exposes for SRNE hybrid inverters.

### What the algorithm does

Every hour at **:00 Europe/Warsaw** the app rebuilds a **Plan Simulation** (this runs even when Smart mode is off):

1. **Inputs** — live battery SOC and today's completed hours from InfluxDB; PV/load forecast for today and tomorrow (Open-Meteo + weekday load cache); Energa **G12** buy prices (peak/off-peak zones); **PSE RCE** quarter-hourly sell prices; battery capacity, inverter AC limit, and transfer losses from config.
2. **Optimizer** — a 15-minute dynamic-programming model (`plan_optimizer.py`) steps through the next 24 hours and picks, for each quarter, whether to import from the grid, charge the battery from the grid, or export stored energy to the grid. The objective is to minimise net cash cost: `grid import × G12 buy − grid export × export credit`. Battery export is allowed only when RCE is high enough to beat keeping energy for self-consumption at off-peak buy price. A SOC floor (`min_soc_pct`) and night reserve prevent over-discharging before the next PV window.
3. **Output in the UI** — the **Energy arbitrage** tab shows the rolling plan hour by hour (action label, PV, load, grid flows, SOC, energy and service cost). Completed hours are reconciled against Influx actuals. **Monthly history** aggregates past days the same way.

At **23:59 Europe/Warsaw** a nightly job rebuilds weekday Load + Open-Meteo PV profiles for tomorrow and the day after, then estimates tomorrow's energy gap (PV − load − usable SOC). If the house needs more energy than PV can cover, it stores a suggested grid-charge rate (`_charge_rate_kw`) used when the plan calls for **Charging from Grid**.

### How it controls the inverter

When `smart_mode_enabled: true` in config, the same hourly **:00** job extracts the **next clock hour** from the plan and writes it to SolarAssistant via REST:

| Planned action | Inverter effect |
|----------------|-----------------|
| **Charging from Grid** | Timer **charge** slot 1 for the next hour (from/to, power, target SOC); `grid_charge` switch ON; charge current limit set from planned kW |
| **Discharging to Grid and Load** | Timer **discharge** slot 1 for the next hour (power, stop at `min_soc_pct`) |
| Other (idle, PV to load, discharge to load only) | Slot 1 cleared — inverter follows normal on-grid behaviour |

Slots **2 and 3** on the inverter are left unchanged (merged with the live SA schedule). Manual edits in the **Rules** tab still work; **Sync hour** triggers the same write immediately.

SolarAssistant applies the timer rules on the SRNE; Solar Smart only sets the schedule and grid-charge permission — work mode and other SA settings stay under your control.

### What you get as a user

- **Visibility** — one place to see live status, buy vs sell prices (G12 + RCE chart), and a cost-aware plan for the rest of today and tomorrow.
- **Optional automation** — flip `smart_mode_enabled` to let the Pi push the next-hour charge or discharge window without opening the SA UI each hour.
- **Accountability** — planned vs actual rows and monthly totals so you can see whether arbitrage decisions matched reality and what they cost.

Smart mode is **off by default**; the plan table and charts work without writing anything to the inverter.

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

- `smart_mode_enabled` — when `true`, the hourly :00 job writes the next-hour plan to SA timer slot 1 (see [Smart energy planning](#smart-energy-planning))
- `debug_tab_enabled` — show Debug tab in UI

## License

MIT — see [LICENSE](LICENSE).

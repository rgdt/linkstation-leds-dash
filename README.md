# LincStation LED Dashboard

Web dashboard for controlling the LED controller (I2C 0x26) on a LincStation N2 NAS.
Replaces the original Unraid interface (port 50000) with a clean, bilingual FR/EN SPA.

Protocol reverse-engineered by [ffalt](https://github.com/ffalt).

## Features

- **LED BAR** — solid / breath / loop modes, brightness, color pickers
- **Front panel** — per-bay white / red / blink / off controls (4× NVMe, 2× SATA, NET, PWR)
- **System stats** — CPU, RAM, disks, polled every 5 s
- **Bilingual** — FR / EN toggle, no page reload
- **Zero-build frontend** — single HTML file, vanilla JS, Google Fonts only

## Requirements

- Debian 13 (Trixie) or any modern Linux
- Python 3.11+
- `i2c-tools` package
- Root (or `i2c` group membership)

## Install

```bash
# 1. System dependencies
apt install i2c-tools python3-venv python3-pip

# 2. Deploy
mkdir -p /opt/lincstation-dashboard
cp led_api.py led_dashboard.html requirements.txt /opt/lincstation-dashboard/

# 3. Python virtualenv
cd /opt/lincstation-dashboard
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 4. Systemd service
cp lincstation-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now lincstation-dashboard

# 5. Open
echo "Dashboard at http://$(hostname -I | awk '{print $1}'):50001"
```

## Manual run (dev / test)

```bash
cd /opt/lincstation-dashboard
venv/bin/python3 led_api.py
```

The server starts even without I2C hardware — it logs a warning and operates in
state-only mode (no `i2cset` calls are made).

## REST API

| Method | Path | Description |
|--------|------|-------------|
| GET  | `/api/state`          | Full LED state + strip metadata |
| GET  | `/api/status`         | System stats + LED state (polling endpoint) |
| POST | `/api/ledbar`         | Update LED BAR (mode, brightness, colors) |
| POST | `/api/switch`         | Power button LED (`color`, `blink`) |
| POST | `/api/strip/<id>`     | Bay LED strip (`state`: off/white/red/blink) |
| POST | `/api/all/off`        | Turn every LED off |

### Strip IDs
`hdd0`, `hdd1`, `net`, `nvme1`, `nvme2`, `nvme3`, `nvme4`

### Examples

```bash
# Solid cyan at 60% brightness
curl -X POST http://localhost:50001/api/ledbar \
  -H 'Content-Type: application/json' \
  -d '{"mode":"solid","brightness":153,"color":[0,200,255]}'

# NVMe 1 — red
curl -X POST http://localhost:50001/api/strip/nvme1 \
  -H 'Content-Type: application/json' \
  -d '{"state":"red"}'

# All off
curl -X POST http://localhost:50001/api/all/off \
  -H 'Content-Type: application/json' -d '{}'
```

## Security

The API has **no authentication**: anyone who can reach port 50001 can drive the
LEDs and edit presets/rules. Keep it on a trusted LAN, or set `BIND_HOST=127.0.0.1`
and put it behind a reverse proxy that adds authentication.

Built-in protections:

- State-changing requests (`POST`/`PUT`/`DELETE`) from another origin are rejected,
  and `POST`/`PUT` require `Content-Type: application/json` (blocks CSRF from
  third-party web pages). If you serve the dashboard through a reverse proxy on a
  different host name, list it in `ALLOWED_ORIGINS` (comma-separated, e.g.
  `ALLOWED_ORIGINS=https://leds.example.lan`).
- Preset names, rule labels and animation parameters are validated server-side and
  escaped in the UI.
- Only `led_dashboard.html` is served as a static file.

| Variable | Default | Purpose |
|----------|---------|---------|
| `BIND_HOST` | `0.0.0.0` | Listen address |
| `ALLOWED_ORIGINS` | *(empty)* | Extra origins allowed to call the API |
| `I2C_BUS` | auto-detect | I2C bus number |
| `DATA_DIR` | app directory | Where presets/rules are stored |

## Extension points

`led_api.py` marks extension areas with a comment block at the bottom of the routes.
Suggested additions:

- **Presets** — `POST /api/preset/<name>` to store/replay named LED configurations
- **Scheduling** — cron-based automation (e.g. dim at night, alert on disk error)
- **Hardware sync** — `POST /api/sync` to read register state back into memory
- **Auth** — add a simple token header check or Flask-Login before the routes

## I2C register map (reference)

| Register | Purpose |
|----------|---------|
| 0x90     | LED BAR mode (0=solid 1=breath 2=loop) |
| 0x91     | LED BAR brightness (0–255) |
| 0x92–94  | Solid/breath RGB |
| 0x95–97  | Loop color 1 RGB |
| 0x98–9A  | Loop color 2 RGB |
| 0xA0/B0  | Bay/switch LED on/off — HDD + NET (mask per bay) |
| 0xA1/B1  | Bay LED on/off — NVMe (mask per bay) |
| 0x50     | Power button blink (0x01=on, 0x00=off) |
| 0x52–5E  | Bay blink registers (one per bay, even-spaced) |

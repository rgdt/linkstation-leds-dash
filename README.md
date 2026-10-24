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
cp led_api.py auth.py led_dashboard.html requirements.txt /opt/lincstation-dashboard/

# 3. Python virtualenv
cd /opt/lincstation-dashboard
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 4. Systemd service — set OIDC_ISSUER, OIDC_CLIENT_ID and PUBLIC_BASE_URL
#    in it first (see "Authentication" below)
cp lincstation-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now lincstation-dashboard

# 5. Open, and sign in right away: the first account becomes admin
echo "Dashboard at http://$(hostname -I | awk '{print $1}'):50001"
```

## Authentication (OIDC / Pocket ID)

Sign-in is delegated to an OpenID Connect provider. It was built for
[Pocket ID](https://pocket-id.org) (passkey sign-in), but any standard OIDC
provider works (Authelia, Authentik, Keycloak…).

In Pocket ID: **OIDC Clients → Add OIDC Client**

- *Name*: `LincStation LEDs`
- *Callback URLs*: `https://leds.example.lan/auth/callback`
- *Public client*: checked (recommended: PKCE only, leave `OIDC_CLIENT_SECRET`
  empty), or unchecked and set the secret it shows.
- *Allowed user groups*: restrict the client to the people concerned.

Then on the dashboard side (`environment:` in docker-compose, or `Environment=`
lines in the systemd unit):

```
OIDC_ISSUER=https://id.example.lan
OIDC_CLIENT_ID=<client id shown by Pocket ID>
PUBLIC_BASE_URL=https://leds.example.lan
```

**Roles.** The first account to sign in becomes *admin*: there is nobody else
to grant it, so do that first sign-in right after deploying. Later accounts
arrive *pending* (no access; configurable with `OIDC_DEFAULT_ROLE`) until an
admin gives them a role from the **Users** button in the header:

| Role | Can |
|------|-----|
| Pending | Nothing, until an admin approves the account. |
| Viewer | See the dashboard, LED state, presets and rules. |
| Operator | Also drive the LEDs and edit presets, rules and the poller. |
| Admin | Also manage users and their roles. |

Roles are stored in `DATA_DIR/users.json` and read on every request: demoting
or removing an account takes effect immediately (a removed account that signs
in again comes back as *pending*). The last admin can be neither demoted nor
removed.

Without `OIDC_ISSUER` the dashboard stays locked (503). `DASHBOARD_AUTH=off`
disables authentication; only use it behind a reverse proxy that already
authenticates.

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
# With authentication on, the API needs the browser session cookie; these
# examples assume DASHBOARD_AUTH=off on a trusted host.
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

Built-in protections:

- Every page and API route requires an OIDC session and a sufficient role (see
  [Authentication](#authentication-oidc--pocket-id)). Serve the dashboard over HTTPS
  (`PUBLIC_BASE_URL` starting with `https://`) so the session cookie is `Secure`;
  it is always `HttpOnly` and `SameSite=Lax`.

- State-changing requests (`POST`/`PUT`/`DELETE`) from another origin are rejected,
  and `POST`/`PUT` require `Content-Type: application/json` (blocks CSRF from
  third-party web pages). `PUBLIC_BASE_URL` is always an allowed origin; list any
  other host name the dashboard is reached through in `ALLOWED_ORIGINS`
  (comma-separated).
- Preset names, rule labels and animation parameters are validated server-side and
  escaped in the UI.
- Only `led_dashboard.html` is served as a static file.

| Variable | Default | Purpose |
|----------|---------|---------|
| `OIDC_ISSUER` | *(empty)* | OIDC provider URL (`https://id.example.lan` for Pocket ID). Without it the dashboard stays locked. |
| `OIDC_CLIENT_ID` | *(empty)* | OIDC client id |
| `OIDC_CLIENT_SECRET` | *(empty)* | Client secret; leave empty for a Pocket ID public client (PKCE only) |
| `OIDC_SCOPES` | `openid profile email` | Requested scopes |
| `PUBLIC_BASE_URL` | *(empty)* | URL the dashboard is served on; the callback is `PUBLIC_BASE_URL/auth/callback` |
| `OIDC_REDIRECT_URI` | *(derived)* | Overrides the callback URL |
| `OIDC_DEFAULT_ROLE` | `pending` | Role of accounts after the first (`pending`, `viewer`, `operator`, `admin`) |
| `DASHBOARD_AUTH` | `on` | `off` disables authentication (only behind an authenticating proxy) |
| `SESSION_SECRET` | generated | Session cookie signing key; generated once into `DATA_DIR/session_secret` otherwise |
| `SESSION_MAX_AGE_SECONDS` | `604800` | Session lifetime (7 days) |
| `BIND_HOST` | `0.0.0.0` | Listen address |
| `ALLOWED_ORIGINS` | *(empty)* | Extra origins allowed to call the API |
| `I2C_BUS` | auto-detect | I2C bus number |
| `DATA_DIR` | app directory | Where presets, rules, users and the session key are stored |

## Extension points

`led_api.py` marks extension areas with a comment block at the bottom of the routes.
Suggested additions:

- **Presets** — `POST /api/preset/<name>` to store/replay named LED configurations
- **Scheduling** — cron-based automation (e.g. dim at night, alert on disk error)
- **Hardware sync** — `POST /api/sync` to read register state back into memory

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

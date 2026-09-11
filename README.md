# maas-shelly-powerstrip

A small webservice that lets [Ubuntu MAAS](https://maas.io) power-control PCs
plugged into a **Shelly Power Strip** (Gen2+ RPC API, tested with the
Power Strip Gen4, model `S4PL-00416EU`). MAAS talks to it through its built-in
`webhook` power type.

## Behaviour

| MAAS action | What the service does |
|-------------|-----------------------|
| power query | Reports `off` if the outlet relay is open, **or** if the relay is closed but usage has stayed at/below `IDLE_WATT_THRESHOLD` (default 5 W) for `IDLE_WINDOW_SECONDS` (default 5 s). Otherwise `on`. |
| power off   | Opens the relay. |
| power on    | Relay open: close it. Relay closed but PC idle (as above): open the relay, wait `CYCLE_OFF_SECONDS` (default 5 s), close it again. Already drawing power: no-op. |

A background task samples the strip every `SAMPLE_INTERVAL` seconds (default 1 s),
so status queries return instantly from the recent history. Power-on waits for a
full window of samples before deciding, so it may take up to ~10 s when a cycle
is needed.

The PC's BIOS must be set to power on when AC is restored, otherwise closing the
relay does nothing.

## Configuration (environment variables)

| Variable | Default | Meaning |
|----------|---------|---------|
| `SHELLY_HOST` | `192.168.202.252` | IP or hostname of the strip. Use an IP with a DHCP reservation; mDNS names generally do not resolve inside containers. |
| `SHELLY_PASSWORD` | unset | Password if authentication is enabled on the strip (user is always `admin`). |
| `IDLE_WATT_THRESHOLD` | `5` | Watts at or below which the PC counts as off. |
| `IDLE_WINDOW_SECONDS` | `5` | Seconds usage must stay idle before reporting off. |
| `SAMPLE_INTERVAL` | `1` | Seconds between polls of the strip. |
| `CYCLE_OFF_SECONDS` | `5` | Relay-open time during a forced power cycle. |
| `API_TOKEN` | unset | If set, requests must send `Authorization: Bearer <token>`. |

## Running

Images are published to GitHub Container Registry on every push, versioned by
[autoversion](https://github.com/trondhindenes/autoversion); `latest` tracks
release builds from `main`.

```sh
docker run -d --restart unless-stopped -p 8000:8000 \
  -e SHELLY_HOST=192.168.202.252 \
  ghcr.io/trondhindenes/maas-shelly-powerstrip:latest
# or build locally
docker compose up -d --build
# or run from source
uv run uvicorn maas_shelly_powerstrip.main:app --host 0.0.0.0 --port 8000
```

Endpoints (outlet ids are `0`–`3` on a four-socket strip):

```
GET  /healthz
GET  /outlets
GET  /outlets/{n}/status   -> {"outlet": 1, "status": "on", "output": true, "apower": 43.2}
POST /outlets/{n}/on       -> ... "action": "powered_on" | "power_cycled" | "already_on"
POST /outlets/{n}/off      -> ... "action": "powered_off"
```

## Web UI

Open `http://<service-host>:8000/` for a small [htmx](https://htmx.org) page that
shows every outlet (status, relay, watts, refreshed every 2 s) with On/Off
buttons that go through the same logic as the MAAS endpoints. htmx is served
locally, so it works without internet access. If `API_TOKEN` is set the page
asks for the token once and keeps it in the browser's localStorage.

## MAAS setup

For each machine, set power type **Webhook** with:

| Field | Value |
|-------|-------|
| Power on URI    | `http://<service-host>:8000/outlets/<n>/on` |
| Power off URI   | `http://<service-host>:8000/outlets/<n>/off` |
| Power query URI | `http://<service-host>:8000/outlets/<n>/status` |
| Power on regex  | `status.*\bon\b` (MAAS default) |
| Power off regex | `status.*\boff\b` (MAAS default) |
| Power token     | value of `API_TOKEN`, if set |

The MAAS rack controller must be able to reach the service; the service must be
able to reach the strip.

## Development

```sh
uv sync
uv run pytest
```

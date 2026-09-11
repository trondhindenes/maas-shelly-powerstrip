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

MAAS talks to this service with its built-in **Webhook** power type
(MAAS 3.1 or newer). Each machine gets its own set of URIs pointing at the
outlet it is plugged into.

### 1. Make sure the network paths work

- The **rack controller** that manages the machine must be able to reach this
  service. MAAS picks the rack controller by the IP in the power query URI, so
  use an IP or a hostname every rack controller can resolve.
- This service must be able to reach the strip (`SHELLY_HOST`).

Check from the rack controller before touching MAAS:

```sh
curl http://<service-host>:8000/outlets/0/status
# {"outlet":0,"status":"off","output":false,"apower":0.0,"action":null}
```

### 2. Configure the machine in the MAAS UI

Machine → **Configuration** → **Power configuration** → *Edit*:

| Field | Value |
|-------|-------|
| Power type | `Webhook` |
| URI to power on the node | `http://<service-host>:8000/outlets/<n>/on` |
| URI to power off the node | `http://<service-host>:8000/outlets/<n>/off` |
| URI to query the nodes power status | `http://<service-host>:8000/outlets/<n>/status` |
| Regex to confirm the node is on | `"status":"on"` |
| Regex to confirm the node is off | `"status":"off"` |
| Power user / Power password | leave empty |
| Power token | value of `API_TOKEN` if you set one, otherwise empty |
| Verify SSL connections | `No` (plain HTTP) |

`<n>` is the outlet number (`0`–`3`, left to right on a four-socket strip).

**The regexes must be changed from the MAAS defaults.** MAAS ships with
`status.*\:.*running` / `status.*\:.*stopped`, which never match this service's
JSON, and MAAS would report the machine's power as *Unknown*. The two patterns
above match the exact `"status":"on"` / `"status":"off"` fields in the response.

### 3. Or configure it with the MAAS CLI

```sh
SYSTEM_ID=abc123          # from the machine's URL in the UI, or `maas $PROFILE machines read`
SERVICE=http://<service-host>:8000
OUTLET=0

maas $PROFILE machine update $SYSTEM_ID \
  power_type=webhook \
  power_parameters_power_on_uri="$SERVICE/outlets/$OUTLET/on" \
  power_parameters_power_off_uri="$SERVICE/outlets/$OUTLET/off" \
  power_parameters_power_query_uri="$SERVICE/outlets/$OUTLET/status" \
  power_parameters_power_on_regex='"status":"on"' \
  power_parameters_power_off_regex='"status":"off"' \
  power_parameters_power_verify_ssl=n
# add this if API_TOKEN is set on the service:
#  power_parameters_power_token="$API_TOKEN"
```

### 4. Verify

- In the UI the machine's power state should show *Off* within a minute
  (MAAS polls the query URI periodically). Use the machine's power menu to
  *Power on* and watch the service log or the web UI at `$SERVICE/`.
- MAAS treats any HTTP status ≥ 400 as a failed power action. This service
  returns 404 for an unknown outlet and 503 if the strip is unreachable, both of
  which show up as an error on the machine in MAAS.

### How MAAS calls the service

| MAAS action | Request | Notes |
|-------------|---------|-------|
| power query | `GET  <power_query_uri>` | Response body is matched against the on regex first, then the off regex; no match means *Unknown*. |
| power on    | `POST <power_on_uri>` | Blocks until done. Takes ~10 s when a power-cycle is needed. |
| power off   | `POST <power_off_uri>` | |

MAAS also sends a `System_Id` header with the machine's system ID, and either
`Authorization: Bearer <power_token>` or HTTP Basic auth if a user/password is
set. This service only checks the bearer token.

## Development

```sh
uv sync
uv run pytest
```

# Automated Asset Tracking System (AATS)

AATS is a lab anti-theft monitoring system for tracking USB and Bluetooth assets across PCs. Student agents monitor devices, publish MQTT events, and the FastAPI server turns those events into live dashboard status, alerts, and history.

## Current status

WARNING: This project is a lab prototype and is NOT production-ready. The
codebase contains several critical security and stability issues that must be
addressed before deployment. See `future_fix.txt` for the canonical backlog of
issues and remediation steps, and `test_report.txt` for the latest test results.

**SECURITY NOTICE (READ FIRST):** Do not deploy this repository to production
without first completing the Phase 1 fixes listed in `future_fix.txt` (hardcoded
credentials, rate limiting, MQTT auth, and event integrity). Before running any
services, create a local `.env` file (see "Configuration" below) with the
required admin credentials and broker settings. Never commit `.env` to source
control — a reference example is provided in `.env.example`.

## What it does

- Tracks USB devices by `VID/PID` and Bluetooth devices by `MAC` address and RSSI threshold.
- Detects transient disconnects with server-side debounce before escalating to a critical alert.
- Publishes PC heartbeat status with MQTT Last Will and Testament so unexpected shutdowns appear as offline.
- Stores device history, current state, PC heartbeat, and pending debounce windows in SQLite.
- Provides a password-gated admin dashboard with lab selection, PC/device removal controls, live refresh, and an alert sound for new critical events.

## Repo layout

- `student_agent/` runs on each lab PC and monitors USB/Bluetooth assets.
- `server/` exposes the FastAPI backend, MQTT listener, SQLite persistence, and admin APIs.
- `admin_dashboard/` contains the browser UI for login, lab selection, live status, and event history.

## Code map

- `student_agent/service_runner.py` loads `student_agent/config.json`, creates the MQTT client, starts the USB and Bluetooth monitors, and publishes a periodic online heartbeat.
- `student_agent/device_monitor.py` polls Windows `Get-PnpDevice -PresentOnly` output and emits `CONNECTED` or `MISSING` when tracked USB devices change state.
- `student_agent/bluetooth_monitor.py` uses `bleak` to scan nearby devices, compares RSSI against the configured threshold, and emits `CONNECTED`, `WEAK_SIGNAL`, or `MISSING`.
- `student_agent/mqtt_client.py` publishes retained status messages and non-retained event messages to the topic pair used by the server.
- `student_agent/windows_service.py` exposes the agent as a Windows service named `AATSAgentService` for boot startup.
- `server/app.py` contains the FastAPI routes, admin login, debounce handling, pending-window restoration, and alert promotion logic.
- `server/database.py` owns the SQLite schema and the read/write methods for events, current device state, PC heartbeat, exclusions, and pending windows.
- `server/mqtt_listener.py` subscribes to the MQTT status and event topics and forwards decoded JSON payloads into the server handlers.
- `server/inspect_db.py` prints recent events, current device state, or heartbeat state for screenshots and debugging.
- `admin_dashboard/login.html`, `index.html`, and `dashboard.html` form the actual UI flow: login, lab selection, then a per-lab dashboard.
- `admin_dashboard/script.js` handles authentication, lab loading, dashboard rendering, periodic refresh, device removal actions, and alert playback.
- `admin_dashboard/styles.css` defines the UI theme, cards, status colors, and dashboard layout.
- `admin_setup.py` and `agent_setup.py` are the packaging and first-run helpers used to create the `.exe` workflow.

## Demo flows

### USB removal

1. Start Mosquitto, the FastAPI server, and the student agent on a lab PC with a tracked USB device configured in `student_agent/config.json`.
2. Show the device as connected in the dashboard.
3. Unplug the device.
4. The server marks it as `WARNING` with `PENDING` status while the debounce timer runs.
5. If it stays unplugged past the configured timeout, the state becomes `CRITICAL`, the dashboard plays an alert sound, and the event is written to the database.

### Bluetooth out of range

1. Configure a Bluetooth device in `student_agent/config.json`.
2. Keep it near the PC so it reports as connected.
3. Move it away until RSSI drops below the configured threshold.
4. The dashboard shows `WEAK_SIGNAL` and then escalates to `CRITICAL` if the device remains out of range long enough.

### Unexpected PC shutdown

1. Confirm the PC appears online in the dashboard PC list.
2. Power off the machine or disconnect it from the network.
3. The MQTT LWT message flips the PC heartbeat to offline without needing a clean agent shutdown.

## Current dashboard behavior

The admin UI now uses a two-step flow:

1. `login.html` authenticates against `POST /auth/login` and stores the returned token in `localStorage`.
2. `index.html` loads the lab list from `GET /labs`, and `dashboard.html?lab=...` shows the selected lab's PCs and devices.

The dashboard currently supports:

- Live PC heartbeat cards with online/offline styling.
- Current device cards with `OK`, `WARNING`, and `CRITICAL` severity states.
- Pending debounce badges for devices waiting to be promoted.
- Recent alerts and event history.
- Per-PC removal from tracking and per-device removal from a PC.
- Auto-refresh plus an audible alert for new critical events.

The dashboard pages map directly to the code:

- `login.html` sends the admin credentials to `POST /auth/login` and redirects to the lab picker when login succeeds.
- `index.html` shows a list of labs returned by `GET /labs`, including device and PC counts.
- `dashboard.html` renders the selected lab's current PC heartbeat list, device grid, and event history.
- `script.js` stores the token under `aats_admin_token`, sends it as `x-admin-token`, and re-renders the page every few seconds.

## MQTT topics

- `aats/lab/{lab_id}/pc/{pc_id}/status`
- `aats/lab/{lab_id}/pc/{pc_id}/event`

## API endpoints

- `GET /health`
- `POST /auth/login`
- `GET /labs`
- `GET /labs/{lab_id}/devices`
- `GET /labs/{lab_id}/pcs`
- `DELETE /labs/{lab_id}/pcs/{pc_id}`
- `DELETE /labs/{lab_id}/pcs/{pc_id}/devices/{device_id}`
- `GET /alerts?from=&to=&severity=&status=`
- `GET /events?lab_id=&pc_id=&device_id=&severity=&status=&from=&to=&limit=`

## Quick start

### Prerequisites

- Python 3.10+
- Mosquitto on the admin machine or a bundled/installed broker available to the admin setup script
- Windows for the packaged `.exe` workflow

NOTE: Before starting the server, create a `.env` file in the repository root
containing at minimum `AATS_ADMIN_USERNAME` and `AATS_ADMIN_PASSWORD` (see the
`Configuration` section). Use `.env.example` as a template and do NOT commit
your `.env` file to git.

### Packaged workflow

1. Build the executables once on each machine:

```powershell
pyinstaller --onefile --uac-admin admin_setup.py
pyinstaller --clean agent_setup.spec
```

2. Run `dist/admin_setup.exe` on the admin PC.
3. Run `dist/agent_setup.exe` on each lab PC.

The admin setup script starts the broker, API, and dashboard. The agent setup script configures the lab PC, writes `student_agent/config.json`, and registers the agent startup mode.

`admin_setup.py` behavior in practice:

- Tries to locate an installed Mosquitto broker first.
- Falls back to a bundled `mqtt_broker/mosquitto.exe` when present.
- Can optionally install Mosquitto with `winget` or a verified download when the relevant environment variables are provided.
- Opens the firewall ports for MQTT, the API, and the dashboard.
- Starts the broker, launches the FastAPI server, serves the dashboard, and opens the browser automatically.
- Broadcasts the admin PC IP so lab agents can discover it during first-run setup.

`agent_setup.py` behavior in practice:

- Waits for the admin IP broadcast for up to 30 seconds, then falls back to manual input.
- Prompts for a PC ID and scans connected USB devices for selection.
- Writes the agent configuration file and chooses a startup mode.
- Uses the Windows service when run as Administrator, otherwise falls back to user registry auto-start.
- On later runs, skips setup and launches the agent directly.

### Manual workflow

```powershell
cd server
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

```powershell
cd student_agent
pip install -r requirements.txt
python main.py
```

Open `admin_dashboard/login.html` in a browser and sign in with the configured admin credentials.

## Configuration

- Agent config: `student_agent/config.json`
- Server env vars: `AATS_HOST`, `AATS_PORT`, `AATS_MQTT_BROKER`, `AATS_MQTT_PORT`, `AATS_DB_PATH`
- Timeout env vars: `AATS_USB_TIMEOUT_SEC`, `AATS_BT_TIMEOUT_SEC`, `AATS_HEARTBEAT_STALENESS_SEC`
- Admin auth env vars: `AATS_ADMIN_USERNAME`, `AATS_ADMIN_PASSWORD`

Notes:
- `AATS_ADMIN_USERNAME` and `AATS_ADMIN_PASSWORD` are required for admin access.
	Do not rely on defaults; create a `.env` file with explicit values before
	launching the server. See `.env.example` for the minimal template.
- `AATS_DB_PATH` should be an absolute path or resolved relative to the
	`server/` package; using the default relative path may create the database in
	an unexpected location if the current working directory differs from the
	repository root. The recommended value is `server/database/aats.db` resolved
	from the server package location.
- `AATS_MQTT_BROKER` / `AATS_MQTT_PORT` must point to an accessible broker.

Example minimal `.env` (DO NOT COMMIT):

```
AATS_ADMIN_USERNAME=admin
AATS_ADMIN_PASSWORD=replace-with-strong-password
AATS_MQTT_BROKER=127.0.0.1
AATS_MQTT_PORT=1883
AATS_DB_PATH=server/database/aats.db
```

Create `.env` from the example and then start the server as shown in
"Manual workflow" below.

## Database model

- `device_events` stores the event history and alert timeline.
- `device_state_current` stores the latest live state per device.
- `pc_heartbeat` stores current PC online/offline state.
- `pending_window` stores debounce windows so they can be restored after a restart.
- `excluded_pcs` stores labs/PCs that have been removed from tracking.
- `excluded_devices` stores individual devices that have been removed from tracking.
- `schema_version` records schema migrations.

The main record fields are intentionally redundant so the dashboard and the report screenshots can be cross-checked against each other:

- `device_events` keeps the timeline with raw device `status`, normalized `severity`, alert lifecycle state, timestamps, and optional `details_json`.
- `device_state_current` keeps the live snapshot with `current_status`, `severity`, `alert_status`, optional `pending_since`, and the last update time.
- `pc_heartbeat` keeps the latest PC state with `pc_status`, `last_seen`, `agent_version`, and `updated_at`.
- `pending_window` keeps debounce scheduling data so a restart does not lose windows that were already in progress.

## How alerting works

- The server converts raw device states into severity values using `server/app.py`.
- USB `MISSING` and Bluetooth `MISSING` or `WEAK_SIGNAL` events enter a pending window first.
- While pending, the dashboard shows `WARNING` and `PENDING`.
- Once the timeout expires, the server promotes the device to `CRITICAL` and records a critical event.
- If a device returns to `CONNECTED`, the alert is closed and the live state returns to `OK`.
- PC heartbeat updates come from the MQTT `status` topic and from the agent's LWT payload when the agent or PC disappears unexpectedly.

The relevant code path is:

1. `student_agent` detects a state change and publishes an MQTT event.
2. `server/mqtt_listener.py` receives the JSON payload and forwards it to `handle_event()` or `handle_status()`.
3. `server/app.py` applies the debounce timeout configured in `server/config.py`.
4. `server/database.py` stores the live state, history row, and any pending-window data.
5. `admin_dashboard/script.js` refreshes the page and renders the updated state for the user.

## Notes for reports

- The dashboard is not a static mockup; it is backed by the FastAPI server and live database queries.
- The current implementation uses a single shared admin account and token header, not per-user roles.
- Pending debounce state is now persisted in the database, while the live in-memory map is still used for runtime scheduling.
- Time synchronization across PCs is still recommended so timestamps line up in screenshots and reports.
- `server/inspect_db.py` is useful for taking screenshots of the database state during demos because it prints a concise text summary for events, device state, and heartbeat.
- `student_agent/main.py` is only a tiny launcher; the real agent runtime lives in `student_agent/service_runner.py`.

## Limitations and future work

- MQTT and HTTP are still unauthenticated at the transport layer unless you add your own TLS and broker credentials.
- The system still depends on the lab PC for USB and Bluetooth sensing, so theft after a full power loss remains a gap.
- SQLite is fine for the current lab-scale deployment but would need to be replaced for larger environments.
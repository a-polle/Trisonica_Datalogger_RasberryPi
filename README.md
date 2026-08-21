# TriSonica Field Datalogger for Raspberry Pi

Autonomous, 24/7 headless data logger and telemetry platform for **TriSonica Mini / Sphere ultrasonic anemometers** on Raspberry Pi (Raspbian Buster / Bullseye / Bookworm).

Designed for unattended outdoor deployments (e.g. rooftop meteorological stations) where power cuts are normal and physical access is restricted.

---

## Key Features

* **10 Hz Real-Time Logging**: Continuous serial streaming with atomic `fsync` every 5 s (bounds data at risk upon power loss to <5 s).
* **Time Provenance**: Chrony/NTP and GPS (`gpsd`) tracking per sample row (`ntp`, `gps`, or `freerun`).
* **Quality & Spike Filtering**: Sentinel value detection and physical acceleration bounds (rejects acoustic glitches while preserving genuine gusts).
* **SD Card Storage Guard**: Stops recording cleanly before the card fills (`<150 MB`), protecting existing datasets and filesystem integrity.
* **Read-Only Web Dashboard (:8080)**:
  * Real-time status verdict (🟢 `Normal` / 🔴 `Problem` with plain-language diagnostics).
  * `/live` real-time streaming feed updating every 2 s.
  * `/data/` individual 6-hour CSV downloads and one-click `/download-all` `.tar.gz` streaming.
  * Sandboxed GET-only operation (`ProtectSystem=strict`, `ProtectHome=read-only`).
* **Automated USB Export**: Insert any USB thumb drive to automatically copy all CSV datasets; status LED indicates progress and safe removal.
* **Dead-Man's Switch Alert**: Automated heartbeat pinging (`trisonica_alert.py`) to notify if recording or reporting stalls.
* **Secondary 24/7 Offsite Backup**: Scheduled pull backup over Tailscale mesh with 100% SHA-256 byte-for-byte replica verification.
* **100% Tested**: Comprehensive 280-test automated test suite.

---

## Architecture & Systemd Services

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            Raspberry Pi 3B+                                 │
├──────────────────────────┬──────────────────────────┬───────────────────────┤
│ trisonica-logger.service │ trisonica-status.service │ trisonica-usb-export  │
│ 10 Hz Serial Logger      │ Web Dashboard & API:8080 │ Auto-mount & USB copy │
│ (fsync, GPS, filters)    │ (Read-only, live stream) │ (LED status codes)    │
└──────────────────────────┴──────────────────────────┴───────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ trisonica-alert.timer    │ Dead-man's switch heartbeat & alarm pings        │
├──────────────────────────┼──────────────────────────────────────────────────┤
│ backup_tools/ (Offsite)  │ 15-minute pull sync to 24/7 secondary storage    │
└──────────────────────────┴──────────────────────────────────────────────────┘
```

---

## Quick Start & Deployment

### 1. Deploying to the Pi
From your development workstation:
```bash
./deploy.sh pi@<pi-hostname-or-ip>
```
`deploy.sh` verifies local tests, stages files on the Pi, executes the 280 tests **on the Pi itself**, promotes verified code atomically, and restarts services only if code changed.

### 2. Running Tests Locally
```bash
python3 test_field_logger.py     # 184 unit tests (logger, time, sentinels, spikes, storage)
python3 test_status_server.py    # 96 unit tests (dashboard, HTTP API, hardening sandbox)
```

---

## Web Dashboard & Remote Access

Access the dashboard via any browser:
* **Local LAN**: `http://<pi-ip>:8080/`
* **Tailscale Mesh**: `http://<pi-tailscale-name>:8080/`

Endpoints:
* `/` — Overall health dashboard and system overview.
* `/live` — 2-second polling live data stream.
* `/data/` — Interactive file browser for individual CSV downloads.
* `/download-all` — On-the-fly streaming compressed archive (`.tar.gz`).
* `/api/status` — JSON health and telemetry payload.
* `/api/live` — Raw text CSV tail of current active samples.

---

## USB Stick Auto-Export & LED Signals

Insert a FAT32/exFAT USB thumb drive:
1. **Slow pulse (1 Hz)**: Export in progress (copying datasets).
2. **Rapid flicker (10 Hz)**: Export complete — safe to unplug.
3. **Double flash**: No USB stick detected or idle.

---

## Secondary Offsite Backup (`backup_tools/`)

The repository includes a production-tested pull backup system for a 24/7 server:
* `sync_trisonica.sh`: Incremental `rsync` with lockfile (`flock`), keepalive, and reaching guards.
* `trisonica-backup.timer`: 15-minute systemd recurring timer with persistent catch-up.
* `test_distributed_resilience.py`: Fault-injection test suite (tests network drops, partial resumes, lock collisions, and SHA-256 integrity).

---

## License

MIT License. See [LICENSE](LICENSE) for details.

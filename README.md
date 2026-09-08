# TriSonica Pi Datalogger

A headless, 24/7 data logger for the **TriSonica Mini** on a Raspberry Pi. Built specifically for rugged outdoor setups where power cuts happen and you can't easily reach the hardware.

## Features

* **10 Hz Logging:** Reads and logs serial data in real-time. It filters out sensor glitches.
* **Live Web Dashboard (Port 8080):** Lightweight Web UI. System status, what the instrument is measuring right now, a live data stream, and CSV downloads.
* **HDMI Status:** A small text screen shows the same essential readings locally without running a desktop or browser.
* **Shareable Link:** Publish the dashboard over Tailscale Funnel — a colleague needs only a URL, no account or client. An unguessable path segment keeps it off the open web; everything else 404s.
* **Auto USB Export:** Plug in a thumb drive. It copies all datasets over and flashes a status LED when it's safe to unplug.
* **Uptime Watchdog:** Pings an external monitor only while healthy, so silence is the alarm — a Pi that dies can't fail to report its own death.
* **Offsite Backups:** A second machine *pulls* over Tailscale on a timer. The Pi holds no credential to the archive, and nothing ever deletes.
* **Server Archive:** The collector can publish its copy through a separate, read-only download page, linked from the live dashboard.
* **Tested:** More than 350 unit and cross-machine integration tests.

## Architecture

On the Pi, as systemd services:
* `trisonica-logger`: The core serial reader and CSV writer.
* `trisonica-status`: The web UI and API server.
* `trisonica-hdmi`: The low-overhead local HDMI status screen.
* `trisonica-usb-export`: Watches for USB drives to trigger the auto-copy.
* `trisonica-alert`: The heartbeat and alerting script.

On the collector (a second, always-on machine): `trisonica-backup.timer`, hourly. See [`backup_tools/`](backup_tools/).

The optional `trisonica-archive` service exposes only the collector's CSV
archive. It shows the last successful backup and offers individual downloads;
the bare host and every path outside its configured private prefix return 404.

## Quick Start

### 1. Deploying
Run this from your computer. It runs the test suite, copies files to your Pi, and restarts only the services that changed.
```bash
./deploy.sh pi@<pi-hostname-or-ip>
```

### 2. Sharing the dashboard
```bash
echo 'PUBLIC_PREFIX=<something-unguessable>' | sudo tee /etc/trisonica-status.conf
sudo systemctl restart trisonica-status
sudo tailscale funnel --bg 8080     # one-time tailnet consent in a browser
```
Everything is then served below `/<PUBLIC_PREFIX>/`. Treat the URL as a capability, not a password: whoever has the link can read the data. Revoke by changing that line and restarting. Leave `PUBLIC_PREFIX` unset for LAN-only.

### 3. Alerting
Create a check at any healthchecks.io-compatible monitor (period 5 min, grace 15 min), then:
```bash
echo 'PING_URL=https://hc-ping.com/your-uuid' | sudo tee /etc/trisonica-alert.conf
sudo chown root:pi /etc/trisonica-alert.conf && sudo chmod 640 /etc/trisonica-alert.conf
sudo systemctl start trisonica-alert.service   # expect: heartbeat sent
```
Ownership matters: the service runs as `pi`, so a root-only `chmod 600` file is invisible to it and alerting stays off while looking configured. The URL must point somewhere *else* — a monitor on the Pi itself is refused.

### 4. Offsite backup
See [`backup_tools/`](backup_tools/). Confine the collector's key on the Pi:
```
command="/usr/local/bin/trisonica-backup-shell",restrict ssh-ed25519 AAAA... collector
```
That's `rrsync -ro /home/pi` — rsync only, read only, no shell.

### 5. Read-only server archive

On the collector, create `/etc/trisonica-archive.conf` with a private path and
the live station link, then deploy and publish the localhost service:

```bash
sudo tee /etc/trisonica-archive.conf >/dev/null <<'EOF'
PUBLIC_PREFIX=<another-unguessable-segment>
STATION_URL=https://station.example/<station-prefix>/
EOF
sudo chmod 0644 /etc/trisonica-archive.conf

./deploy_archive.sh mm12
sudo tailscale funnel --bg --yes 8081
```

To add the archive button on the Pi, set `ARCHIVE_URL` in
`/etc/trisonica-status.conf` and redeploy the Pi dashboard. Treat both URLs as
revocable capabilities and keep them out of source control.

## Layout

`trisonica_field_logger.py` logger · `trisonica_status_server.py` dashboard + API · `trisonica_hdmi_status.py` local screen · `trisonica_alert.py` watchdog · `trisonica_usb_export.py` USB export · `trisonica_backup.py` collector · `trisonica_archive_server.py` server archive · `trisonica-backup-shell` key confinement · `backup_tools/` collector-side helpers · `desktop/` cross-platform desktop logger

## License

See [LICENSE](LICENSE).

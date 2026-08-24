# TriSonica Pi Datalogger

A headless, 24/7 data logger for the **TriSonica Mini** on a Raspberry Pi. Built specifically for rugged outdoor setups where power cuts happen and you can't easily reach the hardware.

## Features

* **10 Hz Logging:** Reads and logs serial data in real-time. It filters out sensor glitches.
* **Live Web Dashboard (Port 8080):** Lightweight Web UI. Check the system status, see what the instrument is measuring right now, watch a live data stream, or download CSV logs.
* **Shareable Read-Only Link:** Publish the dashboard over Tailscale Funnel so a colleague needs nothing but a URL — no account, no client, no VPN. An unguessable path segment keeps it off the open web.
* **Auto USB Export:** Plug in a thumb drive. It automatically copies all your datasets over and flashes a status LED when it's safe to unplug.
* **Uptime Watchdog:** A dead-man's switch. It pings an external monitor only while the station is healthy, so silence is the alarm — a Pi that dies cannot fail to report its own death.
* **Offsite Backups:** A collector on a second machine *pulls* data over Tailscale on a timer. The Pi holds no credential to the archive, and nothing ever deletes.
* **Tested:** 319 unit tests plus 9 cross-machine integration tests, so you don't find bugs the hard way in the field.

## Architecture

On the Pi, everything runs in the background as systemd services:
* `trisonica-logger`: The core serial reader and CSV writer.
* `trisonica-status`: The web UI and API server.
* `trisonica-usb-export`: Watches for USB drives to trigger the auto-copy.
* `trisonica-alert`: The heartbeat and alerting script.

On the collector (a second, always-on machine):
* `trisonica-backup.timer`: Pulls new recordings hourly. See [`backup_tools/`](backup_tools/).

## Quick Start

### 1. Deploying

Run this from your computer. It runs the test suite first, copies the files to
your Pi, and restarts only the services whose files actually changed.

```bash
./deploy.sh pi@<pi-hostname-or-ip>
```

### 2. Watching it from a desk

The dashboard is on port 8080. To publish it as a link anyone can open, set an
unguessable path segment and turn on Tailscale Funnel:

```bash
echo 'PUBLIC_PREFIX=<something-unguessable>' | sudo tee /etc/trisonica-status.conf
sudo systemctl restart trisonica-status
sudo tailscale funnel --bg 8080     # one-time tailnet consent in a browser
```

Everything is then served below `/<PUBLIC_PREFIX>/` and **every other path
returns 404**. Treat the URL as a capability, not a password: whoever has the
link can read the data, exactly like a shared cloud-drive link. Revoke it by
changing that one line and restarting.

Leave `PUBLIC_PREFIX` unset and the dashboard serves at the root — right for a
LAN-only deployment, wrong for anything reachable from the internet.

### 3. Alerting

Create a check at any monitor speaking the healthchecks.io convention (period
5 min, grace 15 min) and give the Pi its URL:

```bash
echo 'PING_URL=https://hc-ping.com/your-uuid' | sudo tee /etc/trisonica-alert.conf
sudo chmod 600 /etc/trisonica-alert.conf
```

The URL has to point somewhere **else**. A monitor on the Pi itself is refused,
because a dead-man's switch wired to the thing it watches can only ever fail
with it.

### 4. Off-site backup

On a second always-on machine, see [`backup_tools/`](backup_tools/). The
collector pulls; the station never pushes. Confine its key on the Pi:

```
command="/usr/local/bin/trisonica-backup-shell",restrict ssh-ed25519 AAAA... collector
```

That forced command is `rrsync -ro /home/pi`, so the key can run rsync and
nothing else, can only read, and cannot get a shell.

## Repository layout

```
trisonica_field_logger.py     the logger itself
trisonica_status_server.py    dashboard + JSON API
trisonica_alert.py            dead-man's switch
trisonica_usb_export.py       USB stick export
trisonica_backup.py           off-site collector (runs on the collector)
trisonica-backup-shell        forced command that confines the collector's key
*.service, *.timer            systemd units
test_*.py                     unit tests
backup_tools/                 collector-side helpers and integration tests
desktop/                      cross-platform desktop logger and plotting
```

`desktop/` is the older cross-platform logger, folded in from a separate
repository so that this is the single place to look.

## License

See [LICENSE](LICENSE).

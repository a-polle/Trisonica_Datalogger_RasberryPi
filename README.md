# TriSonica Pi Datalogger

A headless, 24/7 data logger for the **TriSonica Mini** on a Raspberry Pi. Built specifically for rugged outdoor setups where power cuts happen and you can't easily reach the hardware.

## Features

* **10 Hz Logging:** Reads and logs serial data in real-time. It filters out sensor glitches. 
* **Live Web Dashboard (Port 8080):** Lightweight Web UI. Check the system status, watch a live data stream, or download CSV logs.
* **Auto USB Export:** Plug in a thumb drive. It automatically copies all your datasets over and flashes a status LED when it's safe to unplug.
* **Uptime Watchdog:** Automatically sends heartbeat pings. If the logger crashes or stops recording, you get an alert.
* **Offsite Backups:** A scheduled script pulls data over Tailscale to a secondary server to guarantee you never lose a dataset.
* **Tested:** Backed by 280 automated tests so you don't find bugs the hard way in the field.

## Architecture

Everything runs in the background as systemd services:
* `trisonica-logger`: The core serial reader and CSV writer.
* `trisonica-status`: The web UI and API server.
* `trisonica-usb-export`: Watches for USB drives to trigger the auto-copy.
* `trisonica-alert`: The heartbeat and alerting script.

## Quick Start

### 1. Deploying
Run this from your computer. It will automatically run the test suite, copy the files to your Pi, and restart the services if anything changed.
```bash
./deploy.sh pi@<pi-hostname-or-ip>

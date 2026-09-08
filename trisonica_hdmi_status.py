#!/usr/bin/env python3
"""Show the station's current state on the Raspberry Pi HDMI console.

The recorder and status server remain the sources of truth.  This process only
reads the local JSON status endpoint and paints a small text screen on tty1, so
an HDMI display does not require the desktop or a browser.
"""

import argparse
import json
import os
import re
import signal
import sys
import textwrap
import time
import urllib.error
import urllib.request


DEFAULT_CONFIG = "/etc/trisonica-status.conf"
DEFAULT_BASE_URL = "http://127.0.0.1:8080"
REFRESH_S = 2.0

_running = True


def read_config(path):
    values = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except (IOError, OSError):
        pass
    return values


def status_url(config_path=DEFAULT_CONFIG, base=DEFAULT_BASE_URL):
    prefix = read_config(config_path).get("PUBLIC_PREFIX", "").strip().strip("/")
    if prefix and not re.match(r"^[A-Za-z0-9_-]+$", prefix):
        raise ValueError("invalid dashboard prefix")
    path = "/%s/api/status" % prefix if prefix else "/api/status"
    return base.rstrip("/") + path


def fetch_status(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (IOError, OSError, ValueError, urllib.error.URLError):
        return None


def _number(item, digits=1):
    if not item or item.get("value") is None:
        return "--"
    return ("%%.%df" % digits) % item["value"]


def _age(seconds):
    if seconds is None:
        return "--"
    if seconds < 60:
        return "%d seconds" % max(0, int(seconds))
    return "%d minutes" % int(seconds / 60)


def render(status, width=80):
    """Return one simple, terminal-sized status screen."""
    if not status:
        return "\n".join([
            "TRISONICA WEATHER STATION",
            "=" * 27,
            "",
            "STATUS TEMPORARILY UNAVAILABLE",
            "",
            "Retrying automatically...",
        ])

    health = status.get("health") or {}
    level = health.get("level", "bad")
    heading = {
        "ok": "RECORDING NORMALLY",
        "warn": "ATTENTION NEEDED",
        "bad": "NOT RECORDING",
    }.get(level, "STATUS UNKNOWN")

    values = (status.get("reading") or {}).get("values") or {}
    logger = status.get("logger") or {}
    disk = status.get("disk") or {}

    lines = [
        "TRISONICA WEATHER STATION",
        "=" * 27,
        "",
        heading,
        "",
        "Wind speed   %8s m/s    Direction   %8s deg" %
        (_number(values.get("S"), 2), _number(values.get("D"), 0)),
        "Temperature  %8s C      Humidity    %8s %%" %
        (_number(values.get("T"), 1), _number(values.get("H"), 0)),
        "Pressure     %8s hPa" % _number(values.get("P"), 0),
        "",
        "Last reading  %s ago" % _age(health.get("data_age_s")),
        "Sample rate   %s Hz" %
        ("%.2f" % logger["sample_rate_hz"]
         if logger.get("sample_rate_hz") is not None else "--"),
        "Flagged data  %s" %
        ("%.2f%%" % logger["bad_pct"]
         if logger.get("bad_pct") is not None else "--"),
        "Storage       %s MB free, about %s days" %
        ("%.0f" % disk["free_mb"] if disk.get("free_mb") is not None else "--",
         "%.0f" % disk["estimated_days"]
         if disk.get("estimated_days") is not None else "--"),
    ]

    reasons = health.get("reasons") or []
    if reasons:
        lines.extend(["", "What needs attention:"])
        for reason in reasons:
            lines.extend(textwrap.wrap("- " + str(reason), width=max(30, width),
                                       subsequent_indent="  "))

    timestamp = (status.get("system") or {}).get("timestamp_utc")
    if timestamp:
        lines.extend(["", "Updated %s" % timestamp])
    return "\n".join(lines)


def _stop(_signum, _frame):
    global _running
    _running = False


def main():
    parser = argparse.ArgumentParser(description="TriSonica HDMI status screen")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--status-url")
    parser.add_argument("--interval", type=float, default=REFRESH_S)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    try:
        url = args.status_url or status_url(args.config)
    except ValueError:
        url = ""

    if args.once:
        print(render(fetch_status(url) if url else None))
        return 0

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _stop)

    while _running:
        status = fetch_status(url) if url else None
        sys.stdout.write("\033[2J\033[H" + render(status) + "\n")
        sys.stdout.flush()
        time.sleep(max(0.5, args.interval))
    return 0


if __name__ == "__main__":
    sys.exit(main())

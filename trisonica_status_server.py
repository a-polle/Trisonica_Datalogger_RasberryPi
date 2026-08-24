#!/usr/bin/env python3
"""Read-only status dashboard and data file server for the TriSonica logger.

Serves a lightweight web interface so a researcher on the same LAN can:

    http://pi-ip:8080/            status dashboard
    http://pi-ip:8080/data/       file listing with download links
    http://pi-ip:8080/download-all  every CSV as one streamed .tar.gz
    http://pi-ip:8080/live        the newest rows, as they are recorded
    http://pi-ip:8080/api/status  JSON health snapshot, including a verdict

No SSH, no terminal, no Tailscale required.  Open a browser, bookmark, done.

Read-only by construction: GET is the only method implemented, the data
directory and system state are only ever read, and requested filenames are
reduced to their basename before use.  Nothing here can change the logger,
its data or its code; the systemd unit enforces the same thing from outside
in case this file is ever wrong.

The dashboard's verdict comes first from how long ago the newest data file
last grew.  Everything else -- service states, journal lines -- is a component
reporting on itself, and the failure that matters most (anemometer unplugged)
leaves the logger service 'active' and its last journal line claiming 10 Hz
forever.

Targets Python 3.7 (Raspbian Buster) -- stdlib only, no pip dependencies.
"""

import argparse
import datetime
import glob
import html
import http.server
import json
import logging
import os
import re
import signal
import socketserver
import subprocess
import sys
import tarfile
import threading
import time
from urllib.parse import quote as _url_quote
from urllib.parse import unquote as _url_unquote

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_PORT = 8080
DEFAULT_DATA_DIR = "/home/pi/trisonica-data"
DEFAULT_BIND = "0.0.0.0"

DEFAULT_CONFIG = "/etc/trisonica-status.conf"

# Every page and link is served below this, when it is set:
#
#     PUBLIC_PREFIX=k7f2p9x4m1   ->  http://host:8080/k7f2p9x4m1/
#
# It exists because of how this station is reached from outside. Tailscale
# Funnel gives the dashboard a public HTTPS address so a colleague needs
# nothing but a link -- no account, no client, no VPN -- and the cost of that
# convenience is that the address is also reachable by everyone else, and
# Funnel hostnames appear in public certificate-transparency logs. An
# unguessable path segment is what stands between "Ole can open this on his
# phone" and "this is a public website".
#
# It is not authentication and is not treated as any. It is a capability in a
# URL: whoever has the link has read access, exactly like a shared cloud-drive
# link, and it is revoked by changing one line and restarting. What it buys is
# that the station is not *discoverable* -- a scanner that finds the hostname
# gets 404 on every path it knows how to try.
#
# Unset (the default) the server behaves as it always has, which is what the
# LAN and Tailscale-only deployments want.
LINK_PREFIX = ""

# Approximate data rate for estimating remaining recording time.
# Measured on this deployment: ~149 bytes/row at 10 Hz = ~122 MB/day.
MB_PER_DAY = 122.0

# Services whose state is reported on the dashboard.  These are the same
# services that deploy.sh checks in its verification step.
MONITORED_SERVICES = (
    "trisonica-logger",
    "trisonica-usb-export",
    "gpsd",
    "chrony",
    "tailscaled",
)

# How long to cache gathered status (seconds).  Keeps repeated browser
# refreshes from hammering systemctl and journalctl on a Pi 3B+.
CACHE_TTL_S = 10

# Dashboard auto-refresh interval (seconds).
REFRESH_INTERVAL_S = 60

# Subprocess timeout for status queries (seconds).
SUBPROCESS_TIMEOUT_S = 10

# How long the newest data file may go unwritten before recording counts as
# stopped.  The logger flushes every 5 s, so anything past a couple of minutes
# means no measurements are arriving.  This is the only health check that does
# not rely on a component reporting on itself: the logger service stays
# 'active' by design while it waits for an anemometer that is never coming
# back, and its last journal line then keeps claiming 10 Hz indefinitely.
DATA_STALE_S = 120.0

# The logger prints a status line every 5 minutes (STATUS_LOG_INTERVAL_S in
# trisonica_field_logger.py).  Past two intervals plus slack, the numbers
# parsed out of it are history and must not be shown as if they were current.
STATUS_STALE_S = 660.0

# How many rows the live view shows.
LIVE_ROWS = 25

log = logging.getLogger("trisonica-status")


# ---------------------------------------------------------------------------
# Public prefix
# ---------------------------------------------------------------------------

def read_config(path):
    """Parse KEY=VALUE lines. Missing or unreadable file yields {}."""
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
        return {}
    return values


def normalize_prefix(raw):
    """Return '' or '/segment', rejecting anything that is not one segment.

    Deliberately strict. A prefix arrives from a config file that a tired
    person edits over SSH, and the failure modes of a sloppy one are bad in
    both directions: '/' or '' would silently publish the whole dashboard,
    while a value containing a slash or a '..' would produce links that do not
    match the routes and a station that appears broken from outside.
    """
    token = (raw or "").strip().strip("/")
    if not token:
        return ""
    if not re.match(r"^[A-Za-z0-9._~-]+$", token):
        raise ValueError(
            "PUBLIC_PREFIX must be a single path segment of letters, digits, "
            "'.', '_', '~' or '-'; got %r" % raw)
    # '.' and '..' match the pattern above but are relative-path components,
    # not names. A browser resolves them away before the request is sent, so
    # the link would never arrive at the route it was generated for.
    if set(token) == {"."}:
        raise ValueError("PUBLIC_PREFIX cannot be %r" % raw)
    return "/" + token


def _link(path):
    """Absolute URL for an in-app path, honouring the public prefix.

    Every link the dashboard emits goes through here. Relative links would
    have been an alternative, but they behave differently on '/data' and
    '/data/' and the difference only shows up in a browser -- this is the
    version that is obviously right when read.
    """
    if path == "/":
        return LINK_PREFIX + "/" if LINK_PREFIX else "/"
    return LINK_PREFIX + path


# ---------------------------------------------------------------------------
# Status gathering -- each function returns a dict and never raises
# ---------------------------------------------------------------------------

def _run(cmd, timeout=SUBPROCESS_TIMEOUT_S):
    """Run a command and return its stripped stdout, or '' on any failure."""
    proc = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = proc.communicate(timeout=timeout)
        return out.decode("utf-8", "replace").strip()
    except subprocess.TimeoutExpired:
        # communicate() raises but leaves the child running. Without the kill
        # a wedged journalctl would survive every request that timed out, and
        # a browser refreshing every 60 s would keep spawning more.
        try:
            proc.kill()
            proc.communicate(timeout=1)
        except Exception:
            pass
        return ""
    except Exception:
        return ""


def _tail_lines(path, count):
    """Return the last *count* lines of a text file, oldest first.

    Reads from the end rather than shelling out to tail: the live view polls
    every couple of seconds, and a fork per poll is real work on a Pi 3B+.
    """
    block = 8192
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            data = b""
            while end > 0 and data.count(b"\n") <= count:
                step = min(block, end)
                end -= step
                fh.seek(end)
                data = fh.read(step) + data
    except (IOError, OSError):
        return []
    text = data.decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # A row the logger is halfway through writing has no newline yet. Showing
    # it would put a truncated line on the live page every few seconds.
    if data and not data.endswith(b"\n") and lines:
        lines = lines[:-1]
    # Reading backwards in blocks lands mid-row, so the first line of a
    # partial read is a fragment of one.
    if end > 0 and lines:
        lines = lines[1:]
    return lines[-count:]


def _fmt_age(seconds):
    """Format an age in seconds the way a person would say it."""
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 60:
        return "%d s" % seconds
    if seconds < 3600:
        return "%d min" % (seconds // 60)
    if seconds < 86400:
        return "%d h %d min" % (seconds // 3600, (seconds % 3600) // 60)
    return "%d d %d h" % (seconds // 86400, (seconds % 86400) // 3600)


def get_service_states():
    """Return {service_name: 'active' | 'inactive' | 'failed' | ...}.

    All services are queried in a single systemctl call to minimise
    subprocess overhead on the Pi.
    """
    text = _run(["systemctl", "is-active"] + list(MONITORED_SERVICES))
    lines = text.splitlines() if text else []
    states = {}
    for i, svc in enumerate(MONITORED_SERVICES):
        states[svc] = lines[i].strip() if i < len(lines) else "unknown"
    return states


def get_disk_info(data_dir):
    """Return disk-space metrics for the partition holding *data_dir*."""
    try:
        st = os.statvfs(data_dir)
    except OSError:
        return {}
    free_mb = (st.f_bavail * st.f_frsize) / (1024.0 * 1024.0)
    total_mb = (st.f_blocks * st.f_frsize) / (1024.0 * 1024.0)
    return {
        "free_mb": round(free_mb, 1),
        "total_mb": round(total_mb, 1),
        "used_mb": round(total_mb - free_mb, 1),
        "free_pct": round(100.0 * free_mb / total_mb, 1) if total_mb > 0 else 0,
        "estimated_days": round(free_mb / MB_PER_DAY, 1) if MB_PER_DAY > 0 else 0,
    }


def get_data_files(data_dir):
    """Return a list of dicts describing each CSV in *data_dir*, oldest first."""
    result = []
    try:
        paths = sorted(glob.glob(os.path.join(data_dir, "*.csv")))
    except Exception:
        return result
    for path in paths:
        try:
            st = os.stat(path)
            result.append({
                "name": os.path.basename(path),
                "size_bytes": st.st_size,
                "mtime_epoch": st.st_mtime,
                "mtime_utc": datetime.datetime.utcfromtimestamp(
                    st.st_mtime).strftime("%Y-%m-%d %H:%M UTC"),
            })
        except OSError:
            continue
    return result


def get_recording_info(files):
    """Return which file is being written and when it last grew.

    Newest by modification time, not by name: a file whose name sorts last is
    not necessarily the one receiving rows after a clock correction, and this
    number decides whether the dashboard shows green.
    """
    newest = None
    for entry in files:
        stamp = entry.get("mtime_epoch")
        if stamp is None:
            continue
        if newest is None or stamp > newest.get("mtime_epoch", 0):
            newest = entry
    if newest is None:
        return {}
    return {
        "newest_file": newest["name"],
        "newest_mtime_epoch": newest["mtime_epoch"],
        "newest_size_bytes": newest.get("size_bytes", 0),
    }


# What the instrument sends, and what a person reading it would call it. Only
# the fields worth putting in front of someone deciding about a site visit --
# the wind itself, and the three ambient channels that reveal a head that has
# stopped sensing properly.
READING_FIELDS = (
    ("S", "wind speed", "m/s", 2),
    ("D", "direction", "°", 0),
    ("T", "temperature", "°C", 1),
    ("H", "humidity", "%", 0),
    ("P", "pressure", "hPa", 0),
)


def get_latest_reading(data_dir, files=None):
    """Parse the newest recorded row into named measurements.

    This is the one thing the dashboard could not previously answer. Every
    other check on the page establishes that the machinery is running -- the
    service is up, the file is growing, the rate is 10 Hz -- and all of them
    stay green when the anemometer is reporting nonsense. An iced or
    spider-webbed head still produces rows at 10.00 Hz.

    So the numbers go on the page and the judgement stays with the reader: a
    researcher who sees 0.00 m/s on a windy afternoon, or a temperature that
    has not moved in a day, knows something the health verdict cannot infer.
    """
    if files is None:
        files = get_data_files(data_dir)
    info = get_recording_info(files)
    name = info.get("newest_file")
    if not name:
        return {}
    path = os.path.join(data_dir, name)

    try:
        with open(path, "r") as fh:
            header = fh.readline().strip()
    except (IOError, OSError):
        return {}
    if not header:
        return {}
    columns = header.split(",")

    rows = _tail_lines(path, 1)
    if not rows or rows[0].strip() == header:
        return {}
    values = rows[0].split(",")
    if len(values) != len(columns):
        # A row caught mid-write. Not an error worth reporting: the next
        # refresh is a second away and the file is fine.
        return {}
    row = dict(zip(columns, values))

    reading = {}
    for key, label, unit, digits in READING_FIELDS:
        raw = (row.get(key) or "").strip()
        try:
            number = float(raw)
        except ValueError:
            continue
        # The instrument's own failure sentinel. Showing -99.9 as a
        # temperature would be worse than showing nothing at all.
        if number <= -99.0:
            continue
        reading[key] = {"label": label, "unit": unit,
                        "value": round(number, digits)}

    stamp = (row.get("timestamp_utc") or "").strip()
    return {
        "values": reading,
        "timestamp_utc": stamp,
        "time_synced": (row.get("time_synced") or "").strip() == "1",
        "flags": (row.get("flags") or "").strip(),
        "source_file": name,
    }


def data_age_s(status):
    """Seconds since the newest data file last grew, or None if there is none.

    Computed against the wall clock at call time rather than stored in the
    status dict, so a cached snapshot never reports an age that has stopped
    advancing.
    """
    stamp = status.get("recording", {}).get("newest_mtime_epoch")
    if stamp is None:
        return None
    return max(0.0, time.time() - stamp)


def _parse_status_line(line):
    """Extract structured fields from a logger status line."""
    fields = {}
    m = re.search(r"(\d+) rows", line)
    if m:
        fields["total_rows"] = int(m.group(1))
    m = re.search(r"([\d.]+) Hz", line)
    if m:
        fields["sample_rate_hz"] = float(m.group(1))
    m = re.search(r"([\d.]+)% bad", line)
    if m:
        fields["bad_pct"] = float(m.group(1))
    m = re.search(r"gps=(\w+)", line)
    if m:
        fields["gps_state"] = m.group(1)
    m = re.search(r"sats=(\d+)", line)
    if m:
        fields["gps_sats"] = int(m.group(1))
    m = re.search(r"time=(\w+)\(synced=(\w+)\)", line)
    if m:
        fields["time_source"] = m.group(1)
        fields["time_synced"] = m.group(2) == "True"
    m = re.search(r"([\d.]+) MB free", line)
    if m:
        fields["logger_free_mb"] = float(m.group(1))
    return fields


def _parse_log_time(line):
    """Epoch seconds from a logger line's leading timestamp, or None.

    The logger formats with %(asctime)s, which is local time; time.mktime
    interprets the parsed struct as local time too, so the pair stays correct
    whatever the unit's timezone is set to.
    """
    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def get_logger_status():
    """Parse the latest logger status from journalctl output."""
    text = _run([
        "journalctl", "-u", "trisonica-logger",
        "-n", "100", "--no-pager", "-o", "cat",
    ])
    if not text:
        return {}

    result = {}
    all_lines = text.splitlines()

    # Find the most recent status line (they appear every 5 minutes).
    for line in reversed(all_lines):
        if "status:" in line and "Hz" in line:
            result["status_line"] = line.strip()
            result["status_time_epoch"] = _parse_log_time(line)
            result.update(_parse_status_line(line))
            break

    # Keep the last several log lines for context.
    recent = []
    for line in all_lines[-15:]:
        stripped = line.strip()
        if stripped:
            recent.append(stripped)
    if recent:
        result["recent_log"] = recent

    return result


def get_system_info():
    """Return hostname, uptime and timestamps."""
    return {
        "hostname": _run(["hostname"]) or "unknown",
        "uptime": _run(["uptime", "-p"]) or "unknown",
        "timestamp_utc": datetime.datetime.utcnow().strftime(
            "%Y-%m-%d %H:%M:%S UTC"),
    }


def gather_status(data_dir):
    """Assemble all status information into a single dict."""
    files = get_data_files(data_dir)
    return {
        "system": get_system_info(),
        "services": get_service_states(),
        "disk": get_disk_info(data_dir),
        "logger": get_logger_status(),
        "recording": get_recording_info(files),
        "reading": get_latest_reading(data_dir, files),
        "files": files,
    }


def status_age_s(status):
    """Seconds since the logger last printed a status line, or None."""
    stamp = status.get("logger", {}).get("status_time_epoch")
    if stamp is None:
        return None
    return max(0.0, time.time() - stamp)


def health_report(status):
    """Return ('ok'|'warn'|'bad', [reasons]) for the whole unit.

    The three levels map to green, yellow and red in the dashboard header.
    Green means stop looking; yellow means something deserves attention
    before the next visit; red means measurements are being lost now and a
    trip to the roof is warranted.

    Reasons are returned in plain language because the person reading them
    decides from this page alone whether to book building maintenance.
    """
    services = status.get("services", {})
    li = status.get("logger", {})   # logger info
    disk = status.get("disk", {})
    bad = []
    warn = []

    # --- Is anything actually being recorded? ---
    # First, because it is the question being asked, and because it is the
    # only check that does not depend on a component reporting on itself.
    age = data_age_s(status)
    if age is None:
        bad.append("No data file has ever been written - the anemometer has "
                   "never been connected")
    elif age > DATA_STALE_S:
        bad.append("Nothing recorded for %s - check the anemometer's USB "
                   "connection" % _fmt_age(age))

    if services.get("trisonica-logger") != "active":
        bad.append("The logger service is not running (%s)"
                   % services.get("trisonica-logger", "unknown"))

    free = disk.get("free_mb", -1)
    if 0 <= free < 150:
        bad.append("The card is full - measurements are being lost")
    elif 0 < free < 854:  # ~7 days at 122 MB/day
        warn.append("The card is nearly full - about %.0f days left"
                    % disk.get("estimated_days", 0))

    # --- Numbers from the journal, only while they are still current ---
    stale = status_age_s(status)
    if li.get("status_line") and stale is not None and stale > STATUS_STALE_S:
        warn.append("The logger has not reported in for %s - the measurement "
                    "figures below are that old" % _fmt_age(stale))
    else:
        rate = li.get("sample_rate_hz")
        if rate is not None and rate < 5.0:
            warn.append("Sample rate is %.1f Hz, well below the expected 10 Hz"
                        % rate)
        bad_pct = li.get("bad_pct")
        if bad_pct is not None and bad_pct > 1.0:
            warn.append("%.1f%% of readings are flagged bad - check the "
                        "measurement heads for obstructions" % bad_pct)
        if li.get("time_synced") is False:
            warn.append("The clock is not verified - timestamps in the data "
                        "are marked untrustworthy")

    if services.get("trisonica-usb-export") != "active":
        warn.append("USB export is not running - a stick plugged in on site "
                    "will not be copied to")

    if bad:
        return "bad", bad + warn
    if warn:
        return "warn", warn
    return "ok", []


def overall_health(status):
    """Classify system health as 'ok', 'warn', or 'bad'."""
    return health_report(status)[0]


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_STYLESHEET = """\
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
  Helvetica,Arial,sans-serif;margin:0;padding:16px;background:#f5f5f5;
  color:#333;line-height:1.5}
.ctr{max-width:900px;margin:0 auto}
h1{margin:0 0 4px;font-size:1.5em}
h2{margin:18px 0 8px;font-size:1.15em;border-bottom:1px solid #ddd;
  padding-bottom:4px}
.hdr{background:#fff;padding:16px 20px;border-radius:8px;
  margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
.card{background:#fff;padding:14px 20px;border-radius:8px;
  margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
.ok{border-left:4px solid #4caf50}
.warn{border-left:4px solid #ff9800}
.bad{border-left:4px solid #f44336}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  gap:12px;margin:8px 0}
.m{text-align:center}
.m .v{font-size:1.8em;font-weight:700}
.m .l{font-size:.82em;color:#666}
.svcs{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}
.s{display:inline-flex;align-items:center;gap:5px;padding:4px 12px;
  border-radius:16px;font-size:.88em}
.sa{background:#e8f5e9;color:#2e7d32}
.si{background:#fff3e0;color:#e65100}
.sf{background:#ffebee;color:#c62828}
.su{background:#f5f5f5;color:#757575}
.d{width:7px;height:7px;border-radius:50%;display:inline-block}
.dg{background:#4caf50}.do{background:#ff9800}
.dr{background:#f44336}.dy{background:#bdbdbd}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #eee}
th{font-weight:600;color:#666;font-size:.82em;text-transform:uppercase}
tr:hover{background:#f9f9f9}
.r{text-align:right}
a{color:#1565c0;text-decoration:none}
a:hover{text-decoration:underline}
.log{font-family:"SF Mono",Consolas,"Liberation Mono",monospace;
  font-size:.78em;background:#263238;color:#e0e0e0;padding:12px;
  border-radius:6px;overflow-x:auto;white-space:pre-wrap;
  word-break:break-all;line-height:1.6}
.ft{text-align:center;color:#999;font-size:.78em;margin-top:16px}
.nav a{margin-right:14px}
.btn{display:inline-block;padding:7px 14px;background:#1565c0;color:#fff;
  border-radius:4px;text-decoration:none;font-size:.88em}
.btn:hover{background:#0d47a1;text-decoration:none}
.hm{font-size:.95em;margin:4px 0 0;font-weight:600}
.hok{color:#2e7d32}.hwn{color:#e65100}.hbd{color:#c62828}
.why{margin:6px 0 0;padding-left:20px}
.why li{margin:3px 0}
.stale{color:#e65100;font-size:.85em;margin:2px 0 0}
"""


def _page(title, body, refresh=0):
    """Wrap *body* HTML in a complete page."""
    meta = ""
    if refresh > 0:
        meta = '<meta http-equiv="refresh" content="%d">' % refresh
    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "%s\n"
        "<title>%s</title>\n"
        "<style>%s</style>\n"
        "</head>\n<body>\n"
        '<div class="ctr">\n%s\n</div>\n'
        "</body>\n</html>\n"
    ) % (meta, html.escape(title), _STYLESHEET, body)


def _fmt_size(size_bytes):
    """Format a byte count as a human-readable string."""
    if size_bytes < 1024:
        return "%d B" % size_bytes
    if size_bytes < 1024 * 1024:
        return "%.1f KB" % (size_bytes / 1024.0)
    if size_bytes < 1024 * 1024 * 1024:
        return "%.1f MB" % (size_bytes / (1024.0 * 1024.0))
    return "%.2f GB" % (size_bytes / (1024.0 * 1024.0 * 1024.0))


def _svc_css(state):
    """Return the CSS class pair (container, dot) for a service state."""
    if state == "active":
        return "sa", "dg"
    if state == "failed":
        return "sf", "dr"
    if state in ("inactive", "deactivating"):
        return "si", "do"
    return "su", "dy"


def _health_msg(health):
    """Return (css_class, message) for the overall health state."""
    if health == "ok":
        return "hok", "Status: Normal"
    if health == "warn":
        return "hwn", "Status: Warning \u2014 attention needed"
    return "hbd", "Status: Error \u2014 measurements stopped"


def render_dashboard(status):
    """Render the status dashboard page as an HTML string."""
    system = status.get("system", {})
    services = status.get("services", {})
    disk = status.get("disk", {})
    li = status.get("logger", {})   # logger info
    files = status.get("files", [])

    health, reasons = health_report(status)
    hcss, hmsg = _health_msg(health)
    p = []  # parts

    # --- Header ---
    p.append('<div class="hdr %s">' % health)
    p.append("<h1>TriSonica Field Logger</h1>")
    p.append("<p>%s &mdash; %s</p>" % (
        html.escape(system.get("hostname", "?")),
        html.escape(system.get("uptime", "?"))))
    p.append('<p class="hm %s">%s</p>' % (hcss, html.escape(hmsg)))
    if reasons:
        p.append('<ul class="why">')
        for reason in reasons:
            p.append("<li>%s</li>" % html.escape(reason))
        p.append("</ul>")
    p.append("</div>")

    # --- Overview ---
    p.append('<div class="card">')
    p.append("<h2>Overview</h2>")
    p.append('<div class="svcs">')
    for svc in MONITORED_SERVICES:
        state = services.get(svc, "unknown")
        sc, dc = _svc_css(state)
        p.append('<span class="s %s"><span class="d %s"></span>%s</span>'
                 % (sc, dc, html.escape(svc)))
    p.append("</div></div>")

    # --- What the instrument is actually measuring ---
    #
    # Above the machinery on purpose. Someone who opens this page from a desk
    # wants to know what the weather is doing on that roof; that it is doing
    # it at 10 Hz is the next question, not the first.
    reading = status.get("reading") or {}
    values = reading.get("values") or {}
    if values:
        p.append('<div class="card">')
        p.append("<h2>Current Conditions</h2>")
        p.append('<div class="grid">')
        for key, _label, unit, digits in READING_FIELDS:
            item = values.get(key)
            if not item:
                continue
            shown = ("%%.%df" % digits) % item["value"]
            p.append('<div class="m"><div class="v">%s</div>'
                     '<div class="l">%s (%s)</div></div>'
                     % (html.escape(shown), html.escape(item["label"]),
                        html.escape(unit)))
        p.append("</div>")  # grid

        stamp = reading.get("timestamp_utc")
        note = []
        if stamp:
            note.append("Measured %s." % html.escape(stamp))
        if not reading.get("time_synced", True):
            note.append("The clock was not verified when this row was "
                        "written, so its timestamp is unreliable — the "
                        "measurement itself is fine.")
        if reading.get("flags"):
            note.append("This row carries quality flags: <code>%s</code>."
                        % html.escape(reading["flags"]))
        if note:
            p.append("<p>%s</p>" % " ".join(note))
        p.append("</div>")

    # --- Measurement ---
    p.append('<div class="card">')
    p.append("<h2>Measurement</h2>")
    p.append('<div class="grid">')

    # Age of the newest data first: it answers "is it still recording?"
    # without trusting any component's own account of itself.
    age = data_age_s(status)
    p.append('<div class="m"><div class="v">%s</div>'
             '<div class="l">since last data</div></div>'
             % (html.escape(_fmt_age(age)) if age is not None else "never"))

    # Everything below comes from the logger's 5-minutely journal line. When
    # that line is old the figures are history, and showing a stale "10.00 Hz"
    # is worse than showing nothing.
    stale = status_age_s(status)
    outdated = stale is not None and stale > STATUS_STALE_S

    rate = li.get("sample_rate_hz")
    p.append('<div class="m"><div class="v">%s</div>'
             '<div class="l">Hz sample rate</div></div>'
             % ("%.2f" % rate if rate is not None and not outdated
                else "&mdash;"))

    rows = li.get("total_rows")
    p.append('<div class="m"><div class="v">%s</div>'
             '<div class="l">rows this session</div></div>'
             % ("{:,}".format(rows) if rows is not None else "&mdash;"))

    bad = li.get("bad_pct")
    p.append('<div class="m"><div class="v">%s</div>'
             '<div class="l">flagged bad</div></div>'
             % ("%.2f%%" % bad if bad is not None and not outdated
                else "&mdash;"))

    p.append("</div>")  # grid

    if outdated:
        p.append('<p class="stale">The logger last reported %s ago; '
                 "the figures above are from then.</p>" % _fmt_age(stale))

    # Time & GPS inline
    ts = li.get("time_source", "?")
    synced = li.get("time_synced")
    gps = li.get("gps_state", "?")
    sats = li.get("gps_sats", 0)
    sync_mark = " \u2713" if synced else (" \u2717" if synced is not None else "")
    p.append("<p>Clock: <strong>%s</strong> (synced%s) &middot; "
             "GPS: <strong>%s</strong> (%d sats)</p>"
             % (html.escape(str(ts)), sync_mark,
                html.escape(str(gps)), sats))

    # Say what the GPS state means for the data, because the words above are
    # the instrument's and the reader is deciding whether to climb to a roof.
    # Deliberately not a health warning: with the network supplying the clock
    # nothing is wrong with the recording, and escalating this would send an
    # email every day about a condition that is stable and not urgent.
    if gps == "nofix":
        if str(ts) == "gps":
            p.append('<p class="stale">The GPS has lost its fix. Timestamps '
                     "are still being written, but nothing is verifying them "
                     "any more.</p>")
        else:
            p.append("<p>The GPS has no fix, so rows carry no position and "
                     "the clock is coming from the network instead. "
                     "Timestamps are still verified. Worth checking the "
                     "antenna and its view of the sky on the next visit.</p>")
    p.append("</div>")

    # --- Storage ---
    p.append('<div class="card">')
    p.append("<h2>Storage</h2>")
    p.append('<div class="grid">')

    free = disk.get("free_mb")
    p.append('<div class="m"><div class="v">%s</div>'
             '<div class="l">free space</div></div>'
             % ("{:,.0f} MB".format(free) if free is not None else "&mdash;"))

    days = disk.get("estimated_days")
    p.append('<div class="m"><div class="v">%s</div>'
             '<div class="l">days left</div></div>'
             % ("~%.0f" % days if days is not None else "&mdash;"))

    total_bytes = sum(f.get("size_bytes", 0) for f in files)
    p.append('<div class="m"><div class="v">%d</div>'
             '<div class="l">files (%s)</div></div>'
             % (len(files), _fmt_size(total_bytes)))

    p.append("</div>")  # grid
    p.append('<p><a href="%s">Browse and download data files &rarr;</a></p>'
             % _link("/data/"))
    p.append('<p><a href="%s">View live data feed &rarr;</a></p>'
             % _link("/live"))
    p.append("</div>")

    # --- Recent log ---
    recent = li.get("recent_log", [])
    if recent:
        p.append('<div class="card">')
        p.append("<h2>Recent Log</h2>")
        p.append('<div class="log">')
        p.append("\n".join(html.escape(line) for line in recent))
        p.append("</div></div>")

    # --- Footer ---
    p.append('<p class="ft">Page generated %s &middot; '
             "Auto-refreshes every %d seconds &middot; "
             '<a href="%s">JSON API</a></p>'
             % (html.escape(system.get("timestamp_utc", "?")),
                REFRESH_INTERVAL_S, _link("/api/status")))

    return _page(
        "TriSonica Status \u2014 %s" % system.get("hostname", "?"),
        "\n".join(p),
        refresh=REFRESH_INTERVAL_S,
    )


def render_file_listing(files):
    """Render the data file listing page as an HTML string."""
    p = []
    p.append('<div class="nav">')
    p.append('<a href="%s">&larr; Back to status</a>' % _link("/"))
    p.append("</div>")

    p.append('<div class="hdr">')
    p.append("<h1>Data Files</h1>")
    total_bytes = sum(f.get("size_bytes", 0) for f in files)
    p.append("<p>%d files, %s total</p>" % (len(files), _fmt_size(total_bytes)))
    if files:
        p.append('<p><a href="%s" class="btn">' % _link("/download-all") +
                 '\u2b07 Download all as .tar.gz</a></p>')
    p.append("</div>")

    if not files:
        p.append('<div class="card">')
        p.append("<p>No data files yet.  The logger creates a new file "
                 "when the anemometer is connected.</p>")
        p.append("</div>")
    else:
        p.append('<div class="card">')
        p.append("<table>")
        p.append("<thead><tr>"
                 "<th>File</th>"
                 '<th class="r">Size</th>'
                 "<th>Modified</th>"
                 "</tr></thead>")
        p.append("<tbody>")
        for f in reversed(files):  # newest first
            name_escaped = html.escape(f["name"])
            href = _link("/data/%s" % _url_quote(f["name"], safe=""))
            p.append(
                "<tr>"
                '<td><a href="%s">%s</a></td>'
                '<td class="r">%s</td>'
                "<td>%s</td>"
                "</tr>" % (
                    html.escape(href), name_escaped,
                    _fmt_size(f.get("size_bytes", 0)),
                    html.escape(f.get("mtime_utc", "?"))))
        p.append("</tbody></table>")
        p.append("</div>")

    return _page("TriSonica Data Files", "\n".join(p))


def live_text(data_dir):
    """Return the newest rows of the file being written, header included.

    Plain CSV rather than a rendered table: the same bytes then serve the
    browser view and anyone pointing curl or a script at /api/live.
    """
    info = get_recording_info(get_data_files(data_dir))
    name = info.get("newest_file")
    if not name:
        return "No data files yet."
    path = os.path.join(data_dir, name)
    rows = _tail_lines(path, LIVE_ROWS)
    if not rows:
        return "%s is empty." % name
    header = ""
    try:
        with open(path, "r") as fh:
            header = fh.readline().strip()
    except (IOError, OSError):
        pass
    # A single-line file is its own header; do not print it twice.
    if header and header not in rows:
        rows = [header] + rows
    return "\n".join(rows)


def render_live_page():
    """Render the live data page.

    Fetches the text rather than reloading the page so the view does not
    flicker while someone watches it to confirm the instrument is alive.
    """
    body = (
        '<div class="nav"><a href="%s">&larr; Back to status</a></div>\n'
        '<div class="hdr"><h1>Live Data</h1>'
        "<p>The newest %d rows, refreshed every 2 seconds. Values arriving "
        "here mean the anemometer is talking to the logger right now.</p>"
        "</div>\n"
        '<div class="card"><div class="log" id="live">Loading&hellip;</div>'
        "</div>\n"
        "<script>\n"
        "function poll(){\n"
        "  var r = new XMLHttpRequest();\n"
        "  r.onload = function(){\n"
        "    document.getElementById('live').textContent = r.responseText;\n"
        "  };\n"
        "  r.onerror = function(){\n"
        "    document.getElementById('live').textContent =\n"
        "      'Lost contact with the logger.';\n"
        "  };\n"
        "  r.open('GET', '%s?t=' + Date.now());\n"
        "  r.send();\n"
        "}\n"
        "poll(); setInterval(poll, 2000);\n"
        "</script>\n"
    ) % (_link("/"), LIVE_ROWS, _link("/api/live"))
    return _page("TriSonica Live Data", body)


# ---------------------------------------------------------------------------
# Status cache
# ---------------------------------------------------------------------------

class StatusCache(object):
    """Time-limited cache so repeated refreshes don't hammer the Pi."""

    def __init__(self, data_dir, ttl=CACHE_TTL_S):
        self.data_dir = data_dir
        self.ttl = ttl
        self._lock = threading.Lock()
        self._data = None
        self._time = 0.0

    def get(self):
        now = time.monotonic()
        with self._lock:
            if self._data is not None and (now - self._time) < self.ttl:
                return self._data
        # Gather outside the lock so requests are not serialised behind
        # slow subprocess calls.
        status = gather_status(self.data_dir)
        with self._lock:
            self._data = status
            self._time = time.monotonic()
        return status


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class StatusHandler(http.server.BaseHTTPRequestHandler):
    """Routes GET requests to the appropriate renderer."""

    # HTTP/1.0 so the tar.gz streaming path can close the connection to
    # signal completion rather than requiring Content-Length or chunked.
    protocol_version = "HTTP/1.0"

    # Quieter than the default (one stderr line per request).
    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def log_error(self, fmt, *args):
        if getattr(self, "_client_error", False):
            log.debug("%s %s", self.address_string(), fmt % args)
        else:
            log.warning("%s %s", self.address_string(), fmt % args)

    def send_error(self, code, message=None, explain=None):
        """Log a client's mistake quietly; keep our own failures loud.

        The journal lives on the same SD card as the data. A 404 per request
        would let anything on the LAN write to it - a browser asking for a
        favicon on every refresh, a scanner walking /wp-admin - and the
        genuine faults would be buried in it. 5xx still warns: that is this
        unit failing, and 501 in particular means someone tried to change
        something here.
        """
        self._client_error = 400 <= code < 500
        try:
            http.server.BaseHTTPRequestHandler.send_error(
                self, code, message, explain)
        finally:
            self._client_error = False

    def _strip_prefix(self, path):
        """Remove the public prefix, or return None if it is not there.

        When no prefix is configured every path passes through unchanged, so
        LAN and Tailscale-only deployments are unaffected.
        """
        if not LINK_PREFIX:
            return path
        if path == LINK_PREFIX:
            return "/"
        if path.startswith(LINK_PREFIX + "/"):
            return path[len(LINK_PREFIX):]
        return None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        path = self._strip_prefix(path)
        if path is None:
            # Everything outside the prefix looks like an empty server. No
            # redirect and no hint that a correct path exists: a scanner that
            # found the Funnel hostname should learn nothing from probing it.
            self.send_error(404)
            return
        try:
            if path == "/":
                self._serve_dashboard()
            elif path in ("/data", "/data/"):
                self._serve_file_listing()
            elif path.startswith("/data/"):
                self._serve_data_file(path[len("/data/"):])
            elif path == "/api/status":
                self._serve_json()
            elif path == "/api/live":
                self._serve_live_data()
            elif path == "/live":
                self._serve_live_page()
            elif path == "/download-all":
                self._serve_tar()
            elif path == "/favicon.ico":
                self._serve_favicon()
            else:
                self.send_error(404)
        except Exception:
            log.exception("unhandled error serving %s", path)
            try:
                self.send_error(500)
            except Exception:
                pass

    # -- response helpers --------------------------------------------------

    def _send_html(self, content, code=200):
        body = content.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- handlers ----------------------------------------------------------

    def _serve_favicon(self):
        # Browsers ask for this on every navigation, and the dashboard
        # navigates itself once a minute. Answering "no content" ends the
        # conversation; a 404 would restart it on the next refresh.
        self.send_response(204)
        self.end_headers()

    def _serve_dashboard(self):
        status = self.server.cache.get()
        self._send_html(render_dashboard(status))

    def _serve_file_listing(self):
        status = self.server.cache.get()
        self._send_html(render_file_listing(status.get("files", [])))

    def _serve_data_file(self, raw_name):
        filename = os.path.basename(_url_unquote(raw_name))
        if not filename or "\x00" in filename:
            self.send_error(400, "Invalid filename")
            return
        # basename() already defeats ../ traversal. The suffix check and the
        # realpath containment below make the promise structural rather than
        # incidental: this endpoint can hand out recorded CSVs from the data
        # directory and nothing else, whatever else ends up in there.
        if not filename.endswith(".csv"):
            self.send_error(404, "File not found")
            return
        path = os.path.join(self.server.data_dir, filename)
        root = os.path.realpath(self.server.data_dir)
        if not os.path.realpath(path).startswith(root + os.sep):
            self.send_error(404, "File not found")
            return
        if not os.path.isfile(path):
            self.send_error(404, "File not found")
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            self.send_error(500, "Cannot stat file")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % filename)
        self.end_headers()
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (IOError, OSError):
            pass  # client disconnected mid-transfer

    def _serve_json(self):
        status = self.server.cache.get()
        payload = dict(status)
        # The verdict and the two ages are what a monitoring script wants;
        # leaving them to be re-derived from the raw fields would mean the
        # dashboard and any alerting could disagree about what is wrong.
        level, reasons = health_report(status)
        payload["health"] = {
            "level": level,
            "reasons": reasons,
            "data_age_s": data_age_s(status),
            "status_age_s": status_age_s(status),
        }
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


    def _serve_live_page(self):
        self._send_html(render_live_page())

    def _serve_live_data(self):
        body = live_text(self.server.data_dir).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_tar(self):

        files = get_data_files(self.server.data_dir)
        if not files:
            self._send_html(_page(
                "No Data",
                '<p>No data files to download.</p>'
                '<p><a href="%s">&larr; Back</a></p>' % _link("/")))
            return
        stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%SZ")
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header(
            "Content-Disposition",
            'attachment; filename="trisonica-data-%s.tar.gz"' % stamp)
        # No Content-Length: streaming mode, HTTP/1.0 signals end by close.
        self.end_headers()
        try:
            with tarfile.open(fileobj=self.wfile, mode="w|gz") as tar:
                for entry in files:
                    path = os.path.join(self.server.data_dir, entry["name"])
                    if os.path.isfile(path):
                        tar.add(path, arcname=entry["name"])
        except (IOError, OSError):
            pass  # client disconnected mid-transfer


class StatusHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded HTTP server that serves status and data."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, data_dir):
        self.data_dir = data_dir
        self.cache = StatusCache(data_dir)
        # Python 2-style super() for 3.7 compatibility with the MRO of
        # ThreadingMixIn + HTTPServer.
        http.server.HTTPServer.__init__(self, addr, StatusHandler)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def setup_logging(verbose):
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(handler)


def main():
    parser = argparse.ArgumentParser(
        description="TriSonica status dashboard and data file server")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help="path to the CSV data directory "
                             "(default: %(default)s)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="HTTP port (default: %(default)s)")
    parser.add_argument("--bind", default=DEFAULT_BIND,
                        help="address to bind to (default: %(default)s)")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="file holding PUBLIC_PREFIX (default: "
                             "%(default)s)")
    parser.add_argument("--public-prefix",
                        help="serve everything below this path segment and "
                             "404 outside it; overrides the config file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    global LINK_PREFIX
    config = read_config(args.config)
    raw_prefix = args.public_prefix
    if raw_prefix is None:
        raw_prefix = config.get("PUBLIC_PREFIX", "")
    try:
        LINK_PREFIX = normalize_prefix(raw_prefix)
    except ValueError as exc:
        # Refuse to start rather than fall back to serving everything at the
        # root: a typo in the prefix would otherwise quietly publish the
        # station, and the whole point of the prefix is that it is not public.
        log.error("%s", exc)
        return 1

    try:
        server = StatusHTTPServer((args.bind, args.port), args.data_dir)
    except OSError as exc:
        log.error("cannot start: %s", exc)
        return 1

    log.info("status server started on %s:%d", args.bind, args.port)
    log.info("serving data from %s", args.data_dir)
    if LINK_PREFIX:
        log.info("public prefix active: everything is served below %s/ and "
                 "any other path returns 404", LINK_PREFIX)
    else:
        log.info("no public prefix: serving at the root (LAN/Tailscale only "
                 "- do not expose this to the internet)")

    def handle_signal(signum, frame):
        log.info("shutting down")
        threading.Thread(target=server.shutdown).start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        server.serve_forever()
    finally:
        server.server_close()
        log.info("stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

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
from urllib.parse import urlsplit

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

# Optional link to the off-site archive.  This is configured on the deployed
# Pi rather than embedded in the source because its unguessable URL is a
# revocable capability.  Downloads go directly from the collector: proxying a
# gigabyte archive through the Pi would add card/network load and would make
# the backup inaccessible whenever the field unit is offline.
ARCHIVE_URL = ""

# Approximate data rate for estimating remaining recording time.
# Measured on this deployment: ~149 bytes/row at 10 Hz = ~122 MB/day.
MB_PER_DAY = 122.0

# The logger's own storage thresholds, mirrored.  Named rather than written as
# literals inside health_report() because the two programs have to agree about
# them: below CARD_FULL_MB the logger has already stopped writing (MIN_FREE_MB
# in trisonica_field_logger.py), so that is the number at which this page must
# say the card is full rather than inventing some other reason for the same
# silence.  CARD_LOW_MB is the logger's LOW_SPACE_WARN_MB, about a week's
# recording -- enough notice to book a visit.
CARD_FULL_MB = 150.0
CARD_LOW_MB = MB_PER_DAY * 7

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

# How long a connection may hold a thread without saying anything.  Without a
# timeout it is forever: a client that opens a socket and never sends a request
# keeps its handler thread and its file descriptor for the life of the process
# (measured -- 25 idle sockets, 25 threads, still there ten seconds later).
# That matters here more than on a normal web server, because this dashboard is
# published to the internet by Tailscale Funnel, so anything at all can open
# those sockets, and running out of descriptors takes the page down at exactly
# the moment its job is to say whether the station is alive.
REQUEST_TIMEOUT_S = 30

# ...but a *download* is allowed to be slow.  The socket timeout above applies
# to writes as well as reads, and the whole archive is a 32 MB stream that a
# phone on a train has every right to take its time over, so the bulk paths
# raise it once the response is committed.
BULK_TRANSFER_TIMEOUT_S = 600

# Ceiling on requests in flight at once.  The timeout above bounds how long any
# one connection can squat, but not how many arrive, and each still costs a
# thread and a descriptor.  Far above anything this page generates in use -- a
# browser refreshing once a minute, a live view polling every two seconds, a
# colleague downloading the archive -- so it binds only under abuse, and then
# it refuses cleanly rather than collapsing.
MAX_CONCURRENT_REQUESTS = 48

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


def normalize_archive_url(raw):
    """Return a safe HTTPS archive URL, or '' when none is configured."""
    value = (raw or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise ValueError("ARCHIVE_URL must be a plain HTTPS URL")
    return value.rstrip("/") + "/"


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


def _unescape_mount_field(text):
    """mountinfo octal-escapes space, tab, newline and backslash."""
    for code, char in (("\\040", " "), ("\\011", "\t"),
                       ("\\012", "\n"), ("\\134", "\\")):
        text = text.replace(code, char)
    return text


def filesystem_read_only(path, mountinfo="/proc/self/mountinfo"):
    """True when the FILESYSTEM carrying *path* is mounted read-only.

    Deliberately not statvfs's ST_RDONLY, which this service cannot trust
    about itself.  trisonica-status runs under ProtectHome=read-only, so the
    data directory genuinely IS read-only inside its own mount namespace, and
    statvfs cannot tell that apart from a card ext4 has given up on.  Observed
    on the unit the first time this was deployed: the dashboard went red and
    the dead-man's switch emailed "MEASUREMENTS ARE NOT BEING RECORDED" while
    the station recorded at 10.1 Hz.

    /proc/self/mountinfo keeps the two apart, which is why it is read here:

        per-mount options   ro   <- what the sandbox did
        superblock options  rw   <- what the filesystem is actually doing

    A card that ext4 has remounted read-only shows `ro` in the SECOND field.
    That is the one worth raising an alarm about, and it is the only one this
    looks at.

    Undeterminable means False.  A missing or unparseable mountinfo is not
    evidence that a card has failed, and inventing that verdict is precisely
    the mistake being corrected here.
    """
    try:
        target = os.path.realpath(path)
    except OSError:
        target = path
    best = -1
    read_only = False
    try:
        with open(mountinfo) as fh:
            for line in fh:
                fields = line.split()
                try:
                    sep = fields.index("-")
                except ValueError:
                    continue
                # fstype, source and super options follow the separator.
                if len(fields) <= sep + 3 or sep < 6:
                    continue
                point = _unescape_mount_field(fields[4])
                if target != point and not target.startswith(
                        point.rstrip("/") + "/"):
                    continue
                if len(point) <= best:      # keep the most specific mount
                    continue
                best = len(point)
                read_only = "ro" in fields[sep + 3].split(",")
    except (IOError, OSError, IndexError):
        return False
    return read_only


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
        # The one storage fault free_mb cannot show.  The unit is installed
        # with `tune2fs -e remount-ro`, so a card that develops errors is
        # remounted read-only rather than being allowed to corrupt more data --
        # and a read-only filesystem still reports its free space quite
        # happily.  Without this flag the page saw gigabytes free, found no
        # storage problem, and blamed the anemometer for the silence; the one
        # action that helps, replacing the card, appeared nowhere on it.
        #
        # Read from the superblock, NOT from st.f_flag: this service's own
        # sandbox sets ST_RDONLY on the data directory.  See
        # filesystem_read_only().
        "read_only": filesystem_read_only(data_dir),
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

    # --- Has the card stopped accepting writes? ---
    # Established before the recording question below, because either of these
    # IS the answer to it.  The logger stops writing on purpose in both cases,
    # so the silence they cause must not be reported as a separate, unexplained
    # fault with a different remedy attached to it.
    free = disk.get("free_mb", -1)
    stopped_because = None
    if disk.get("read_only"):
        stopped_because = ("The SD card has gone read-only, which means it has "
                           "developed errors. Data already recorded is safe, "
                           "but nothing further can be written and freeing "
                           "space will not help - the card has to be replaced")
    elif 0 <= free < CARD_FULL_MB:
        stopped_because = ("The card is full - the logger has stopped writing "
                           "rather than delete anything, so measurements are "
                           "being lost until it is emptied or swapped")
    if stopped_because:
        bad.append(stopped_because)
    elif 0 < free < CARD_LOW_MB:
        warn.append("The card is nearly full - about %.0f days left"
                    % disk.get("estimated_days", 0))

    # --- Is anything actually being recorded? ---
    # The question the page exists to answer, and the only check that does not
    # depend on a component reporting on itself.
    age = data_age_s(status)
    if age is None:
        bad.append("No data file has ever been written - the anemometer has "
                   "never been connected")
    elif age > DATA_STALE_S:
        if stopped_because:
            # Sending someone to check a USB cable for a silence the card
            # already accounts for is how a trip to the roof gets made with a
            # spare cable and no spare card.
            bad.append("Nothing has been recorded for %s, which the card "
                       "fault above explains" % _fmt_age(age))
        else:
            bad.append("Nothing recorded for %s - check the anemometer's USB "
                       "connection" % _fmt_age(age))

    if services.get("trisonica-logger") != "active":
        bad.append("The logger service is not running (%s)"
                   % services.get("trisonica-logger", "unknown"))

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


def _health_msg(health):
    """Return (css_class, message) for the overall health state."""
    if health == "ok":
        return "hok", "Recording"
    if health == "warn":
        return "hwn", "Attention needed"
    return "hbd", "Not recording"


def render_dashboard(status):
    """Render the status dashboard page as an HTML string."""
    system = status.get("system", {})
    disk = status.get("disk", {})
    li = status.get("logger", {})   # logger info
    files = status.get("files", [])

    health, reasons = health_report(status)
    hcss, hmsg = _health_msg(health)
    p = []  # parts

    # --- Header ---
    p.append('<div class="hdr %s">' % health)
    p.append("<h1>TriSonica Weather Station</h1>")
    p.append('<p class="hm %s">%s</p>' % (hcss, html.escape(hmsg)))
    if reasons:
        p.append('<ul class="why">')
        for reason in reasons:
            p.append("<li>%s</li>" % html.escape(reason))
        p.append("</ul>")
    p.append("</div>")

    # --- Current conditions ---
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

        note = []
        if not reading.get("time_synced", True):
            note.append("Measurement time is not verified")
        if reading.get("flags"):
            note.append("The latest reading contains flagged values")
        if note:
            p.append("<p>%s</p>" % " &middot; ".join(note))
        p.append("</div>")

    # --- Measurement ---
    p.append('<div class="card">')
    p.append("<h2>Recording</h2>")
    p.append('<div class="grid">')

    # Age of the newest data first: it answers "is it still recording?"
    # without trusting any component's own account of itself.
    age = data_age_s(status)
    p.append('<div class="m"><div class="v">%s</div>'
             '<div class="l">last row</div></div>'
             % (html.escape(_fmt_age(age)) if age is not None else "never"))

    # Everything below comes from the logger's 5-minutely journal line. When
    # that line is old the figures are history, and showing a stale "10.00 Hz"
    # is worse than showing nothing.
    stale = status_age_s(status)
    outdated = stale is not None and stale > STATUS_STALE_S

    rate = li.get("sample_rate_hz")
    p.append('<div class="m"><div class="v">%s</div>'
             '<div class="l">sample rate</div></div>'
             % ("%.2f" % rate if rate is not None and not outdated
                else "&mdash;"))

    bad = li.get("bad_pct")
    p.append('<div class="m"><div class="v">%s</div>'
             '<div class="l">flagged</div></div>'
             % ("%.2f%%" % bad if bad is not None and not outdated
                else "&mdash;"))

    p.append("</div>")  # grid

    if outdated:
        p.append('<p class="stale">Logger status: %s old.</p>' %
                 _fmt_age(stale))

    ts = li.get("time_source", "?")
    synced = li.get("time_synced")
    gps = li.get("gps_state", "?")
    if not synced:
        p.append('<p class="stale">Measurement time is not verified.</p>')
    if gps in ("nofix", "unreachable"):
        p.append("<p>Position is currently unavailable.</p>")
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

    # Without this the two halves of the page contradict each other: the header
    # says nothing is being recorded and the panel underneath reports gigabytes
    # free and weeks left, which reads as a page arguing with itself rather
    # than as a card that has failed.
    if disk.get("read_only"):
        p.append('<p class="stale">The card is mounted <strong>read-only</strong>'
                 ' - it has developed errors. The free space above is real but '
                 'unusable; the card has to be replaced.</p>')

    links = [
        '<a href="%s" class="btn">Data files</a>' % _link("/data/"),
        '<a href="%s" class="btn">Live rows</a>' % _link("/live"),
    ]
    if ARCHIVE_URL:
        links.append('<a href="%s" class="btn" rel="noreferrer">'
                     'Server archive</a>' % html.escape(ARCHIVE_URL, quote=True))
    p.append("<p>%s</p>" % " ".join(links))
    p.append("</div>")

    # --- Footer ---
    p.append('<p class="ft">Updated %s &middot; refreshes every %d s</p>'
             % (html.escape(system.get("timestamp_utc", "?")),
                REFRESH_INTERVAL_S))

    return _page(
        "TriSonica Weather Station",
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
    p.append("<h1>Data</h1>")
    total_bytes = sum(f.get("size_bytes", 0) for f in files)
    p.append("<p>%d files &middot; %s</p>" %
             (len(files), _fmt_size(total_bytes)))
    if files:
        p.append('<p><a href="%s" class="btn">' % _link("/download-all") +
                 'Download all (.tar.gz)</a></p>')
    p.append("</div>")

    if not files:
        p.append('<div class="card">')
        p.append("<p>No files yet.</p>")
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
        "<p>Latest %d rows &middot; every 2 s</p>"
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
        "      'Live feed unavailable.';\n"
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
    """Time-limited cache so repeated refreshes don't hammer the Pi.

    One gather costs four subprocesses, one of them a `journalctl -n 100`, and
    every one of them competes for the same SD card as a live 10 Hz recording.

    A plain TTL is not enough, because requests that arrive together also MISS
    together: eight concurrent readers ran eight gathers (measured), which is
    thirty-two subprocesses for one answer.  So exactly one thread refreshes and
    the others wait for its result.  A burst costs one gather, not one each.

    Waiting is bounded.  If the refresher is taking longer than any healthy
    gather could, a waiter stops queueing behind it and gathers itself -- a
    wedged refresh must not be able to hold every reader indefinitely, since
    the readers are the people trying to find out whether the station is alive.
    """

    # Comfortably past a healthy gather even on a busy Pi, and short of the
    # point where a browser gives up.
    WAIT_S = 30.0

    def __init__(self, data_dir, ttl=CACHE_TTL_S):
        self.data_dir = data_dir
        self.ttl = ttl
        self._lock = threading.Lock()
        self._ready = threading.Condition(self._lock)
        self._refreshing = False
        self._data = None
        self._time = 0.0

    def _fresh(self):
        return (self._data is not None
                and (time.monotonic() - self._time) < self.ttl)

    def get(self):
        deadline = time.monotonic() + self.WAIT_S
        with self._lock:
            while True:
                if self._fresh():
                    return self._data
                if not self._refreshing:
                    self._refreshing = True
                    break                       # this thread does the work
                if not self._ready.wait(
                        timeout=max(0.0, deadline - time.monotonic())):
                    self._refreshing = True     # the other one is wedged
                    break

        # Gathered outside the lock, so a reader that already has a fresh
        # answer is never serialised behind a slow subprocess call.
        status = None
        try:
            status = gather_status(self.data_dir)
        finally:
            with self._lock:
                self._refreshing = False
                if status is not None:
                    self._data = status
                    self._time = time.monotonic()
                # Whether it worked or raised, the waiters have to be released:
                # on success they take this result, and on failure one of them
                # becomes the next refresher rather than waiting out the clock.
                self._ready.notify_all()
        return status


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class StatusHandler(http.server.BaseHTTPRequestHandler):
    """Routes GET requests to the appropriate renderer."""

    # HTTP/1.0 so the tar.gz streaming path can close the connection to
    # signal completion rather than requiring Content-Length or chunked.
    protocol_version = "HTTP/1.0"

    # Picked up by socketserver.StreamRequestHandler.setup(), which applies it
    # to the connection.  See REQUEST_TIMEOUT_S.
    timeout = REQUEST_TIMEOUT_S

    # Things the base class reports through log_error() that are the CLIENT's
    # doing, not this unit failing.  They belong on the quiet path with the 4xx
    # responses below: a socket that opens and then goes silent is closed by
    # `timeout`, and the loud path would write a journal line -- onto the same
    # SD card as the data -- for every idle connection anything on the internet
    # cared to open.
    _CLIENT_FAULTS = ("Request timed out",)

    # Quieter than the default (one stderr line per request).
    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def log_error(self, fmt, *args):
        theirs = (getattr(self, "_client_error", False)
                  or fmt.startswith(self._CLIENT_FAULTS))
        if theirs:
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

    def _allow_slow_transfer(self):
        """Let a committed response take as long as a slow client needs.

        Safe to do mid-request because protocol_version is HTTP/1.0: the
        connection is not reused, so the relaxed timeout dies with it.
        """
        try:
            self.connection.settimeout(BULK_TRANSFER_TIMEOUT_S)
        except (OSError, AttributeError):
            pass

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
        self._allow_slow_transfer()
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
        self._allow_slow_transfer()
        try:
            with tarfile.open(fileobj=self.wfile, mode="w|gz") as tar:
                for entry in files:
                    path = os.path.join(self.server.data_dir, entry["name"])
                    if os.path.isfile(path):
                        tar.add(path, arcname=entry["name"])
        except (IOError, OSError):
            pass  # client disconnected mid-transfer


class StatusHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded HTTP server that serves status and data.

    Threads are capped.  ThreadingMixIn on its own spawns one per connection
    with no ceiling, which is fine on a LAN and is not fine on an address
    Tailscale Funnel publishes to the internet: the descriptor limit is what
    would stop it, and reaching that takes the dashboard down.  Refusing the
    49th caller costs a colleague nothing and leaves the page answering.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, data_dir):
        self.data_dir = data_dir
        self.cache = StatusCache(data_dir)
        self._active = 0
        self._active_lock = threading.Lock()
        self.refused = 0
        # Python 2-style super() for 3.7 compatibility with the MRO of
        # ThreadingMixIn + HTTPServer.
        http.server.HTTPServer.__init__(self, addr, StatusHandler)

    #: Sent without a handler, so it is assembled rather than written out with
    #: a hand-counted length that the next edit to the text would falsify.
    _BUSY_BODY = b"Too many connections in flight; try again shortly.\n"
    _BUSY_RESPONSE = (b"HTTP/1.0 503 Service Unavailable\r\n"
                      b"Content-Type: text/plain; charset=utf-8\r\n"
                      b"Content-Length: " + str(len(_BUSY_BODY)).encode() +
                      b"\r\nConnection: close\r\n\r\n" + _BUSY_BODY)

    def process_request(self, request, client_address):
        with self._active_lock:
            over = self._active >= MAX_CONCURRENT_REQUESTS
            if over:
                self.refused += 1
                refusal = self.refused
            else:
                self._active += 1
        if not over:
            try:
                socketserver.ThreadingMixIn.process_request(
                    self, request, client_address)
            except Exception:
                # The thread never started, so process_request_thread will
                # never run to give the slot back. Leaking one here would be
                # permanent, and this fails precisely when threads are scarce -
                # so enough of them would wedge the server at zero capacity for
                # good. The base server closes the socket on its way out.
                with self._active_lock:
                    self._active -= 1
                raise
            return
        # Answered right here rather than from a handler: spawning a thread to
        # explain that there are too many threads is the shape of the problem,
        # not the fix.  Rate-limited the way the logger's repeating faults are,
        # so the refusals cannot themselves fill the journal.
        if refusal in (1, 10, 100, 1000, 10000):
            log.warning("refused a connection: %d already in flight "
                        "(refusal %d). Something is opening far more "
                        "connections than this dashboard is read by.",
                        MAX_CONCURRENT_REQUESTS, refusal)
        try:
            request.sendall(self._BUSY_RESPONSE)
        except OSError:
            pass
        self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            socketserver.ThreadingMixIn.process_request_thread(
                self, request, client_address)
        finally:
            with self._active_lock:
                self._active -= 1


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

    global LINK_PREFIX, ARCHIVE_URL
    config = read_config(args.config)
    raw_prefix = args.public_prefix
    if raw_prefix is None:
        raw_prefix = config.get("PUBLIC_PREFIX", "")
    try:
        LINK_PREFIX = normalize_prefix(raw_prefix)
        ARCHIVE_URL = normalize_archive_url(config.get("ARCHIVE_URL", ""))
    except ValueError as exc:
        # Refuse to start rather than silently ignore an unsafe public URL or
        # fall back to serving everything at the root after a prefix typo.
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

#!/usr/bin/env python3
"""Unattended field datalogger for the LI-COR LI-550 TriSonica Mini.

Designed for a Raspberry Pi 3B+ that a researcher powers on and walks away
from: no keyboard, no screen, no commands. It starts at boot, waits for the
anemometer, logs to the SD card, and signals its state on the Pi's green LED.

Three properties matter more than features here, because nobody is watching:

  1. Timestamps must never be silently wrong. The Pi has no battery-backed
     clock and boots at the epoch, so every row records WHERE its time came
     from (gps / ntp / freerun) and whether the clock was trustworthy.

  2. Bad data must be visible. Sentinel values (-99.x) and physically
     impossible readings are flagged per row rather than averaged into
     oblivion, and a sustained bad-data rate changes the LED pattern.

  3. It must survive having its power pulled, which is how it will be
     stopped in the field. Rows are flushed and fsync'd at a bounded
     interval so at most FSYNC_INTERVAL_S of data is ever at risk.

Targets Python 3.7 (Raspbian Buster) - no walrus, no f-string '=', no
dict-union operator.
"""

import argparse
import csv
import datetime
import errno
import glob
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque

try:
    import serial
except ImportError:
    sys.stderr.write("pyserial is required: sudo apt install python3-serial\n")
    raise

# --------------------------------------------------------------------------
# Configuration defaults
# --------------------------------------------------------------------------

# All elapsed-time measurements in this module use time.monotonic(), never
# time.time(). This Pi has no RTC: it boots at whatever fake-hwclock saved and
# is later corrected by GPS or NTP, and that correction can move the clock
# BACKWARDS by hours (observed: a 4.5 h jump on 2026-08-07). With a wall clock,
# a backward jump makes every "now - last_x" negative, so fsync, the disk-full
# check, stale-link detection and file rotation all silently stop for the
# duration of the jump - voiding the guarantee that a pulled plug costs at most
# FSYNC_INTERVAL_S of data. Wall-clock time is used ONLY for the timestamps
# actually written into the data (datetime.utcnow) and for file names.

BAUD_RATE = 115200
DEVICE_SCAN_INTERVAL_S = 3.0      # how often to look for the anemometer
SERIAL_READ_TIMEOUT_S = 2.0
STALE_DATA_TIMEOUT_S = 10.0       # no bytes for this long => treat as dropped
# Bytes arriving that are not measurements. The timeout above keys on BYTES,
# not on content, so an instrument sitting in its configuration menu - or one
# being read at the wrong baud rate - keeps it fresh indefinitely while
# producing no data at all. Generous next to the 10 Hz sample rate: 15 s is
# 150 missed samples, well past any burst of line noise or a truncated
# sentence at connection time.
NO_PARSE_TIMEOUT_S = 15.0
FSYNC_INTERVAL_S = 5.0            # bound on data at risk from a power cut
STATUS_LOG_INTERVAL_S = 300.0     # periodic health line in the journal
FILE_ROTATE_HOURS = 6             # cap the blast radius of one corrupt file

# Storage guard. At ~149 bytes/row and 10 Hz this logger writes ~122 MB/day,
# so a 4 GB card lasts about a month. Running the filesystem completely dry is
# the dangerous case: writes fail, opening a replacement file fails too, and
# without this guard the process would die and be restarted forever by
# systemd - a silent crash loop with no usable indication. Instead we stop
# writing while there is still headroom, keep the process alive, and raise a
# distinct LED alarm so a human can see it. Existing data is never deleted:
# on a research instrument, discarding measurements to make room is worse
# than stopping.
MIN_FREE_MB = 150
DISK_CHECK_INTERVAL_S = 60
# Measured on this deployment: ~149 bytes/row at 10 Hz.
MB_PER_DAY = 122.0
# Warn while there is still about a week left, so a remote operator has time
# to act. MIN_FREE_MB alone is roughly one day's notice.
LOW_SPACE_WARN_MB = MB_PER_DAY * 7
# ...but not on every check. The low-space condition holds for the whole last
# week of a deployment, so one line per DISK_CHECK_INTERVAL_S wrote ~10,000
# warnings into the journal - burying everything else at exactly the moment
# someone is looking at a nearly-full card. Hourly keeps it a reminder rather
# than a flood; the 5-minute status line carries the free-space figure anyway.
LOW_SPACE_WARN_INTERVAL_S = 3600.0
HEADER_DISCOVERY_ROWS = 20        # sample this many rows to learn the schema
GPS_STALE_S = 30.0                # GPS fix older than this is not recorded
TIME_SOURCE_REFRESH_S = 30.0      # how often to re-ask chrony

# The anemometer emits -99.x as its "no valid measurement" sentinel (observed
# values: -99.40 .. -99.63). Bounded deliberately: a naive "value <= -99"
# test would also swallow -150.37, which is a genuinely out-of-range reading
# and a different fault worth distinguishing in the data.
SENTINEL_LOW = -100.0
SENTINEL_HIGH = -99.0

# Physically plausible ranges. Values outside these are real readings that
# cannot be true, which is a DIFFERENT failure from the sentinel: during the
# power-related fault we diagnosed, the unit emitted 24 m/s winds and -24 C
# indoors, and those pass a naive sentinel check.
PLAUSIBLE = {
    "S":  (0.0, 60.0),      # 3D wind speed, m/s
    "S2": (0.0, 60.0),      # 2D wind speed, m/s
    "D":  (0.0, 360.0),     # direction, deg
    "U":  (-60.0, 60.0),    # component, m/s
    "V":  (-60.0, 60.0),
    "W":  (-60.0, 60.0),
    "T":  (-45.0, 65.0),    # sonic temperature, C
    "H":  (0.0, 100.0),     # humidity, %
    "P":  (800.0, 1100.0),  # pressure, hPa
    "PI": (-180.0, 180.0),  # pitch, deg
    "RO": (-180.0, 180.0),  # roll, deg
    "MD": (0.0, 360.0),     # magnetic direction, deg
    "TD": (0.0, 360.0),     # true direction, deg
}

# Largest change between two consecutive samples (100 ms apart at 10 Hz) that
# the atmosphere can actually produce. Static range checks are not enough: the
# fault we diagnosed produced S=24.85 m/s and T=-22.04 C, both of which sit
# INSIDE the plausible ranges above. What makes them impossible is the jump -
# from 0.12 m/s and +21.4 C in one sample. These thresholds are deliberately
# generous so that real gusts and real turbulence are never rejected.
# Direction is omitted because it wraps at 360 and a delta is meaningless.
MAX_DELTA = {
    "S":  15.0,   # m/s per sample
    "S2": 15.0,
    "U":  15.0,
    "V":  15.0,
    "W":  15.0,
    "T":  5.0,    # C per sample
    "H":  5.0,    # % per sample
    "P":  2.0,    # hPa per sample
}

# A spike check needs a reference. If the stream has been interrupted for
# longer than this, the previous value is too old to compare against.
SPIKE_REFERENCE_MAX_AGE_S = 2.0

# Canonical sensor column order, matching the instrument's own transmission
# order. Schema discovery must NOT simply use first-seen order: the serial
# connection frequently opens mid-line, so the first parsed sample can be a
# truncated row starting partway through the sentence. That produced files
# with different column orders between runs on 2026-08-07 - harmless per file,
# but a trap when concatenating files or indexing columns by position.
# Unknown fields (firmware differences, added parameters) are appended after
# these, sorted, so they are still captured deterministically.
CANONICAL_FIELDS = ("S", "S2", "D", "U", "V", "W", "T", "H", "P",
                    "PI", "RO", "MD", "TD")

# Fields whose failure means the measurement is scientifically useless, as
# opposed to merely incomplete. Used to decide the LED alarm state.
CRITICAL_FIELDS = ("S", "T")

LED_PATH = "/sys/class/leds/led0"   # green ACT LED on Pi 3B+
# The blink thread writes roughly eight times a second, and a single failed
# write means nothing - the sysfs file can be busy. A run of them means the
# light has stopped working, and the light is the whole interface in the
# field, so that has to reach the journal. Ten consecutive failures is a
# second or two of a dark LED.
LED_WRITE_FAILURES_BEFORE_WARNING = 10

# Written by trisonica-usb-export.service while it is copying data onto a USB
# stick. The logger only READS this: the export runs as a separate root
# service precisely so that it cannot disturb the recording path. All the
# logger does is show a distinct LED pattern meaning "copying, do not remove
# the stick".
EXPORT_BUSY_MARKER = "/run/trisonica-export-busy"
# The export service refreshes the marker's mtime as it copies. A marker that
# has stopped being refreshed belongs to an export that was killed - SIGTERM
# on a service restart, or a wedged USB device - and must not keep masking
# every other LED state. Generously above the export's own refresh interval.
EXPORT_BUSY_STALE_S = 60.0

# systemd restarts a process that CRASHES, but a process that hangs - blocked
# on a device read that never returns, or deadlocked - stays alive and nobody
# notices. The unit sets WatchdogSec; if these pings stop arriving, systemd
# kills and restarts us. Ping well inside the deadline so a slow loop does not
# trigger a spurious restart.
WATCHDOG_PING_INTERVAL_S = 20.0

# Cap on the gpsd receive buffer. gpsd emits newline-delimited JSON; anything
# larger than this without a newline is not that, and must not be allowed to
# grow unboundedly in a process that runs for months.
GPSD_MAX_BUFFER = 1 << 20   # 1 MiB
# Consecutive failures to reach gpsd before saying so. The reader retries
# every 5 s, so a couple of failures is just gpsd being restarted - which
# happens - while a run of them means the receiver, its wiring or the service
# is broken. Reported once, because 17,280 retries a day must not fill the
# journal.
GPSD_UNREACHABLE_ATTEMPTS = 3

# The same cap on the instrument's own stream, for the same reason. readline()
# has no length limit of its own, so a device stuck emitting bytes with no
# newline - which is what a wedged USB bridge looks like - grows the line
# buffer one-for-one with what it sends. Measured: 1.1 MB in, +1124 kB RSS.
# At 115200 baud that is ~41 MB/h, so a unit left running would be killed by
# the OOM reaper inside a day, taking the recording with it.
#
# A real sentence is about 150 bytes, so this is 27x headroom. Capping it also
# turns the failure into a visible one: the oversized chunks do not parse, so
# they are counted and the no-parseable-data alarm raises the LED, instead of
# the process quietly swelling until it dies.
MAX_LINE_BYTES = 4096

log = logging.getLogger("trisonica")


def sd_notify(message):
    """Send a datagram to systemd's notify socket. No-op when not under systemd.

    Implemented directly rather than via python-systemd so the logger keeps
    working on a machine where that package is not installed.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):          # abstract namespace socket
        addr = "\0" + addr[1:]
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.connect(addr)
        sock.sendall(message.encode("utf-8"))
        return True
    except (OSError, socket.error):
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Time provenance
# --------------------------------------------------------------------------

class TimeSource(object):
    """Tracks where the system clock's authority currently comes from.

    A Pi 3B+ has no RTC. Without this, a field unit that never saw a GPS fix
    would write perfectly formatted timestamps from 1970 and nothing in the
    dataset would reveal it.
    """

    GPS = "gps"
    NTP = "ntp"
    FREERUN = "freerun"

    # Class attribute so it resolves even on an instance built without
    # __init__, and so reporting a missing chronyc can never itself raise.
    _tool_warned = False

    def __init__(self):
        self._lock = threading.Lock()
        self._source = self.FREERUN
        self._synced = False
        self._stop = threading.Event()
        self._thread = None
        self.refresh()

    def _query_chrony(self):
        """Return (source, synced). Never raises."""
        try:
            out = subprocess.check_output(
                ["chronyc", "-n", "tracking"],
                stderr=subprocess.STDOUT, timeout=5,
            ).decode("utf-8", "replace")
        except OSError as exc:
            # chronyc missing altogether is a different problem from a chrony
            # that is running but unsynchronised, and it is otherwise silent:
            # every row would be stamped time_synced=0 and the status LED
            # would sit at two flashes for the whole deployment with a
            # perfectly good clock. Say it once.
            if not self._tool_warned:
                self._tool_warned = True
                log.warning("cannot run chronyc (%s) - time provenance will "
                            "report '%s' and the status LED will stay at two "
                            "flashes even if the clock is correct",
                            exc, self.FREERUN)
            return self.FREERUN, False
        except Exception:
            return self.FREERUN, False

        ref_id = ""
        leap = ""
        for line in out.splitlines():
            if line.startswith("Reference ID"):
                ref_id = line.split(":", 1)[1].strip()
            elif line.startswith("Leap status"):
                leap = line.split(":", 1)[1].strip()

        # chrony reports 'Not synchronised' as a leap status; an unsynchronised
        # chrony also reports reference id 00000000 (or 7F7F0101).
        synced = leap.lower().startswith("normal") and "00000000" not in ref_id
        if not synced:
            return self.FREERUN, False
        if "NMEA" in ref_id.upper() or "GPS" in ref_id.upper():
            return self.GPS, True
        return self.NTP, True

    def refresh(self):
        source, synced = self._query_chrony()
        with self._lock:
            if source != self._source or synced != self._synced:
                log.info("time source: %s -> %s (synced=%s)",
                         self._source, source, synced)
            self._source = source
            self._synced = synced

    def _run(self):
        while not self._stop.wait(TIME_SOURCE_REFRESH_S):
            self.refresh()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="timesource")
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._stop.set()

    @property
    def state(self):
        with self._lock:
            return self._source, self._synced


# --------------------------------------------------------------------------
# GPS
# --------------------------------------------------------------------------

class GpsReader(object):
    """Streams position from gpsd over its JSON socket.

    Deliberately tolerant: gpsd may be absent, restarting, or without a fix.
    Any of those simply means no position columns for those rows, never a
    logger crash. Reconnects on its own.
    """

    # Class attributes so they resolve however the object is built.
    _reach_failures = 0
    _unreachable_logged = False

    def __init__(self, host="127.0.0.1", port=2947):
        self.host = host
        self.port = port
        self._reach_failures = 0
        self._unreachable_logged = False
        self._lock = threading.Lock()
        self._fix = None          # dict, or None
        self._fix_time = 0.0
        self._sats_used = 0
        self._stop = threading.Event()
        self._thread = None
        self.connected = False

    def _handle(self, msg):
        # gpsd normally sends JSON objects, but a truncated read can yield a
        # bare array or scalar. Guard here rather than relying on the caller.
        if not isinstance(msg, dict):
            return
        cls = msg.get("class")
        if cls == "TPV":
            mode = msg.get("mode", 0)
            if mode >= 2:  # 2 = 2D fix, 3 = 3D fix
                with self._lock:
                    self._fix = {
                        "lat": msg.get("lat"),
                        "lon": msg.get("lon"),
                        "alt": msg.get("alt"),
                        "mode": mode,
                    }
                    self._fix_time = time.monotonic()
        elif cls == "SKY":
            sats = msg.get("satellites") or []
            used = sum(1 for s in sats
                       if isinstance(s, dict) and s.get("used"))
            with self._lock:
                self._sats_used = used

    def _run(self):
        while not self._stop.is_set():
            sock = None
            try:
                sock = socket.create_connection((self.host, self.port), timeout=5)
                sock.settimeout(5)
                sock.send(b'?WATCH={"enable":true,"json":true}\n')
                self.connected = True
                if self._unreachable_logged:
                    log.warning("gpsd is reachable again after %d failed "
                                "attempts", self._reach_failures)
                    self._unreachable_logged = False
                self._reach_failures = 0
                log.info("gpsd connected at %s:%d", self.host, self.port)
                buf = b""
                while not self._stop.is_set():
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > GPSD_MAX_BUFFER:
                        # No newline in a very large read means the stream is
                        # not the line-delimited JSON we expect. Drop it
                        # rather than growing memory for the life of the
                        # process.
                        log.warning("gpsd buffer exceeded %d bytes without a "
                                    "newline; discarding", GPSD_MAX_BUFFER)
                        buf = b""
                        continue
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            self._handle(json.loads(line.decode("utf-8", "replace")))
                        except (ValueError, AttributeError):
                            continue
            except Exception as exc:
                if self.connected:
                    log.warning("gpsd connection lost: %s", exc)
                else:
                    # Never got a connection at all. This used to be entirely
                    # silent, so a dead gpsd, an unplugged receiver or a wrong
                    # port produced no log line ever, and the status line said
                    # "nofix" - exactly what a working receiver indoors says.
                    self._reach_failures += 1
                    if (not self._unreachable_logged
                            and self._reach_failures >= GPSD_UNREACHABLE_ATTEMPTS):
                        self._unreachable_logged = True
                        log.warning(
                            "cannot reach gpsd at %s:%d after %d attempts "
                            "(%s). Position and offline time are unavailable, "
                            "and the status line will read 'unreachable' "
                            "rather than 'nofix' so the two are not confused. "
                            "Recording continues.",
                            self.host, self.port, self._reach_failures, exc)
            finally:
                self.connected = False
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
            if not self._stop.is_set():
                self._stop.wait(5.0)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="gpsd")
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._stop.set()

    def current(self):
        """Latest fix if it is fresh enough to attach to a sample, else None."""
        with self._lock:
            if self._fix is None:
                return None, self._sats_used
            if time.monotonic() - self._fix_time > GPS_STALE_S:
                return None, self._sats_used
            return dict(self._fix), self._sats_used


# --------------------------------------------------------------------------
# LED status
# --------------------------------------------------------------------------

class LedStatus(object):
    """Signals logger state on the Pi's green ACT LED.

    The researcher has no screen, so this is the entire user interface:

        1 flash, pause    ready - logging, clock verified
        2 flashes, pause  logging, but timestamps not yet verified
        3 flashes, pause  waiting for the anemometer
        rapid flicker     data is bad - check the instrument
        mostly on         card full - measurements are being lost
        even 1 Hz blink   copying to a USB stick - do not remove it
        off               not running

    Degrades silently to a no-op if the LED is not writable, because a
    permissions problem must never stop data collection.
    """

    # States, in the order a human should read them. The signalling language is
    # deliberately based on COUNTING BLINKS rather than judging frequency:
    # telling 1 Hz from 5 Hz by eye is unreliable, but counting one, two or
    # three flashes before a pause is not. More blinks = more attention needed.
    READY = "ready"          # 1 blink  - logging, clock verified, all good
    NO_TIME = "no_time"      # 2 blinks - logging, but timestamps unverified
    WAITING = "waiting"      # 3 blinks - no anemometer
    BAD_DATA = "bad_data"    # rapid    - anemometer producing garbage
    DISK_FULL = "disk_full"  # near-solid - card full, data being lost
    EXPORTING = "exporting"  # even 1 Hz blink - copying to USB, do not remove

    # Kept as an alias so older callers/tests referring to OK still work.
    OK = READY

    # Consecutive failed sysfs writes, and whether that has been reported.
    # Class attributes so the counter exists however the object is built.
    _write_failures = 0
    _write_failure_logged = False

    def __init__(self, path=LED_PATH, enabled=True):
        self.path = path
        self.enabled = enabled
        self._state = self.WAITING
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._available = False
        if enabled:
            self._available = self._take_control()

    def _write(self, name, value):
        try:
            fh = open(os.path.join(self.path, name), "w")
            try:
                fh.write(str(value))
            finally:
                fh.close()
        except (IOError, OSError) as exc:
            # A dead status light used to be completely silent: _write simply
            # returned False and the blink thread ignored it, so a researcher
            # in the field would see nothing and the journal would say nothing
            # either. Report a sustained failure once, and the recovery once.
            self._write_failures += 1
            if (not self._write_failure_logged
                    and self._write_failures >= LED_WRITE_FAILURES_BEFORE_WARNING):
                self._write_failure_logged = True
                log.error("the status LED has stopped responding (%d failed "
                          "writes to %s: %s). It is the only interface in the "
                          "field and will now show nothing. Recording is "
                          "unaffected and continues.",
                          self._write_failures, self.path, exc)
            return False
        if self._write_failure_logged:
            log.warning("the status LED is responding again after %d failed "
                        "writes", self._write_failures)
        self._write_failures = 0
        self._write_failure_logged = False
        return True

    def _take_control(self):
        # The ACT LED normally follows SD-card activity; claim it for status.
        if not os.path.isdir(self.path):
            log.warning("LED path %s absent - status LED disabled", self.path)
            return False
        if not self._write("trigger", "none"):
            log.warning("cannot write %s/trigger - status LED disabled "
                        "(logging continues)", self.path)
            return False
        return True

    def set(self, state):
        with self._lock:
            if state != self._state:
                log.info("status: %s -> %s", self._state, state)
            self._state = state

    # Blink shape shared by the countable states: a short flash, a short gap
    # between flashes, and a long gap that separates one group from the next.
    _FLASH = 0.12
    _GAP = 0.22
    _PAUSE = 1.3

    def _burst(self, count):
        """N short flashes followed by a long pause, so N is countable."""
        steps = []
        for i in range(count):
            steps.append((1, self._FLASH))
            steps.append((0, self._GAP if i < count - 1 else self._PAUSE))
        return steps

    def _pattern(self, state):
        """Return a list of (brightness, duration_s) steps."""
        if state == self.READY:
            return self._burst(1)      # 1 blink  - everything good
        if state == self.NO_TIME:
            return self._burst(2)      # 2 blinks - timestamps not yet verified
        if state == self.WAITING:
            return self._burst(3)      # 3 blinks - anemometer not connected
        if state == self.BAD_DATA:
            # Continuous rapid flicker, visibly unlike any countable burst.
            # Deliberately off-dominant so that "mostly ON" remains unique to
            # DISK_FULL - the only state meaning data is actively being lost.
            return [(1, 0.06), (0, 0.10)]
        if state == self.EXPORTING:
            # Even 50/50 blink at 1 Hz. Unmistakably different from the
            # countable bursts (8% duty), the rapid flicker and the near-solid
            # alarm, so "do not remove the stick" cannot be misread.
            return [(1, 0.5), (0, 0.5)]
        if state == self.DISK_FULL:
            # Near-solid: the only pattern that is mostly ON, because it is
            # the only one meaning measurements are being lost right now.
            return [(1, 1.5), (0, 0.4)]
        return self._burst(3)

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                state = self._state
            for level, duration in self._pattern(state):
                if self._stop.is_set():
                    break
                self._write("brightness", 255 if level else 0)
                self._stop.wait(duration)

    def start(self):
        if not self._available:
            return
        self._thread = threading.Thread(target=self._run, name="led")
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._stop.set()
        # Let the blink thread observe the flag and exit before restoring the
        # trigger; otherwise it can write brightness afterwards and leave the
        # LED stuck on with the SD-activity trigger reattached.
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._available:
            self._write("brightness", 0)
            self._write("trigger", "mmc0")   # restore normal SD-activity role


# --------------------------------------------------------------------------
# Parsing and validation
# --------------------------------------------------------------------------

def parse_line(line):
    """Parse 'S -99.58,S2  00.00,D -000,...' into {'S': '-99.58', ...}.

    Values are space-padded and the separator is a comma. Returns {} for
    anything unrecognisable rather than raising.

    Both halves are validated. The instrument's configuration menu, kernel
    boot messages and line noise all reach this function, and without the
    checks a line like 'Press Any Key to Continue' would parse into a
    plausible-looking {'Press': 'Any Key to Continue'} and invent a column.
    Every real field is a short alphanumeric key with a numeric value.
    """
    out = {}
    for pair in line.split(","):
        pair = pair.strip()
        if not pair or " " not in pair:
            continue
        key, _, value = pair.partition(" ")
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        if len(key) > 4 or not key.isalnum():
            continue
        try:
            float(value)
        except ValueError:
            continue
        # Stored as the original string so the instrument's own formatting
        # (leading zeros, sign, decimal places) survives into the CSV.
        out[key] = value
    return out


class Validator(object):
    """Classifies each sample and detects single-sample spikes.

    Three independent failure modes are distinguished, because they mean
    different things to whoever analyses the data later:

        :err    the instrument reported its own sentinel (-99.x) - it knows
                it failed
        :impl   a value outside what the instrument can physically measure
        :spike  a value that is individually plausible but changed faster
                than the atmosphere can change, i.e. a transient glitch

    Spike detection compares against the last value that was itself accepted,
    so one bad sample cannot poison the reference for the next.
    """

    def __init__(self):
        self._last_good = {}     # key -> (value, monotonic timestamp)

    def reset(self):
        """Forget references - call after a reconnection or stream gap."""
        self._last_good = {}

    def check(self, parsed):
        """Return (flags, n_err, n_impl, n_spike, critical_bad)."""
        flags = []
        n_err = 0
        n_impl = 0
        n_spike = 0
        critical_bad = False
        now = time.monotonic()

        for key, raw in parsed.items():
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue

            # 1. Instrument-reported failure (its -99.x sentinel).
            if SENTINEL_LOW < value <= SENTINEL_HIGH:
                flags.append(key + ":err")
                n_err += 1
                if key in CRITICAL_FIELDS:
                    critical_bad = True
                continue

            # 2. Outside the instrument's physical measurement range.
            bounds = PLAUSIBLE.get(key)
            if bounds is not None and not (bounds[0] <= value <= bounds[1]):
                flags.append(key + ":impl")
                n_impl += 1
                if key in CRITICAL_FIELDS:
                    critical_bad = True
                continue

            # 3. Changed faster than physically possible.
            limit = MAX_DELTA.get(key)
            if limit is not None:
                previous = self._last_good.get(key)
                if previous is not None:
                    prev_value, prev_time = previous
                    fresh = (now - prev_time) <= SPIKE_REFERENCE_MAX_AGE_S
                    if fresh and abs(value - prev_value) > limit:
                        flags.append(key + ":spike")
                        n_spike += 1
                        if key in CRITICAL_FIELDS:
                            critical_bad = True
                        # Do not adopt a spike as the new reference.
                        continue

            self._last_good[key] = (value, now)

        return ";".join(flags), n_err, n_impl, n_spike, critical_bad


# --------------------------------------------------------------------------
# Logger
# --------------------------------------------------------------------------

class FieldLogger(object):

    # Health state carried as class attributes so that every method below
    # resolves on an instance assembled without __init__, and so adding a
    # counter can never turn into an AttributeError inside the recording loop.
    data_dir = None            # always set by __init__; here so that a
                               # diagnostic can name it without risking an
                               # AttributeError inside an error path
    csv_file = None            # open data file, when there is one
    csv_writer = None
    csv_path = None
    last_data = 0.0            # monotonic time bytes last arrived at all
    last_parsed = 0.0          # monotonic time of the last parseable sample
    unparsed_lines = 0         # bytes that arrived but were not measurements
    recovery_failures = 0      # consecutive failed attempts to reopen a file
    rows_at_last_status = 0    # total_rows when the last status line was cut
    last_low_space_warn = 0.0  # monotonic time of the last low-space warning
    dropped_fields = 0         # values discarded for want of a column
    files_unlinked = 0         # times the open data file was deleted under us
    statvfs_failures = 0       # consecutive unreadable free-space checks
    last_statvfs_error = None  # why, so the log can say
    _stall_logged = False
    _dropped_logged = False
    _known_keys = None         # sensor columns of the current file, as a set

    def __init__(self, args):
        self.args = args
        self.data_dir = os.path.abspath(args.data_dir)
        self.running = True

        self.time_source = TimeSource()
        self.gps = GpsReader() if args.gps else None
        self.led = LedStatus(enabled=args.led)
        self.validator = Validator()

        self.serial = None
        self.port_path = None

        self.csv_file = None
        self.csv_writer = None
        self.csv_path = None
        self.columns = None
        self.file_opened_at = 0.0
        self.last_fsync = 0.0

        self._pending = []          # rows buffered during schema discovery

        # (mtime, monotonic time we first saw it) for the export busy marker.
        self._export_mark = None

        # Health tracking. A rolling window drives the LED so a single glitch
        # does not raise an alarm but a broken instrument does.
        self.total_rows = 0
        self.total_bad = 0
        self.recent = deque(maxlen=300)   # ~30 s at 10 Hz
        self.session_start = time.monotonic()
        self.last_status_log = time.monotonic()

        self.last_watchdog = 0.0
        # Samples skipped because of an error we did not anticipate. Surfaced
        # in the periodic status line so a remote operator can see it.
        self.unexpected_errors = 0

        # Parse-liveness. Distinct from `recent`, which only ever sees samples
        # that already parsed: these two track whether anything parseable is
        # arriving at all.
        self.last_data = 0.0
        self.last_parsed = 0.0
        self.unparsed_lines = 0
        self._stall_logged = False

        self.recovery_failures = 0
        self.rows_at_last_status = 0
        self.last_low_space_warn = 0.0

        self.files_unlinked = 0
        self.statvfs_failures = 0
        self.last_statvfs_error = None

        # Values the instrument sent that this file has no column for.
        self.dropped_fields = 0
        self._dropped_logged = False
        self._known_keys = None

        # Storage guard state.
        self.disk_full = False
        self.last_disk_check = 0.0
        self.rows_dropped = 0

    # -- storage ----------------------------------------------------------

    def ensure_data_dir(self):
        try:
            if not os.path.isdir(self.data_dir):
                os.makedirs(self.data_dir)
            probe = os.path.join(self.data_dir, ".write_test")
            fh = open(probe, "w")
            fh.write("ok")
            fh.close()
            os.remove(probe)
            return True
        except (IOError, OSError) as exc:
            log.error("data directory %s not writable: %s", self.data_dir, exc)
            return False

    def free_mb(self):
        try:
            st = os.statvfs(self.data_dir)
            return (st.f_bavail * st.f_frsize) / (1024.0 * 1024.0)
        except OSError as exc:
            # Keep the reason. "No such file or directory" and "Input/output
            # error" call for completely different actions, and check_disk
            # cannot say which if the exception is thrown away here.
            self.last_statvfs_error = exc
            return -1.0

    def unique_path(self, stamp):
        """Return a data-file path that does not already exist.

        Filenames are derived from the clock, and this Pi has no RTC:
        fake-hwclock restores the SHUTDOWN time on every boot, so successive
        power cycles produce near-identical timestamps. Two boots that restore
        the same second would otherwise open the same filename in "w" mode and
        silently destroy the earlier run's data. Observed in the field on
        2026-08-07, where two boots produced names two seconds apart.
        """
        base = os.path.join(self.data_dir, "TrisonicaData_" + stamp + "Z")
        candidate = base + ".csv"
        suffix = 1
        while os.path.exists(candidate):
            candidate = "%s_%02d.csv" % (base, suffix)
            suffix += 1
            if suffix > 99:      # pathological; fall back to a pid-tagged name
                candidate = "%s_pid%d.csv" % (base, os.getpid())
                break
        return candidate

    def open_file(self):
        self.close_file()
        stamp = datetime.datetime.utcnow().strftime("%Y-%m-%d_%H%M%S")
        self.csv_path = self.unique_path(stamp)
        # "x" not "w": refuse to truncate an existing file even if the
        # uniqueness check above were somehow raced.
        self.csv_file = open(self.csv_path, "x", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        self.columns = None
        self._known_keys = None
        self._dropped_logged = False
        self._pending = []
        self.file_opened_at = time.monotonic()
        self.last_fsync = time.monotonic()
        log.info("logging to %s (%.0f MB free)", self.csv_path, self.free_mb())

    def close_file(self):
        if self.csv_file is None:
            return
        try:
            self.flush_pending()
            self.csv_file.flush()
            os.fsync(self.csv_file.fileno())
            self.csv_file.close()
            log.info("closed %s", self.csv_path)
        except (IOError, OSError) as exc:
            log.error("error closing data file: %s", exc)
        finally:
            self.csv_file = None
            self.csv_writer = None

    @staticmethod
    def order_sensor_keys(discovered):
        """Impose the canonical column order on discovered sensor fields.

        Known fields come first in the instrument's transmission order;
        anything unrecognised is appended sorted, so the schema is a pure
        function of WHICH fields appeared, never of the order in which a
        possibly-truncated first line happened to reveal them.
        """
        found = set(discovered)
        ordered = [k for k in CANONICAL_FIELDS if k in found]
        extra = sorted(found - set(CANONICAL_FIELDS))
        return ordered + extra

    def build_columns(self, sensor_keys):
        """Fixed provenance columns first, sensor fields, then GPS."""
        cols = ["timestamp_utc", "time_source", "time_synced"]
        cols.extend(self.order_sensor_keys(sensor_keys))
        cols.extend(["flags", "n_err", "n_impl", "n_spike"])
        if self.gps is not None:
            cols.extend(["lat", "lon", "alt_m", "gps_mode", "gps_sats"])
        return cols

    # Number of fixed columns before and after the sensor block. Kept as
    # methods so _emit and build_columns can never drift out of step.
    N_PREFIX = 3

    def n_suffix(self):
        return 4 + (5 if self.gps is not None else 0)

    def flush_pending(self):
        """Write buffered discovery rows once the schema is known."""
        if not self._pending:
            return
        if self.columns is None:
            keys = []
            for _, parsed, _ in self._pending:
                for key in parsed:
                    if key not in keys:
                        keys.append(key)
            self.columns = self.build_columns(keys)
            self.csv_writer.writerow(self.columns)
            log.info("schema: %s", ",".join(self.columns))
        for row in self._pending:
            self._emit(row)
        self._pending = []

    def _emit(self, row):
        stamp, parsed, extra = row
        source, synced = extra["time"]
        values = [stamp, source, 1 if synced else 0]
        # Sensor columns sit between the fixed prefix and the fixed suffix.
        sensor_cols = self.columns[self.N_PREFIX:len(self.columns) - self.n_suffix()]
        # A field the instrument only started sending AFTER this file's schema
        # was fixed has no column, and its values would otherwise be dropped
        # with nothing anywhere to say so. A missing field is already handled
        # gracefully below (blank); the reverse was silent. Cached because
        # this runs on every row.
        if self._known_keys is None:
            self._known_keys = set(sensor_cols)
        unknown = [k for k in parsed if k not in self._known_keys]
        if unknown:
            self.dropped_fields += len(unknown)
            if not self._dropped_logged:
                self._dropped_logged = True
                log.warning("instrument sent field(s) %s that were absent "
                            "during schema discovery for this file; their "
                            "values are NOT being recorded. The next file "
                            "(rotation is every %d h) will include them.",
                            ",".join(sorted(unknown)), FILE_ROTATE_HOURS)
        for key in sensor_cols:
            values.append(parsed.get(key, ""))
        values.extend([extra["flags"], extra["n_err"],
                       extra["n_impl"], extra["n_spike"]])
        if self.gps is not None:
            fix = extra["fix"]
            if fix is None:
                values.extend(["", "", "", "", extra["sats"]])
            else:
                values.extend([
                    "%.7f" % fix["lat"] if fix.get("lat") is not None else "",
                    "%.7f" % fix["lon"] if fix.get("lon") is not None else "",
                    "%.2f" % fix["alt"] if fix.get("alt") is not None else "",
                    fix.get("mode", ""),
                    extra["sats"],
                ])
        self.csv_writer.writerow(values)

    def write_sample(self, parsed):
        now = datetime.datetime.utcnow()
        stamp = now.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        flags, n_err, n_impl, n_spike, critical_bad = self.validator.check(parsed)

        fix, sats = (None, 0)
        if self.gps is not None:
            fix, sats = self.gps.current()

        extra = {
            "time": self.time_source.state,
            "flags": flags,
            "n_err": n_err,
            "n_impl": n_impl,
            "n_spike": n_spike,
            "fix": fix,
            "sats": sats,
        }
        row = (stamp, parsed, extra)

        self.total_rows += 1
        bad = 1 if critical_bad else 0
        self.total_bad += bad
        self.recent.append(bad)

        # Storage exhausted: keep reading the instrument and keep the process
        # alive (so it recovers by itself if space is freed), but write
        # nothing. Crashing here would produce a systemd restart loop.
        if self.disk_full:
            self.rows_dropped += 1
            return

        if self.columns is None:
            self._pending.append(row)
            if len(self._pending) >= HEADER_DISCOVERY_ROWS:
                self.flush_pending()
        else:
            self._emit(row)

        self.maybe_fsync()
        self.maybe_rotate()

    def maybe_fsync(self):
        now = time.monotonic()
        if now - self.last_fsync < FSYNC_INTERVAL_S:
            return
        if self.csv_file is None or self.csv_file.closed:
            return
        try:
            self.csv_file.flush()
            os.fsync(self.csv_file.fileno())
            self.last_fsync = now
        except (IOError, OSError, ValueError) as exc:
            log.error("fsync failed: %s", exc)

    def check_disk(self):
        """Stop or resume writing based on remaining space.

        Deliberately conservative: we stop with MIN_FREE_MB still available
        rather than writing until ENOSPC, because a filesystem with zero free
        space cannot even be cleanly closed, and the OS itself starts failing.
        """
        now = time.monotonic()
        if now - self.last_disk_check < DISK_CHECK_INTERVAL_S:
            return
        self.last_disk_check = now

        # Has the file been deleted from under us? Freeing space by removing
        # the data directory, or just the current CSV, is a natural thing for
        # someone to do - the card-full guidance in the README asks for
        # exactly that - and nothing here noticed. write() keeps succeeding
        # against the unlinked inode, statvfs on a missing directory only
        # reports "unknown" and this method carried on, so the logger went on
        # recording into nothing until the next rotation, up to six hours
        # later, with no warning and no change to the LED.
        if (not self.disk_full and self.csv_file is not None
                and not self.csv_file.closed):
            try:
                unlinked = os.fstat(self.csv_file.fileno()).st_nlink == 0
            except (IOError, OSError, ValueError):
                # ValueError, not just OSError: fileno() on a closed handle
                # raises "I/O operation on closed file", which is neither an
                # IOError nor an OSError and would otherwise escape check_disk
                # and kill the loop. maybe_fsync guards the same way.
                unlinked = False
            if unlinked:
                self.files_unlinked += 1
                log.error("the data file was deleted while it was being "
                          "written (%s). Everything recorded into it since "
                          "the deletion is gone. Opening a replacement.",
                          self.csv_path)
                if self.ensure_data_dir():
                    try:
                        self.open_file()
                    except (IOError, OSError) as exc:
                        log.error("could not open a replacement file (%s); "
                                  "pausing writes", exc)
                        self.disk_full = True
                else:
                    log.error("the data directory is gone and cannot be "
                              "recreated; pausing writes")
                    self.disk_full = True
                return

        free = self.free_mb()
        if free < 0:
            # Carrying on is right - an unreadable statvfs must not halt
            # recording - but it was silent, and it disables the guard that
            # stops writing before the card fills. A deployment could run for
            # weeks with that protection off and nothing anywhere saying so.
            # Rate limited the way the other repeating faults are.
            self.statvfs_failures += 1
            if self.statvfs_failures in (1, 10, 100, 1000):
                log.error("cannot read free space for %s (%s; failure %d). "
                          "The guard that stops writing before the card fills "
                          "is NOT running. Recording continues.",
                          self.data_dir, self.last_statvfs_error,
                          self.statvfs_failures)
            return
        if self.statvfs_failures:
            log.warning("free space is readable again after %d failed checks",
                        self.statvfs_failures)
            self.statvfs_failures = 0

        # Early warning: MIN_FREE_MB is roughly one day of logging, which is
        # too late to be useful to someone checking in from another country.
        # Rate limited to LOW_SPACE_WARN_INTERVAL_S - see the constant.
        if not self.disk_full and free < LOW_SPACE_WARN_MB:
            if (not self.last_low_space_warn or
                    now - self.last_low_space_warn >= LOW_SPACE_WARN_INTERVAL_S):
                self.last_low_space_warn = now
                days = free / MB_PER_DAY if MB_PER_DAY else 0
                log.warning("storage low: %.0f MB free (~%.1f days at 10 Hz)",
                            free, days)
        elif free >= LOW_SPACE_WARN_MB:
            # Space came back (a card swap, or files removed). Re-arm, so
            # crossing the threshold again warns at once instead of waiting
            # out the remainder of the interval.
            self.last_low_space_warn = 0.0

        if not self.disk_full and free < MIN_FREE_MB:
            self.disk_full = True
            log.error("STORAGE FULL: %.0f MB free (< %d MB). Stopping writes "
                      "to protect existing data. Logger stays running; "
                      "free space or swap the card to resume.",
                      free, MIN_FREE_MB)
            try:
                self.close_file()               # flush and fsync what we have
            except Exception as exc:
                log.error("could not close data file cleanly: %s", exc)
        elif self.disk_full and free > MIN_FREE_MB * 2:
            # Hysteresis: require double the threshold before resuming, so a
            # few freed megabytes cannot cause repeated start/stop churn.
            #
            # The file is opened BEFORE the alarm is cleared, and the success
            # message is logged only once it actually worked. statvfs still
            # reports free space on a filesystem that ext4 has remounted
            # read-only, so on a failing card this branch is reached on every
            # single check; announcing "storage recovered" up front wrote the
            # opposite of what had happened into the journal once a minute for
            # the rest of the deployment.
            try:
                self.open_file()
            except (IOError, OSError) as exc:
                # Space is free but the filesystem will not accept a new file.
                # Freeing more will not help - the card itself needs checking.
                # Stay stopped, and rate-limit the report the way the
                # unexpected-error path does instead of repeating it forever.
                self.recovery_failures += 1
                if self.recovery_failures in (1, 10, 100, 1000):
                    log.error("%.0f MB free but a new data file cannot be "
                              "opened (%s) - the card is most likely "
                              "read-only; staying stopped (attempt %d)",
                              free, exc, self.recovery_failures)
                return
            self.disk_full = False
            self.recovery_failures = 0
            log.warning("storage recovered: %.0f MB free, resuming "
                        "(%d rows were dropped)", free, self.rows_dropped)

    def maybe_watchdog(self):
        """Tell systemd we are still making progress."""
        now = time.monotonic()
        if now - self.last_watchdog < WATCHDOG_PING_INTERVAL_S:
            return
        self.last_watchdog = now
        sd_notify("WATCHDOG=1")

    def maybe_rotate(self):
        if FILE_ROTATE_HOURS <= 0:
            return
        if time.monotonic() - self.file_opened_at >= FILE_ROTATE_HOURS * 3600.0:
            log.info("rotating data file after %d h", FILE_ROTATE_HOURS)
            # Rows still buffered for schema discovery are safe across this:
            # open_file() closes the outgoing file first, and close_file()
            # flushes the buffer into it before closing. Covered by
            # TestRotationKeepsBufferedRows.
            self.open_file()

    # -- device -----------------------------------------------------------

    def find_port(self):
        """Locate the anemometer, preferring the stable by-id path.

        Deliberately excludes /dev/serial* and /dev/ttyAMA* so the logger can
        never grab the GPS UART, which gpsd owns.
        """
        if self.args.port != "auto":
            return self.args.port if os.path.exists(self.args.port) else None

        by_id = sorted(glob.glob("/dev/serial/by-id/*CP2102*"))
        if by_id:
            return by_id[0]
        by_id = sorted(glob.glob("/dev/serial/by-id/*"))
        for path in by_id:
            # Both halves case-normalized. The u-blox check used to be
            # case-sensitive while the GPS one was not, so a by-id name
            # rendered 'U-BLOX_AG_...' passed both tests and was opened as the
            # anemometer - seizing the UART gpsd owns, which is the single
            # thing this function exists to prevent.
            upper = path.upper()
            if "GPS" not in upper and "U-BLOX" not in upper:
                return path
        tty = sorted(glob.glob("/dev/ttyUSB*"))
        return tty[0] if tty else None

    def connect(self):
        path = self.find_port()
        if path is None:
            return False
        try:
            self.serial = serial.Serial(path, self.args.baud,
                                        timeout=SERIAL_READ_TIMEOUT_S)
            self.serial.reset_input_buffer()
            self.port_path = path
            # Start the liveness clocks here: the grace period before we
            # declare "connected but silent" belongs to this connection, not
            # to whenever the process happened to start.
            self.last_data = time.monotonic()
            self.last_parsed = time.monotonic()
            self._stall_logged = False
            log.info("connected to %s at %d baud", path, self.args.baud)
            return True
        except (serial.SerialException, OSError) as exc:
            log.warning("cannot open %s: %s", path, exc)
            self.serial = None
            return False

    def disconnect(self):
        if self.serial is not None:
            try:
                self.serial.close()
            except Exception:
                pass
            self.serial = None
            self.port_path = None
        # References are stale across a gap; comparing the first sample after
        # a reconnection against a minutes-old value would be meaningless.
        self.validator.reset()

    # -- health -----------------------------------------------------------

    def bad_rate(self):
        if not self.recent:
            return 0.0
        return float(sum(self.recent)) / len(self.recent)

    def note_parsed(self):
        """Record that a sample parsed: the instrument is producing data."""
        self.last_parsed = time.monotonic()
        if self._stall_logged:
            log.warning("instrument output is readable again after %d "
                        "unreadable lines - recording resumes",
                        self.unparsed_lines)
            self._stall_logged = False

    def note_unparsed(self):
        """Record bytes that arrived but were not a measurement sentence."""
        self.unparsed_lines += 1

    def data_stalled(self):
        """True when bytes are arriving but none of them are measurements.

        The stale-data reconnect in run() keys on BYTES, so an instrument that
        has dropped into its configuration menu, or is being read at the wrong
        baud rate, keeps that timer fresh forever. Nothing is written, so
        `recent` never sees a sample and bad_rate() stays at zero - which used
        to leave the LED showing READY while the deployment recorded nothing.
        This is the check that makes that state visible.

        A zero reference means no connection has been established yet (the
        class default), which is not a stall.

        Bytes must actually be ARRIVING for this to mean anything. Without
        that test the check also fires when the logger itself was blocked: a
        slow card can hold it in fsync for tens of seconds, during which
        nothing is read and last_parsed ages for a reason that has nothing to
        do with the instrument. Observed on the unit 2026-08-12 - four alarms
        under heavy I/O, each followed within 20 ms by a successful parse,
        each blaming the baud rate for a healthy anemometer. When no bytes are
        arriving at all, the stale-data reconnect in run() owns the case.
        """
        if self.serial is None or not self.last_parsed:
            return False
        now = time.monotonic()
        if not self.last_data or now - self.last_data > STALE_DATA_TIMEOUT_S:
            return False
        return (now - self.last_parsed) > NO_PARSE_TIMEOUT_S

    def export_busy(self):
        """True while a USB export is actually running.

        Presence alone is not enough. The export service cannot delete its
        own marker if it is killed mid-copy, and this state outranks every
        other one, so a leftover file would report "copying" - and hide a
        full disk or a missing anemometer - until the next reboot. The
        export refreshes the marker as it works, so one that has stopped
        changing is treated as abandoned.

        The mtime is compared for EQUALITY only; the elapsed time is measured
        with the monotonic clock. Using the wall clock here would let the
        first GPS correction of the deployment jump the apparent age past the
        limit and drop the EXPORTING light in the middle of a live copy -
        precisely when the researcher is deciding whether to pull the stick.
        """
        try:
            mtime = os.path.getmtime(EXPORT_BUSY_MARKER)
        except OSError:
            self._export_mark = None
            return False
        now = time.monotonic()
        if self._export_mark is None or self._export_mark[0] != mtime:
            self._export_mark = (mtime, now)      # appeared, or just refreshed
            return True
        return (now - self._export_mark[1]) < EXPORT_BUSY_STALE_S

    def update_led(self):
        """Map system state to the LED, worst problem first.

        The distinction between READY and NO_TIME is the point of this
        redesign: previously the logger showed "all good" the moment data
        started flowing, even while the clock was still free-running. A
        researcher would begin a measurement believing everything was fine
        while the timestamps were not yet trustworthy.
        """
        # 1. A USB export is running. Outranks everything because a human is
        #    standing there deciding whether to pull the stick, and removing
        #    it mid-copy corrupts the export.
        if self.export_busy():
            self.led.set(LedStatus.EXPORTING)
            return
        # 2. Data is being LOST.
        if self.disk_full:
            self.led.set(LedStatus.DISK_FULL)
            return
        # 3. No instrument at all.
        if self.serial is None:
            self.led.set(LedStatus.WAITING)
            return
        # 4. Instrument connected but not producing usable measurements:
        #    either samples that parse and fail validation, or bytes that are
        #    not measurements at all. The second case used to fall through to
        #    READY and report "everything good" while nothing was recorded.
        stalled = self.data_stalled()
        if stalled and not self._stall_logged:
            self._stall_logged = True
            log.error("connected to %s but nothing parseable for %.0fs "
                      "(%d unreadable lines) - NOTHING IS BEING RECORDED. "
                      "Check the baud rate and that the instrument is not "
                      "sitting in its configuration menu.",
                      self.port_path, NO_PARSE_TIMEOUT_S, self.unparsed_lines)
        if self.bad_rate() > 0.5 or stalled:
            self.led.set(LedStatus.BAD_DATA)
            return
        # 5. Logging fine, but timestamps are not yet verified.
        _source, synced = self.time_source.state
        if not synced:
            self.led.set(LedStatus.NO_TIME)
            return
        # 6. Everything good.
        self.led.set(LedStatus.READY)

    def log_status(self):
        now = time.monotonic()
        interval = now - self.last_status_log
        if interval < STATUS_LOG_INTERVAL_S:
            return
        self.last_status_log = now
        # Rate over the interval that just ended, not since process start. A
        # lifetime average stays depressed for hours after any outage, which
        # hides exactly the current-rate drop someone checking in remotely is
        # looking for.
        rows_since = self.total_rows - self.rows_at_last_status
        self.rows_at_last_status = self.total_rows
        rate = rows_since / interval if interval > 0 else 0.0
        source, synced = self.time_source.state
        pct = (100.0 * self.total_bad / self.total_rows) if self.total_rows else 0.0
        fix, sats = (None, 0)
        if self.gps is not None:
            fix, sats = self.gps.current()
        # "nofix" and "cannot talk to gpsd at all" call for opposite actions -
        # give it sky, versus check the receiver - and used to read the same.
        if self.gps is None:
            gps_state = "off"
        elif fix:
            gps_state = "fix"
        elif self.gps.connected:
            gps_state = "nofix"
        else:
            gps_state = "unreachable"
        if self.disk_full:
            log.error("status: STORAGE FULL - %d rows dropped, %.0f MB free",
                      self.rows_dropped, self.free_mb())
        if self.unexpected_errors:
            log.warning("status: %d samples skipped by unexpected errors",
                        self.unexpected_errors)
        if self.unparsed_lines:
            log.warning("status: %d lines arrived that were not measurements",
                        self.unparsed_lines)
        if self.dropped_fields:
            log.warning("status: %d field values dropped for want of a column "
                        "in this file", self.dropped_fields)
        if self.files_unlinked:
            log.warning("status: %d data file(s) were deleted while being "
                        "written", self.files_unlinked)
        log.info("status: %d rows, %.2f Hz now, %.2f%% bad, "
                 "time=%s(synced=%s), gps=%s sats=%d, %.0f MB free",
                 self.total_rows, rate, pct, source, synced,
                 gps_state, sats, self.free_mb())

    # -- main loop --------------------------------------------------------

    def run(self):
        if not self.ensure_data_dir():
            return 1

        self.time_source.start()
        if self.gps is not None:
            self.gps.start()
        self.led.start()
        try:
            self.open_file()
        except (IOError, OSError) as exc:
            # Full or read-only card at boot. Start in the stopped state so
            # the LED shows the alarm and check_disk() can recover later,
            # instead of crash-looping every 5 s with nothing to see.
            log.error("cannot open a data file at startup (%s) - "
                      "starting in storage-alarm state", exc)
            self.disk_full = True

        log.info("field logger started (pid %d)", os.getpid())
        if sd_notify("READY=1"):
            log.info("systemd watchdog active (ping every %.0fs)",
                     WATCHDOG_PING_INTERVAL_S)
        self.last_data = time.monotonic()
        last_scan = 0.0

        while self.running:
            if self.serial is None:
                now = time.monotonic()
                if now - last_scan >= DEVICE_SCAN_INTERVAL_S:
                    last_scan = now
                    # connect() sets both liveness clocks on success, so a
                    # fresh connection always starts with a clean grace period.
                    if not self.connect():
                        log.debug("anemometer not present, waiting")
                self.maybe_watchdog()
                self.update_led()
                time.sleep(0.5)
                continue

            try:
                raw = self.serial.readline(MAX_LINE_BYTES)
            except (serial.SerialException, OSError) as exc:
                log.warning("serial read failed (%s) - reconnecting", exc)
                self.disconnect()
                self.update_led()
                continue

            if raw:
                self.last_data = time.monotonic()
                line = raw.decode("ascii", "replace").strip()
                parsed = parse_line(line)
                if len(parsed) >= 3:
                    self.note_parsed()
                    try:
                        self.write_sample(parsed)
                    except (IOError, OSError) as exc:
                        err = getattr(exc, "errno", None)
                        if err == errno.EROFS:
                            # The card went read-only, almost always because
                            # ext4 hit an I/O error. Distinct from "full":
                            # freeing space will not help, the card needs
                            # checking or replacing.
                            if not self.disk_full:
                                log.error("FILESYSTEM IS READ-ONLY (EROFS) - "
                                          "the SD card has likely developed "
                                          "errors. Writes stopped; existing "
                                          "data preserved. Check dmesg and "
                                          "fsck the card.")
                                self.disk_full = True
                                try:
                                    self.close_file()
                                except Exception:
                                    pass
                        elif err == errno.ENOSPC:
                            # Out of space. Do NOT try to open another file:
                            # that fails too, and the resulting exception
                            # would crash-loop the service forever.
                            if not self.disk_full:
                                log.error("STORAGE FULL (ENOSPC) - stopping "
                                          "writes, logger stays running")
                                self.disk_full = True
                                try:
                                    self.close_file()
                                except Exception:
                                    pass
                        else:
                            log.error("write failed: %s - starting a new file",
                                      exc)
                            try:
                                self.open_file()
                            except (IOError, OSError) as exc2:
                                log.error("cannot open a replacement file "
                                          "(%s); pausing writes", exc2)
                                self.disk_full = True
                    except Exception:
                        # Last resort. Anything not anticipated above would
                        # otherwise propagate out of the loop and kill the
                        # process; with StartLimitIntervalSec=0 systemd would
                        # then restart it every 5 s forever. If the trigger is
                        # deterministic that ends data collection permanently,
                        # with nobody present. Skip the sample instead and
                        # keep going - a lost row is recoverable, a dead
                        # logger is not.
                        self.unexpected_errors += 1
                        if self.unexpected_errors in (1, 10, 100, 1000):
                            log.exception(
                                "unexpected error writing a sample "
                                "(occurrence %d) - skipping it and continuing",
                                self.unexpected_errors)
                else:
                    # Bytes that are not a measurement sentence. Counted
                    # rather than dropped on the floor: this is the only
                    # evidence that a connected instrument has stopped
                    # producing data, and update_led() acts on it.
                    self.note_unparsed()
            elif time.monotonic() - self.last_data > STALE_DATA_TIMEOUT_S:
                log.warning("no data for %.0fs - reconnecting",
                            STALE_DATA_TIMEOUT_S)
                self.disconnect()

            self.check_disk()
            self.maybe_watchdog()
            self.update_led()
            self.log_status()

        return 0

    def shutdown(self):
        log.info("shutting down")
        sd_notify("STOPPING=1")
        self.running = False
        self.disconnect()
        self.close_file()
        self.led.stop()
        self.time_source.stop()
        if self.gps is not None:
            self.gps.stop()
        elapsed = time.monotonic() - self.session_start
        log.info("session: %d rows in %s, %d flagged",
                 self.total_rows,
                 str(datetime.timedelta(seconds=int(elapsed))),
                 self.total_bad)


# --------------------------------------------------------------------------

def setup_logging(verbose):
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(stream)


def main():
    parser = argparse.ArgumentParser(
        description="Unattended TriSonica field datalogger")
    parser.add_argument("--data-dir", default="/home/pi/trisonica-data",
                        help="where CSV files are written")
    parser.add_argument("--port", default="auto",
                        help="serial port, or 'auto' to detect")
    parser.add_argument("--baud", type=int, default=BAUD_RATE)
    parser.add_argument("--no-gps", dest="gps", action="store_false",
                        default=True, help="do not read position from gpsd")
    parser.add_argument("--no-led", dest="led", action="store_false",
                        default=True, help="do not drive the status LED")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    logger = FieldLogger(args)

    def handle_signal(signum, frame):
        # Only set the flag; all teardown happens on the main thread so the
        # file is closed and fsync'd exactly once.
        logger.running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        return logger.run()
    finally:
        logger.shutdown()


if __name__ == "__main__":
    sys.exit(main())

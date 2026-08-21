#!/usr/bin/env python3
"""Dead-man's switch for the TriSonica field logger.

Pings an external monitor every few minutes for as long as the station is
healthy.  Silence is the alarm:

    healthy            -> ping, monitor stays quiet
    recording stopped  -> ping /fail, monitor emails at once
    Pi dead, card dead,
    network gone       -> no ping at all, monitor's timeout emails

That last row is the point, and it is why the check cannot live on the Pi
alone.  A unit that emails its own bad news says nothing when it is the unit
that failed, and "no email" then looks exactly like "everything is fine" --
which is the failure this station is most likely to have and least likely to
notice, sitting on a roof nobody visits.

The verdict comes from the dashboard's own /api/status rather than being
recalculated here, so what the monitor alerts on and what the web page shows
can never drift apart.  It also means this check covers the dashboard: if the
page a researcher relies on has stopped answering, that is itself worth an
email.

Configure by writing the monitor's URL to /etc/trisonica-alert.conf:

    PING_URL=https://hc-ping.com/your-uuid-here

Unconfigured, it does nothing and says so once -- deploying it before the
monitor exists is harmless.

Targets Python 3.7 (Raspbian Buster) -- stdlib only, no pip dependencies.
"""

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request

DEFAULT_CONFIG = "/etc/trisonica-alert.conf"
DEFAULT_STATUS_URL = "http://127.0.0.1:8080/api/status"

# systemd's StateDirectory= puts us here; the fallback is for running by hand.
STATE_DIR = os.environ.get("STATE_DIRECTORY") or "/var/lib/trisonica-alert"
COUNTER_FILE = "counter"

# A 'warn' has to persist this many checks before it raises an alarm. At the
# 5-minute timer interval that is half an hour. Warnings are real - a card
# with days left needs a site visit booked - but several of them appear for a
# minute at a time during a restart, and an alarm that cries wolf is an alarm
# that gets filtered into a folder nobody reads.
ESCALATE_AFTER = 6

HTTP_TIMEOUT_S = 15

log = logging.getLogger("trisonica-alert")


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


def read_counter(state_dir):
    """How many consecutive checks have been unhealthy."""
    try:
        with open(os.path.join(state_dir, COUNTER_FILE)) as fh:
            return int(fh.read().strip() or 0)
    except (IOError, OSError, ValueError):
        return 0


def write_counter(state_dir, count):
    """Persist the run of unhealthy checks; failure here is not fatal."""
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, COUNTER_FILE), "w") as fh:
            fh.write("%d" % count)
    except (IOError, OSError) as exc:
        log.warning("cannot record state in %s (%s) - a warning will need to "
                    "re-accumulate from zero", state_dir, exc)


def fetch_status(url):
    """Return the dashboard's status dict, or None if it cannot be read."""
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("cannot read %s (%s)", url, exc)
        return None


def decide(status, count):
    """Return (alarm, summary, new_count) for a status dict.

    *status* is None when the dashboard could not be read at all. That is
    treated like a warning rather than an immediate alarm: the status service
    restarts itself within 30 s, and a single missed poll during that window
    is not worth waking anyone.
    """
    if status is None:
        count += 1
        summary = ("The status dashboard on the Pi is not answering "
                   "(check %d of %d before this raises the alarm)"
                   % (count, ESCALATE_AFTER))
        return count >= ESCALATE_AFTER, summary, count

    health = status.get("health") or {}
    level = health.get("level", "unknown")
    reasons = health.get("reasons") or []

    if level == "ok":
        return False, "Recording normally.", 0

    detail = "\n".join("- %s" % r for r in reasons) or "- no reason given"

    if level == "bad":
        return True, "MEASUREMENTS ARE NOT BEING RECORDED\n\n%s" % detail, 0

    # warn, or a level this version does not recognise
    count += 1
    if count >= ESCALATE_AFTER:
        return True, ("Unresolved for %d consecutive checks:\n\n%s"
                      % (count, detail)), count
    return False, ("Attention needed (check %d of %d):\n\n%s"
                   % (count, ESCALATE_AFTER, detail)), count


def build_body(status, summary):
    """Compose the text the monitor will put in its email."""
    lines = [summary, ""]
    if status:
        system = status.get("system") or {}
        recording = status.get("recording") or {}
        disk = status.get("disk") or {}
        health = status.get("health") or {}
        age = health.get("data_age_s")
        lines.append("station : %s, %s"
                     % (system.get("hostname", "?"), system.get("uptime", "?")))
        if age is not None:
            lines.append("last row: %.0f s ago (%s)"
                         % (age, recording.get("newest_file", "?")))
        if disk.get("free_mb") is not None:
            lines.append("card    : %.0f MB free, about %s days"
                         % (disk["free_mb"], disk.get("estimated_days", "?")))
        lines.append("checked : %s" % system.get("timestamp_utc", "?"))
    return "\n".join(lines)


def ping(base_url, body, alarm):
    """Send the heartbeat. Returns True if the monitor accepted it.

    The /fail suffix is the convention used by healthchecks.io and by the
    push endpoints of most self-hosted equivalents; it turns the next email
    from "we heard nothing" into "it told us it is broken", which is a much
    more useful thing to read on a phone.
    """
    url = base_url.rstrip("/") + "/fail" if alarm else base_url
    data = body.encode("utf-8")[:10000]  # monitors cap the body they keep
    try:
        request = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as exc:
        log.warning("monitor rejected the ping: HTTP %s", exc.code)
    except Exception as exc:
        # Nothing to escalate to. The monitor notices the missing ping by
        # itself, which is exactly what it is for.
        log.warning("cannot reach the monitor (%s)", exc)
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Heartbeat the TriSonica station to an external monitor")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="file holding PING_URL (default: %(default)s)")
    parser.add_argument("--status-url", default=DEFAULT_STATUS_URL,
                        help="dashboard JSON endpoint (default: %(default)s)")
    parser.add_argument("--state-dir", default=STATE_DIR,
                        help="where the warning counter is kept")
    parser.add_argument("--dry-run", action="store_true",
                        help="decide and print, but do not ping")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    config = read_config(args.config)
    ping_url = config.get("PING_URL", "")
    if not ping_url and not args.dry_run:
        # Not an error: the unit is deployed before the monitor is created.
        log.info("no PING_URL in %s - nothing to report to. Add one to start "
                 "alerting.", args.config)
        return 0

    status = fetch_status(args.status_url)
    count = read_counter(args.state_dir)
    alarm, summary, count = decide(status, count)
    write_counter(args.state_dir, count)
    body = build_body(status, summary)

    if args.dry_run:
        print("alarm: %s\n%s" % (alarm, body))
        return 0

    if ping(ping_url, body, alarm):
        log.info("%s: %s", "ALARM sent" if alarm else "heartbeat sent",
                 summary.splitlines()[0])
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

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
import getpass
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_CONFIG = "/etc/trisonica-alert.conf"

# The dashboard's own config, read so that this check follows the dashboard
# when it moves. When PUBLIC_PREFIX is set the status endpoint is no longer at
# /api/status, and a checker that keeps asking the old path gets a 404 and
# reports the station as dead -- an alarm caused entirely by the alarm.
STATUS_CONFIG = "/etc/trisonica-status.conf"
DEFAULT_STATUS_BASE = "http://127.0.0.1:8080"
STATUS_PATH = "/api/status"

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


def config_unreadable(path):
    """True if the file exists but this process cannot read it.

    Worth separating from "not configured yet". This service runs as an
    unprivileged user, and a config written `chmod 600` while owned by root --
    which is the obvious thing to do to a file holding a secret, and what the
    documentation used to advise -- is silently invisible to it. The symptom is
    "no PING_URL", identical to a station whose monitor has not been set up,
    and alerting stays off while looking configured to whoever wrote the file.
    """
    return os.path.exists(path) and not os.access(path, os.R_OK)


def build_status_url(status_config=STATUS_CONFIG, base=DEFAULT_STATUS_BASE):
    """Where the dashboard's JSON lives, following its public prefix."""
    prefix = (read_config(status_config).get("PUBLIC_PREFIX") or "").strip()
    prefix = prefix.strip("/")
    if prefix:
        return "%s/%s%s" % (base.rstrip("/"), prefix, STATUS_PATH)
    return base.rstrip("/") + STATUS_PATH


def points_at_itself(ping_url, status_url):
    """True if the 'external monitor' is actually this same station.

    Worth refusing rather than attempting. The entire value of this check is
    that it reports *outward*: a unit that mails its own bad news says nothing
    when the unit itself is what failed, and 'no email' then looks exactly like
    'everything is fine'. A PING_URL on loopback, or on the dashboard's own
    address, silently converts the dead-man's switch into a component that can
    only ever fail with the thing it is watching -- which is the one shape this
    check must never have.
    """
    try:
        ping = urllib.parse.urlparse(ping_url)
        status = urllib.parse.urlparse(status_url)
    except ValueError:
        return False
    host = (ping.hostname or "").lower()
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    return bool(host) and (ping.netloc.lower() == status.netloc.lower())


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
    parser.add_argument("--status-url",
                        help="dashboard JSON endpoint (default: derived from "
                             "%s so it follows PUBLIC_PREFIX)" % STATUS_CONFIG)
    parser.add_argument("--status-config", default=STATUS_CONFIG,
                        help="dashboard config to read PUBLIC_PREFIX from")
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
    status_url = args.status_url or build_status_url(args.status_config)

    if not ping_url and config_unreadable(args.config):
        # Distinct from "not configured yet", and much worse: somebody has set
        # alerting up and it is off anyway.
        log.error("%s exists but is not readable by this service (running as "
                  "%s). Alerting is OFF. Fix with: sudo chown root:%s %s && "
                  "sudo chmod 640 %s",
                  args.config, getpass.getuser(), getpass.getuser(),
                  args.config, args.config)
        return 1

    if not ping_url and not args.dry_run:
        # Not an error: the unit is deployed before the monitor is created.
        log.info("no PING_URL in %s - nothing to report to. Add one to start "
                 "alerting.", args.config)
        return 0

    if ping_url and points_at_itself(ping_url, status_url):
        # Refuse loudly and do nothing else. Pinging on would look armed in
        # the journal while being incapable of reporting the failures this
        # exists for.
        log.error("PING_URL in %s points at this station (%s). A dead-man's "
                  "switch has to report to something that stays up when this "
                  "unit does not - a healthchecks.io check or equivalent. "
                  "Not pinging.", args.config, ping_url)
        return 1

    status = fetch_status(status_url)
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

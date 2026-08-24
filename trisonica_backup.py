#!/usr/bin/env python3
"""Off-site collector for the TriSonica field logger.

Runs on the HOMESERVER (mm12), not on the Pi, and pulls.  Everything else
about this script follows from that one decision, so it is worth stating why.

The Pi sits on a roof, on a network nobody in this project controls, and it is
the machine most likely to be stolen, reimaged or handed to a technician.  If
it pushed to the homeserver it would have to carry a credential that can write
there, and whoever ends up with the card would inherit the ability to corrupt
or erase the archive -- the one copy that exists precisely because the field
unit is not trustworthy.  Pulling inverts that: the Pi holds no secret at all,
and the key that does exist lives on the homeserver, is restricted to
read-only rsync below /home/pi, and cannot get a shell (see
trisonica-backup-shell).

It also never deletes.  Not on the Pi -- the key cannot write -- and not here:
there is no --delete, so a file that vanishes from the card stays in the
archive.  A mirror that faithfully reproduces a deletion is not a backup.

The SD card holds about a month; this closes that gap without touching the
recording path, which stays exactly as it was.  Nothing here can stop the
logger, and if the homeserver is off for a week the only consequence is a week
of catching up.

This file carries no site-specific values. Point it at your station by writing
/etc/trisonica-backup.conf on the collector:

    REMOTE=pi@<station-tailscale-ip-or-host>
    DEST=/path/to/your/archive
    SSH_KEY=/home/<you>/.ssh/id_ed25519

Prefer the station's Tailscale IP over its MagicDNS name: a node's Tailscale
address is stable for the life of the node, while name resolution is one more
service that can be down at 03:00 for reasons that have nothing to do with the
station.

Stdlib only, no pip dependencies.
"""

import argparse
import errno
import fcntl
import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import time

DEFAULT_CONFIG = "/etc/trisonica-backup.conf"

# One run at a time. The timer alone does not guarantee this: a slow first
# pull, or someone running the script by hand to see what it does, can overlap
# the scheduled one, and two rsyncs writing the same partial file is how an
# archive acquires a truncated CSV.
#
# It lives in the archive directory rather than /tmp because the systemd unit
# sets PrivateTmp=true: a lock in /tmp would be a *different* file for the
# scheduled run and for someone running this by hand, which is precisely the
# collision the lock exists to prevent.
LOCK_NAME = ".backup.lock"

# rsync exit codes that mean "the station was not reachable" rather than "the
# backup is broken". A rooftop Pi on a campus network drops off for entirely
# ordinary reasons; treating that as a service failure would put the unit in
# a restart loop and train whoever reads the journal to ignore it.
UNREACHABLE_EXITS = (30, 35, 255)

# Deliberately unusable placeholders. The real station address and archive path
# are site-specific and live in /etc/trisonica-backup.conf, so that this file is
# the same on every collector and publishing it discloses nothing about where
# any particular station is.
DEFAULT_REMOTE = "pi@station.example"
DEFAULT_DEST = "/srv/trisonica"

# Paths are relative to the rrsync root (/home/pi), not absolute: the forced
# command confines the key to that subtree and rewrites paths accordingly.
DEFAULT_SOURCE = "trisonica-data"
DEFAULT_SUBDIR = "data"

STATUS_FILE = "backup-status.json"

# Generous: a first run copies the whole card over a link that may cross
# continents, and being killed halfway is worse than being slow. --partial-dir
# means even a timeout leaves progress behind for the next run.
DEFAULT_TIMEOUT_S = 6 * 3600

# KB/s ceiling on the transfer. This is the only throttle that actually works
# on the station: the Pi's kernel uses the mq-deadline I/O scheduler, and
# ionice classes are implemented by CFQ and BFQ only, so the `ionice -c 3` in
# the forced command is a no-op there. A cap on the rate is scheduler-agnostic.
#
# 2 MB/s is far above what an hourly run needs (~4.5 MB of appended CSV) and
# far below what the card can deliver, so in normal operation it never binds.
# It exists for the two cases that do bite: the first full pull, and a
# collector on the same LAN as the station, where rsync would otherwise read
# as fast as the card allows while a 10 Hz recording is in progress.
DEFAULT_BWLIMIT_KBPS = 2000

log = logging.getLogger("trisonica-backup")


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


def build_rsync_command(remote, source, dest_dir, ssh_key, checksum=False,
                        bwlimit=DEFAULT_BWLIMIT_KBPS):
    """Assemble the rsync invocation.

    Flag choices worth defending:

    -rlt    recursive, keep symlinks and mtimes.  Deliberately *not* -a:
            that adds -pgoD, and copying the Pi's uid/gid and permission bits
            onto a different machine's filesystem creates ownership noise for
            no benefit.  mtimes matter (they order the archive); owners do not.
    -z      the payload is CSV, which compresses roughly 7:1 on this data --
            the README measures 222 MB of it arriving as a 32 MB tar.gz.
    --partial-dir
            an interrupted transfer resumes instead of restarting.  The active
            file is up to 27 MB and the first run is the whole card.
    --checksum
            only on demand.  The routine run trusts size+mtime, which is right
            for append-only files; --checksum re-reads every byte on the card
            and is a monthly audit, not an hourly job.

    No --delete, by design: see the module docstring.
    """
    ssh_cmd = [
        "ssh",
        "-i", os.path.expanduser(ssh_key),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=20",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
    ]
    cmd = [
        "rsync",
        "-rlt",
        "-z",
        "--partial-dir=.rsync-partial",
        "--stats",
        "--human-readable",
        "-e", " ".join(shlex.quote(part) for part in ssh_cmd),
    ]
    if bwlimit:
        cmd.append("--bwlimit=%d" % bwlimit)
    if checksum:
        cmd.append("--checksum")
    cmd.append("%s:%s/" % (remote, source.rstrip("/")))
    cmd.append(dest_dir.rstrip("/") + "/")
    return cmd


def parse_stats(output):
    """Pull the few numbers worth keeping out of rsync --stats."""
    stats = {}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Number of regular files transferred:"):
            stats["files_transferred"] = _int_after(line)
        elif line.startswith("Number of files:"):
            stats["files_total"] = _int_after(line)
        elif line.startswith("Total transferred file size:"):
            stats["transferred"] = line.split(":", 1)[1].strip()
        elif line.startswith("Total file size:"):
            stats["archive_size"] = line.split(":", 1)[1].strip()
    return stats


def _int_after(line):
    """First integer after the colon, commas stripped. None if absent."""
    try:
        return int(line.split(":", 1)[1].strip().split()[0].replace(",", ""))
    except (IndexError, ValueError):
        return None


def summarize_dest(dest_dir):
    """Count and measure what the archive actually holds now.

    Read from the filesystem rather than from rsync's report: this answers
    "what do I have" even on a run that transferred nothing, and it is the
    number worth trusting after a failure.
    """
    newest_name, newest_mtime, count, total = None, 0.0, 0, 0
    try:
        for name in os.listdir(dest_dir):
            path = os.path.join(dest_dir, name)
            if not name.endswith(".csv") or not os.path.isfile(path):
                continue
            stat = os.stat(path)
            count += 1
            total += stat.st_size
            if stat.st_mtime > newest_mtime:
                newest_mtime, newest_name = stat.st_mtime, name
    except (IOError, OSError) as exc:
        log.warning("cannot inspect %s (%s)", dest_dir, exc)
    return {
        "files": count,
        "bytes": total,
        "newest_file": newest_name,
        "newest_mtime": newest_mtime or None,
    }


def read_status(dest_root):
    """Previous outcome, or {} if there is none to read."""
    try:
        with open(os.path.join(dest_root, STATUS_FILE)) as fh:
            return json.load(fh)
    except (IOError, OSError, ValueError):
        return {}


def write_status(dest_root, payload):
    """Record the outcome where something else can poll it.

    A backup nobody checks is indistinguishable from one that stopped a month
    ago, which is the same failure the station's own dead-man's switch exists
    to prevent. Written atomically so a reader never catches a half-file.
    """
    path = os.path.join(dest_root, STATUS_FILE)
    tmp = path + ".tmp"
    try:
        os.makedirs(dest_root, exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except (IOError, OSError) as exc:
        log.warning("cannot write %s (%s)", path, exc)


def acquire_lock(dest_root):
    """Take the run lock, or return None if another run holds it.

    The handle is returned and must stay referenced for the lifetime of the
    run: closing it, including by garbage collection, releases the lock.
    """
    path = os.path.join(dest_root, LOCK_NAME)
    try:
        os.makedirs(dest_root, exist_ok=True)
        handle = open(path, "w")
    except (IOError, OSError) as exc:
        log.warning("cannot open lock file %s (%s) - continuing unlocked",
                    path, exc)
        return None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError) as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            handle.close()
            return None
        raise
    return handle


def run_backup(remote, source, dest_root, subdir, ssh_key,
               checksum=False, timeout=DEFAULT_TIMEOUT_S, dry_run=False,
               bwlimit=DEFAULT_BWLIMIT_KBPS):
    """Pull once. Returns (outcome, status_dict)."""
    dest_dir = os.path.join(dest_root, subdir)
    cmd = build_rsync_command(remote, source, dest_dir, ssh_key, checksum,
                              bwlimit)

    if dry_run:
        print(" ".join(shlex.quote(part) for part in cmd))
        return True, {}

    try:
        os.makedirs(dest_dir, exist_ok=True)
    except (IOError, OSError) as exc:
        log.error("cannot create %s (%s)", dest_dir, exc)
        return False, {"error": str(exc)}

    started = time.time()
    log.info("pulling %s:%s -> %s", remote, source, dest_dir)
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout)
        output = proc.stdout.decode("utf-8", "replace")
        code = proc.returncode
    except subprocess.TimeoutExpired:
        log.error("rsync exceeded %d s and was killed; partial progress is "
                  "kept and the next run resumes it", timeout)
        output, code = "", -1
    except (IOError, OSError) as exc:
        log.error("cannot run rsync (%s)", exc)
        output, code = str(exc), -1

    elapsed = time.time() - started
    if code == 0:
        outcome = "ok"
    elif code in UNREACHABLE_EXITS:
        outcome = "unreachable"
    else:
        outcome = "failed"

    status = {
        "outcome": outcome,
        "ok": outcome == "ok",
        "rsync_exit": code,
        "started_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC",
                                     time.gmtime(started)),
        "finished_epoch": time.time(),
        "duration_s": round(elapsed, 1),
        "remote": remote,
        "source": source,
        "dest": dest_dir,
        "checksum_pass": bool(checksum),
        "bwlimit_kbps": bwlimit or 0,
        "collector": socket.gethostname(),
    }
    status.update(parse_stats(output))
    status["archive"] = summarize_dest(dest_dir)

    if outcome == "ok":
        log.info("ok in %.0f s: %s file(s) transferred, archive now %d file(s)",
                 elapsed, status.get("files_transferred", "?"),
                 status["archive"]["files"])
    elif outcome == "unreachable":
        # Expected, not exceptional. The archive is still whole; it is just
        # not growing. How long that has been true is the thing worth
        # noticing, and last_success_epoch in the status file carries it.
        log.warning("station not reachable (rsync exit %s) - archive is "
                    "intact but no longer current; will retry", code)
    else:
        # Keep rsync's own words: its exit codes are specific (23 = partial
        # transfer, 24 = a file vanished mid-run) and paraphrasing loses that.
        log.error("rsync failed (exit %s) after %.0f s", code, elapsed)
        for line in output.strip().splitlines()[-8:]:
            log.error("  %s", line)
        status["error_tail"] = output.strip().splitlines()[-8:]

    return outcome, status


def main():
    parser = argparse.ArgumentParser(
        description="Pull the TriSonica station's recordings to this machine")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="file holding overrides (default: %(default)s)")
    parser.add_argument("--remote", help="user@host of the station")
    parser.add_argument("--source",
                        help="path on the station, relative to the rrsync "
                             "root (default: %s)" % DEFAULT_SOURCE)
    parser.add_argument("--dest", help="archive root on this machine")
    parser.add_argument("--subdir",
                        help="subdirectory of --dest to fill (default: %s)"
                             % DEFAULT_SUBDIR)
    parser.add_argument("--ssh-key", help="private key for the backup account")
    parser.add_argument("--checksum", action="store_true",
                        help="re-verify every byte instead of trusting "
                             "size+mtime (slow; monthly audit)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                        help="seconds before rsync is killed")
    parser.add_argument("--bwlimit", type=int, default=DEFAULT_BWLIMIT_KBPS,
                        help="KB/s ceiling on the transfer; 0 disables "
                             "(default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the rsync command and stop")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    config = read_config(args.config)
    remote = args.remote or config.get("REMOTE", DEFAULT_REMOTE)
    source = args.source or config.get("SOURCE", DEFAULT_SOURCE)
    dest_root = args.dest or config.get("DEST", DEFAULT_DEST)
    subdir = args.subdir or config.get("SUBDIR", DEFAULT_SUBDIR)
    ssh_key = args.ssh_key or config.get("SSH_KEY", "~/.ssh/id_ed25519")

    if args.dry_run:
        run_backup(remote, source, dest_root, subdir, ssh_key,
                   checksum=args.checksum, timeout=args.timeout,
                   dry_run=True, bwlimit=args.bwlimit)
        return 0

    lock = acquire_lock(dest_root)
    if lock is None:
        log.info("another run holds the lock - skipping this one")
        return 0

    previous = read_status(dest_root)
    outcome, status = run_backup(remote, source, dest_root, subdir, ssh_key,
                                 checksum=args.checksum, timeout=args.timeout,
                                 bwlimit=args.bwlimit)

    # Carry the last *successful* pull forward across failures, so the file
    # answers "how stale is this archive" and not merely "did the most recent
    # attempt work". Those differ exactly when it matters.
    if outcome == "ok":
        status["last_success_epoch"] = status["finished_epoch"]
    else:
        status["last_success_epoch"] = previous.get("last_success_epoch")
    stale = status["last_success_epoch"]
    if stale:
        status["stale_hours"] = round((time.time() - stale) / 3600.0, 2)

    write_status(dest_root, status)
    lock.close()

    # "unreachable" exits 0 on purpose: it is a statement about the network,
    # not about this collector, and Restart=on-failure would otherwise retry
    # every five minutes at a station that is simply offline.
    return 1 if outcome == "failed" else 0


if __name__ == "__main__":
    sys.exit(main() or 0)

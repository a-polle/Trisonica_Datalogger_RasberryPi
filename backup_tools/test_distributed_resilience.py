#!/usr/bin/env python3
"""Resilience and fault-injection tests for the off-site collector.

Run on the COLLECTOR (the homeserver), against a live station:

    python3 test_distributed_resilience.py

These are integration tests. They talk to the real station over the real
network and are therefore slower and less hermetic than the unit suites beside
the logger; what they buy is coverage of the failure modes that only exist
between two machines.

  1. Lock contention - two runs cannot write the same partial file
  2. Unreachable station - reported, not treated as a broken backup
  3. Interrupted transfer - resumes rather than restarting, and stays intact
  4. A file being appended to at 10 Hz can be copied safely
  5. SSH keepalive and a stable address
  6. The station's status API answers, and answers quickly
  7. The archive matches the station
  8. systemd timer and service are correctly configured
  9. The backup key cannot write to the station or get a shell

NOTE ON METHOD. Three of these tests used to create scratch files on the
station over SSH. That is no longer possible, and its impossibility is the
point: the collector's key is confined by a forced command to read-only rsync
below /home/pi, so it can neither write a fixture nor run sha256sum remotely.
They were rewritten to work through the one channel the key does allow, using
real recordings instead of synthetic ones. Test 9 asserts the restriction
directly.
"""

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
SYNC_SCRIPT = os.path.join(TOOLS_DIR, "sync_trisonica.sh")
COLLECTOR = os.path.join(TOOLS_DIR, "trisonica_backup.py")

# Everything site-specific comes from the collector's own config, the same file
# trisonica_backup.py reads. Nothing here names a particular station, so this
# file is the same on every collector and publishing it discloses nothing.
#
# The station's dashboard config lives on the STATION, so PUBLIC_PREFIX cannot
# be read from here; the full status URL is collector-side knowledge and
# belongs in this config too.
BACKUP_CONFIG = os.environ.get("TRISONICA_BACKUP_CONFIG",
                               "/etc/trisonica-backup.conf")


def _config():
    values = {}
    try:
        with open(BACKUP_CONFIG) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip()
    except (IOError, OSError):
        pass
    return values


_CONF = _config()

# e.g. REMOTE=pi@100.64.0.1 -> host part only
REMOTE = _CONF.get("REMOTE", "")
REMOTE_IP = REMOTE.split("@")[-1] if REMOTE else ""
ARCHIVE_ROOT = _CONF.get("DEST", "")
LOCAL_DIR = os.path.join(ARCHIVE_ROOT, "data") if ARCHIVE_ROOT else ""

# Must match LOCK_NAME in trisonica_backup.py. Not /tmp: the systemd unit sets
# PrivateTmp=true, so a lock there would be a *different* file for the
# scheduled run and for a manual one -- the exact collision it prevents.
LOCK_FILE = os.path.join(ARCHIVE_ROOT, ".backup.lock") if ARCHIVE_ROOT else ""

# TEST-NET-1 (RFC 5737). Guaranteed unroutable, so "unreachable" is a property
# of the address rather than of whatever the network happens to be doing.
UNREACHABLE_IP = "192.0.2.1"

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]


def station_base_url():
    """Base URL of the station's dashboard, including any public prefix.

    Read from STATUS_URL in the collector's config. Hard-coding the path is
    what broke the station's own alerting: a public prefix moved /api/status
    and the checker kept asking the old path, got a 404, and reported a
    perfectly healthy station as dead. Absent the setting, fall back to the
    unprefixed address, which is correct for a station that has no prefix.
    """
    try:
        with open(BACKUP_CONFIG) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("STATUS_URL") and "=" in line:
                    return line.split("=", 1)[1].strip().rstrip("/")
    except (IOError, OSError):
        pass
    return "http://%s:8080" % REMOTE_IP


def station_url(path):
    return station_base_url() + path


def newest_archived():
    """Name of the newest CSV in the archive -- i.e. the one still growing."""
    names = sorted(n for n in os.listdir(LOCAL_DIR) if n.endswith(".csv"))
    return names[-1] if names else None


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class TestDistributedResilience(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not REMOTE_IP or not ARCHIVE_ROOT:
            raise unittest.SkipTest(
                "no station configured: set REMOTE and DEST in %s"
                % BACKUP_CONFIG)

    def test_01_lock_contention_and_concurrency(self):
        """Two runs must not write the same partial file."""
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            res = subprocess.run([SYNC_SCRIPT], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 timeout=60)
            self.assertEqual(res.returncode, 0,
                             "a skipped run must not look like a failure: %s"
                             % res.stdout)
            self.assertIn("lock", res.stdout.lower(), res.stdout)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_02_unreachable_station_is_reported_not_failed(self):
        """An offline station is a fact about the network, not a broken backup.

        Exercises the real collector, not a mock of it: the previous version of
        this test asserted against a shell script written inside the test,
        which could not have caught the collector regressing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            t0 = time.time()
            res = subprocess.run(
                ["python3", COLLECTOR, "--remote", "pi@" + UNREACHABLE_IP,
                 "--dest", tmp, "--config", "/nonexistent"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                timeout=180)
            dt = time.time() - t0

            self.assertEqual(res.returncode, 0,
                             "unreachable must exit 0 or systemd will restart-"
                             "loop against a station that is simply offline")
            self.assertIn("not reachable", res.stdout.lower(), res.stdout)
            self.assertLess(dt, 150, "took too long to give up")

            with open(os.path.join(tmp, "backup-status.json")) as fh:
                status = json.load(fh)
            self.assertEqual(status["outcome"], "unreachable")
            self.assertFalse(status["ok"])

    def test_03_interrupted_transfer_resumes_and_stays_intact(self):
        """Kill a transfer mid-flight; the retry must resume and verify.

        Uses a real recording rather than a scratch file, because the
        collector's key cannot create one on the station.
        """
        name = sorted(n for n in os.listdir(LOCAL_DIR)
                      if n.endswith(".csv"))[0]
        known_good = sha256_of(os.path.join(LOCAL_DIR, name))

        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, name)
            remote = "pi@%s:trisonica-data/%s" % (REMOTE_IP, name)
            base = ["rsync", "-t", "--partial-dir=.rsync-partial",
                    "-e", "ssh " + " ".join(SSH_OPTS)]

            proc = subprocess.Popen(base + ["--bwlimit=60", remote, dest],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            time.sleep(3)
            proc.terminate()
            proc.wait(timeout=30)

            done = subprocess.run(base + [remote, dest],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True,
                                  timeout=300)
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertEqual(sha256_of(dest), known_good,
                             "resumed file does not match the station's copy")

    def test_04_a_file_being_written_can_be_copied_safely(self):
        """The newest recording is appended to at 10 Hz while it is read.

        No simulation needed and none possible: the station is recording now,
        so the real active file is the fixture.
        """
        name = newest_archived()
        self.assertIsNotNone(name, "archive is empty")

        with tempfile.TemporaryDirectory() as tmp:
            remote = "pi@%s:trisonica-data/%s" % (REMOTE_IP, name)
            cmd = ["rsync", "-t", "--partial-dir=.rsync-partial",
                   "-e", "ssh " + " ".join(SSH_OPTS), remote,
                   os.path.join(tmp, name)]

            first = subprocess.run(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True,
                                   timeout=300)
            self.assertEqual(first.returncode, 0, first.stdout)
            size1 = os.path.getsize(os.path.join(tmp, name))

            time.sleep(12)

            second = subprocess.run(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    timeout=300)
            self.assertEqual(second.returncode, 0, second.stdout)
            size2 = os.path.getsize(os.path.join(tmp, name))

            self.assertGreater(size2, size1,
                               "the active file did not grow in 12 s - is the "
                               "station still recording?")

            # Every completed line must still be well-formed CSV. A torn read
            # would show up as a short final row, so the last line is allowed
            # to be partial and everything before it is not.
            with open(os.path.join(tmp, name)) as fh:
                lines = fh.read().splitlines()
            self.assertGreater(len(lines), 2)
            width = len(lines[0].split(","))
            for row in lines[1:-1]:
                self.assertEqual(len(row.split(",")), width,
                                 "malformed row in a mid-write copy: %r" % row)

    def test_05_ssh_keepalive_and_a_stable_address(self):
        """A dropped TCP session must be noticed, and the address must not move."""
        path = os.path.expanduser("~/.ssh/config")
        self.assertTrue(os.path.exists(path))
        with open(path) as fh:
            content = fh.read()
        # The station reached by IP, not MagicDNS: a Tailscale address is
        # stable for the life of the node and does not depend on name
        # resolution being up.
        self.assertIn(REMOTE_IP, content,
                      "%s is not in ~/.ssh/config" % REMOTE_IP)
        for opt in ("ServerAliveInterval", "ServerAliveCountMax",
                    "ConnectTimeout"):
            self.assertIn(opt, content)

    def test_06_status_api_answers_and_answers_quickly(self):
        """The dashboard must respond, at whatever path it is published on."""
        url = station_url("/api/status")
        t0 = time.time()
        res = subprocess.run(["curl", "-s", "-m", "10", url],
                             stdout=subprocess.PIPE, text=True, timeout=20)
        dt = time.time() - t0
        self.assertLess(dt, 11)
        self.assertNotIn("Error code: 404", res.stdout,
                         "got a 404 from %s - has PUBLIC_PREFIX changed?" % url)
        payload = json.loads(res.stdout)
        self.assertIn("health", payload)
        self.assertIn("level", payload["health"])

    def test_07_archive_matches_the_station(self):
        """Every completed recording on the card is here, and is identical.

        Done with `rsync --dry-run`, which is the only integrity check the
        restricted key permits -- sha256sum cannot be run on the station.

        Size+mtime by default rather than --checksum. Re-reading the whole card
        is exactly the kind of I/O that has been measured taking the logger
        from 10.00 Hz down to 6.69 Hz, and this suite must not damage the
        recording it is verifying. Set TRISONICA_DEEP_AUDIT=1 for the real
        byte-level comparison, when a gap in the record is acceptable.
        """
        cmd = ["rsync", "-rlt", "--dry-run", "--out-format=%n",
               "-e", "ssh " + " ".join(SSH_OPTS),
               "pi@%s:trisonica-data/" % REMOTE_IP, LOCAL_DIR + "/"]
        if os.environ.get("TRISONICA_DEEP_AUDIT") == "1":
            cmd.insert(3, "--checksum")

        res = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, timeout=1800)
        self.assertEqual(res.returncode, 0, res.stdout)

        newest = newest_archived()
        stale = [line.strip() for line in res.stdout.splitlines()
                 if line.strip().endswith(".csv") and line.strip() != newest]
        self.assertEqual(stale, [],
                         "these completed files differ from the station: %s"
                         % stale)

    def test_08_systemd_timer_and_service_lifecycle(self):
        """The schedule and the sandbox both have to be what we think."""
        active = subprocess.run(
            ["systemctl", "is-active", "trisonica-backup.timer"],
            stdout=subprocess.PIPE, text=True).stdout.strip()
        self.assertEqual(active, "active")

        timer = subprocess.run(["systemctl", "cat", "trisonica-backup.timer"],
                               stdout=subprocess.PIPE, text=True).stdout
        # Persistent: a collector that was asleep should catch up on return.
        self.assertIn("Persistent=true", timer)
        self.assertIn("OnUnitActiveSec=1h", timer)

        service = subprocess.run(
            ["systemctl", "cat", "trisonica-backup.service"],
            stdout=subprocess.PIPE, text=True).stdout
        self.assertIn("ProtectSystem=strict", service)
        # Load-bearing, and once missing: ProtectSystem=strict covers the OS
        # hierarchy but leaves home writable, and the archive lives in /home
        # beside the ~/.ssh key this job authenticates with.
        self.assertIn("ProtectHome=read-only", service)
        self.assertIn("ReadWritePaths=", service)

    def test_09_the_backup_key_cannot_write_or_get_a_shell(self):
        """The confinement the other tests now depend on."""
        shell = subprocess.run(
            ["ssh"] + SSH_OPTS + ["pi@" + REMOTE_IP, "id"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=60)
        self.assertNotEqual(shell.returncode, 0,
                            "the backup key got a shell on the station")
        self.assertIn("not rsync", shell.stdout.lower(), shell.stdout)

        with tempfile.TemporaryDirectory() as tmp:
            probe = os.path.join(tmp, "probe.txt")
            with open(probe, "w") as fh:
                fh.write("probe\n")
            write = subprocess.run(
                ["rsync", "-t", "-e", "ssh " + " ".join(SSH_OPTS), probe,
                 "pi@%s:trisonica-data/" % REMOTE_IP],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                timeout=60)
            self.assertNotEqual(write.returncode, 0,
                                "the backup key wrote to the station")
            self.assertIn("read-only", write.stdout.lower(), write.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

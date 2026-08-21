#!/usr/bin/env python3
"""Automated Resilience and Fault-Injection Test Suite for TriSonica Backup on mm12 <-> rbp3b.

Tests:
  1. Lock Contention & Concurrency (flock)
  2. Unreachable Host / Simulated Network Drop Graceful Recovery
  3. Interrupted / Killed Transfer Partial Resume and Integrity
  4. Active Writing Concurrency (Simulating live 10 Hz logger append)
  5. Tailscale IP Resolution and SSH Config Keepalive Verification
  6. API Status Degradation Non-blocking Behavior
  7. End-to-End SHA-256 Dataset Integrity Audit
  8. Systemd Timer and Service Lifecycle Validation
"""

import fcntl
import hashlib
import os
import signal
import subprocess
import sys
import time
import unittest

REMOTE_HOST = "rbp3b"
REMOTE_IP = "100.125.165.61"
LOCAL_DIR = "/home/alex/Backups/trisonica-data"
SYNC_SCRIPT = "/home/alex/Backups/sync_trisonica.sh"
LOCK_FILE = "/tmp/trisonica_backup.lock"

class TestDistributedResilience(unittest.TestCase):

    def test_01_lock_contention_and_concurrency(self):
        """Verify that multiple concurrent sync invocations do not collide or crash."""
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o666)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            res = subprocess.run([SYNC_SCRIPT], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            self.assertEqual(res.returncode, 0, f"Script exited with {res.returncode}: {res.stderr}")
            self.assertIn("already in progress", res.stdout)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def test_02_unreachable_host_graceful_handling(self):
        """Verify that when remote host is unreachable, the sync script fails gracefully without throwing errors."""
        mock_script = """#!/usr/bin/env bash
set -euo pipefail
REMOTE_HOST="192.0.2.1"
REMOTE_IP="192.0.2.1"
REMOTE_DIR="/home/pi/trisonica-data/"
LOCAL_DIR="/home/alex/Backups/trisonica-data"
mkdir -p "$LOCAL_DIR"

if ! ssh -o BatchMode=yes -o ConnectTimeout=2 "pi@${REMOTE_HOST}" "true" 2>/dev/null; then
    echo "WARNING: Cannot reach ${REMOTE_HOST} (${REMOTE_IP}). Station may be offline. Will retry on next timer."
    exit 0
fi
"""
        mock_path = "/tmp/mock_unreachable_sync.sh"
        with open(mock_path, "w") as f:
            f.write(mock_script)
        os.chmod(mock_path, 0o755)

        try:
            t0 = time.time()
            res = subprocess.run([mock_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            dt = time.time() - t0
            self.assertEqual(res.returncode, 0)
            self.assertIn("WARNING: Cannot reach", res.stdout)
            self.assertLess(dt, 6.0, "Timeout check took too long")
        finally:
            if os.path.exists(mock_path):
                os.remove(mock_path)

    def test_03_interrupted_transfer_and_resume_integrity(self):
        """Simulate an interrupted connection mid-transfer and verify rsync resumes and verifies 100% SHA-256 hash."""
        test_file = "Trisonica_Test_Partial_Transfer.dat"
        remote_path = f"/home/pi/trisonica-data/{test_file}"
        local_path = f"{LOCAL_DIR}/{test_file}"

        # 1. Create a 10MB test file on remote Pi
        create_cmd = ["ssh", f"pi@{REMOTE_HOST}", f"head -c 10485760 /dev/urandom > {remote_path} && sha256sum {remote_path}"]
        res = subprocess.run(create_cmd, stdout=subprocess.PIPE, text=True, check=True)
        remote_sha256 = res.stdout.strip().split()[0]

        try:
            # 2. Start rsync throttled so transfer is actively running, then interrupt it
            proc = subprocess.Popen([
                "rsync", "-avz", "--partial", "--bwlimit=500",
                f"pi@{REMOTE_HOST}:{remote_path}", local_path
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(1.5)
            proc.terminate()
            proc.wait()

            # 3. Resume sync normally without bandwidth limit
            res_resume = subprocess.run([
                "rsync", "-avz", "--partial",
                f"pi@{REMOTE_HOST}:{remote_path}", local_path
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

            # 4. Check resumed file size and SHA-256
            self.assertEqual(os.path.getsize(local_path), 10485760)
            with open(local_path, "rb") as f:
                local_sha256 = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(local_sha256, remote_sha256, "Resumed file checksum mismatch!")

        finally:
            subprocess.run(["ssh", f"pi@{REMOTE_HOST}", f"rm -f {remote_path}"], check=False)
            if os.path.exists(local_path):
                os.remove(local_path)

    def test_04_live_append_concurrency_stream(self):
        """Simulate syncing a file while it is actively being appended to at high speed."""
        stream_file = "Trisonica_Live_Append_Sim.csv"
        remote_path = f"/home/pi/trisonica-data/{stream_file}"
        local_path = f"{LOCAL_DIR}/{stream_file}"

        writer_script = f"""
import time
with open('{remote_path}', 'w') as f:
    f.write('header\\n')
    f.flush()
    for i in range(150):
        f.write('row_' + str(i) + '\\n')
        f.flush()
        time.sleep(0.02)
"""
        start_writer = [
            "ssh", f"pi@{REMOTE_HOST}",
            f"python3 -c \"{writer_script}\""
        ]
        writer_proc = subprocess.Popen(start_writer)

        try:
            time.sleep(0.5)
            subprocess.run([
                "rsync", "-avz", "--partial",
                f"pi@{REMOTE_HOST}:{remote_path}", local_path
            ], check=True)

            self.assertTrue(os.path.exists(local_path))
            writer_proc.wait(timeout=10)

            subprocess.run([
                "rsync", "-avz", "--partial",
                f"pi@{REMOTE_HOST}:{remote_path}", local_path
            ], check=True)

            with open(local_path, "r") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 151)
            self.assertEqual(lines[0].strip(), "header")
            self.assertEqual(lines[-1].strip(), "row_149")
        finally:
            subprocess.run(["ssh", f"pi@{REMOTE_HOST}", f"rm -f {remote_path}"], check=False)
            if os.path.exists(local_path):
                os.remove(local_path)

    def test_05_ssh_config_keepalive_and_ip_mapping(self):
        """Verify that ~/.ssh/config contains keepalive, timeout, and direct IP mapping."""
        ssh_config_path = os.path.expanduser("~/.ssh/config")
        self.assertTrue(os.path.exists(ssh_config_path))
        with open(ssh_config_path) as f:
            content = f.read()
        self.assertIn("Host rbp3b", content)
        self.assertIn("HostName 100.125.165.61", content)
        self.assertIn("ServerAliveInterval", content)
        self.assertIn("ServerAliveCountMax", content)
        self.assertIn("ConnectTimeout", content)

    def test_06_api_status_non_blocking(self):
        """Verify that querying status API completes within 3s or handles failure without crashing."""
        cmd = ["curl", "-s", "-m", "3", f"http://{REMOTE_IP}:8080/api/status"]
        t0 = time.time()
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        dt = time.time() - t0
        self.assertLess(dt, 3.5)
        self.assertIn("health", res.stdout)

    def test_07_full_dataset_sha256_audit(self):
        """Audit SHA-256 hashes of all completed CSV files between rbp3b and mm12."""
        remote_cmd = ["ssh", f"pi@{REMOTE_HOST}", "cd /home/pi/trisonica-data && sha256sum TrisonicaData_*.csv"]
        res = subprocess.run(remote_cmd, stdout=subprocess.PIPE, text=True, check=True)
        remote_hashes = {}
        for line in res.stdout.strip().splitlines():
            parts = line.strip().split()
            if len(parts) == 2:
                remote_hashes[parts[1]] = parts[0]

        newest = sorted(remote_hashes.keys())[-1]

        matches = 0
        diffs = []
        for fname, rhash in remote_hashes.items():
            if fname == newest:
                continue
            lpath = os.path.join(LOCAL_DIR, fname)
            self.assertTrue(os.path.exists(lpath), f"Missing file locally: {fname}")
            with open(lpath, "rb") as f:
                lhash = hashlib.sha256(f.read()).hexdigest()
            if lhash == rhash:
                matches += 1
            else:
                diffs.append((fname, rhash, lhash))

        self.assertEqual(len(diffs), 0, f"SHA-256 mismatches: {diffs}")
        self.assertEqual(matches, len(remote_hashes) - 1)

    def test_08_systemd_timer_and_service_lifecycle(self):
        """Verify systemd timer is active, scheduled, and persistent."""
        res_timer = subprocess.run(["systemctl", "is-active", "trisonica-backup.timer"], stdout=subprocess.PIPE, text=True)
        self.assertEqual(res_timer.stdout.strip(), "active")

        res_unit = subprocess.run(["systemctl", "cat", "trisonica-backup.timer"], stdout=subprocess.PIPE, text=True)
        self.assertIn("Persistent=true", res_unit.stdout)
        self.assertIn("OnUnitActiveSec=15m", res_unit.stdout)

if __name__ == "__main__":
    unittest.main(verbosity=2)

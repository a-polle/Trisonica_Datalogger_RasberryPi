#!/usr/bin/env bash
# Deploy the read-only archive page to the collector.
#
# The one-time /etc/trisonica-archive.conf is intentionally not created here:
# it contains the private public-path capability and must never enter source,
# staging output, or shell tracing.

set -euo pipefail

HOST="${1:-mm12}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGE="/tmp/trisonica-archive-deploy-$$"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST")

cleanup() {
    "${SSH[@]}" "find '$STAGE' -mindepth 1 -delete 2>/dev/null; rmdir '$STAGE' 2>/dev/null" \
        >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "$HERE"
python3 test_archive_server.py >/dev/null 2>&1
echo "+ archive tests pass locally"

"${SSH[@]}" "mkdir -p '$STAGE'"
scp -q trisonica_archive_server.py test_archive_server.py \
    trisonica-archive.service "$HOST:$STAGE/"
"${SSH[@]}" "cd '$STAGE' && python3 test_archive_server.py" >/dev/null 2>&1
echo "+ archive tests pass on $HOST"

"${SSH[@]}" "test -r /etc/trisonica-archive.conf" || {
    echo "x /etc/trisonica-archive.conf is missing or unreadable on $HOST" >&2
    exit 1
}

"${SSH[@]}" "
    sudo install -d -m 0755 /usr/local/lib/trisonica
    sudo install -m 0755 '$STAGE/trisonica_archive_server.py' \
        /usr/local/lib/trisonica/trisonica_archive_server.py
    sudo install -m 0644 '$STAGE/trisonica-archive.service' \
        /etc/systemd/system/trisonica-archive.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now trisonica-archive.service
    sudo systemctl restart trisonica-archive.service
"

"${SSH[@]}" 'python3 - <<'"'"'PY'"'"'
import re, subprocess, sys, urllib.request
values = {}
for line in open("/etc/trisonica-archive.conf"):
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.strip().split("=", 1)
        values[key] = value
prefix = values.get("PUBLIC_PREFIX", "").strip("/")
if not re.match(r"^[A-Za-z0-9._~-]+$", prefix):
    raise SystemExit("invalid archive prefix")
if subprocess.run(["systemctl", "is-active", "--quiet",
                   "trisonica-archive.service"]).returncode:
    raise SystemExit("archive service is not active")
with urllib.request.urlopen("http://127.0.0.1:8081/%s/" % prefix,
                            timeout=5) as response:
    if response.status != 200 or b"TriSonica Server Archive" not in response.read():
        raise SystemExit("archive page did not verify")
print("+ archive service active and local page verified")
PY'

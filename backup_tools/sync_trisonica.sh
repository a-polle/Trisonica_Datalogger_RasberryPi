#!/usr/bin/env bash
# Unattended backup sync for TriSonica field logger from rbp3b
set -euo pipefail

LOCK_FILE="/tmp/trisonica_backup.lock"
exec 200>"$LOCK_FILE"
flock -n 200 || { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Backup sync already in progress, skipping."; exit 0; }

REMOTE_HOST="rbp3b"
REMOTE_IP="100.125.165.61"
REMOTE_DIR="/home/pi/trisonica-data/"
LOCAL_DIR="/home/alex/Backups/trisonica-data"

mkdir -p "$LOCAL_DIR"

# Quick connectivity test (timeout 5s)
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "pi@${REMOTE_HOST}" "true" 2>/dev/null; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WARNING: Cannot reach ${REMOTE_HOST} (${REMOTE_IP}). Station may be offline or unreachable. Will retry on next timer."
    exit 0
fi

# Query station health snapshot
HEALTH_INFO=$(curl -s -m 3 "http://${REMOTE_IP}:8080/api/status" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    h = d.get(\"health\", {})
    l = d.get(\"logger\", {})
    print(f\"health={h.get(\x27level\x27,\x27?\x27)}, rate={l.get(\x27sample_rate_hz\x27,\x27?\x27)}Hz, bad={l.get(\x27bad_pct\x27,\x27?\x27)}%, free={l.get(\x27logger_free_mb\x27,\x27?\x27)}MB\")
except Exception:
    pass
" 2>/dev/null || echo "")

if [ -n "$HEALTH_INFO" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Station health snapshot: ${HEALTH_INFO}"
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Syncing from ${REMOTE_HOST}..."

rsync -avz \
  --partial \
  --timeout=30 \
  -e "ssh -o BatchMode=yes -o ConnectTimeout=10" \
  "pi@${REMOTE_HOST}:${REMOTE_DIR}" \
  "${LOCAL_DIR}/"

COUNT=$(find "$LOCAL_DIR" -maxdepth 1 -name "*.csv" | wc -l)
TOTAL_SIZE=$(du -sh "$LOCAL_DIR" | awk "{print \$1}")
LATEST_FILE=$(ls -t "$LOCAL_DIR"/*.csv 2>/dev/null | head -n 1 || echo "none")

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Backup sync completed: ${COUNT} files, ${TOTAL_SIZE} total. Latest: $(basename "$LATEST_FILE")"

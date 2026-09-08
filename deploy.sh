#!/usr/bin/env bash
#
# Deploy this directory to the field unit and verify it took.
#
#   ./deploy.sh              # deploy to the default host
#   ./deploy.sh pi@rbp3b     # or name one
#   ./deploy.sh --all        # restart both services even if nothing changed
#
# Safe to run repeatedly. It refuses to restart anything unless the tests pass
# on the Pi itself, restarts only the service whose files actually changed,
# and verifies the running services afterwards rather than assuming success.
#
# Written because the alternative - remembering which files to scp, in which
# order, and which services to restart - is exactly how a device ends up
# running something different from what was reviewed.

set -uo pipefail

FORCE_ALL=0
HOST=""
for arg in "$@"; do
    case "$arg" in
        --all)  FORCE_ALL=1 ;;
        -h|--help)
            sed -n '3,8p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        -*)     echo "unknown option: $arg" >&2; exit 2 ;;
        *)      HOST="$arg" ;;
    esac
done
HOST="${HOST:-pi@rbp3b}"

REMOTE_DIR="/home/pi/trisonica"
# Files land here first and are promoted into REMOTE_DIR only once the tests
# have passed against them ON the Pi. Without this, a deploy that copied files
# and then failed its tests left new code sitting on disk that nothing was
# running - and the NEXT deploy, seeing matching checksums, reported "no
# service code changed" and restarted nothing. The unit then ran stale code
# indefinitely while the script said everything was fine. Observed 2026-08-13.
STAGE_DIR="/home/pi/trisonica.staging"
# Records the code that was last restarted AND verified healthy, per unit, so
# that "the file is on disk" can never again be mistaken for "the service is
# running it".
STATE_FILE="/home/pi/trisonica/.deployed-state"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# One multiplexed connection for the whole run. This script makes a dozen
# round trips and a fresh SSH handshake for each was most of its runtime.
WORK="$(mktemp -d "${TMPDIR:-/tmp}/trisonica-deploy-XXXXXX")" || exit 1
CTL="$WORK/ssh"
SSH="ssh -o ConnectTimeout=15 -o BatchMode=yes -o ControlMaster=auto
     -o ControlPath=$CTL -o ControlPersist=120"
SCP="scp -o ControlPath=$CTL"

cleanup () {
    ssh -o ControlPath="$CTL" -O exit "$HOST" >/dev/null 2>&1
    rm -rf "$WORK"
}
trap cleanup EXIT

say  () { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok   () { printf '  \033[32m+\033[0m %s\n' "$*"; }
bad  () { printf '  \033[31mx\033[0m %s\n' "$*"; }
info () { printf '    %s\n' "$*"; }

fail () { bad "$*"; exit 1; }

# Which service each file belongs to. Anything not listed here (README,
# session notes) is documentation and restarts nothing.
unit_for () {
    case "$1" in
        trisonica_field_logger.py|trisonica-logger.service)
            echo trisonica-logger ;;
        trisonica_usb_export.py|trisonica-usb-export.service)
            echo trisonica-usb-export ;;
        trisonica_status_server.py|trisonica-status.service)
            echo trisonica-status ;;
        trisonica_hdmi_status.py|trisonica-hdmi.service)
            echo trisonica-hdmi ;;
        trisonica_alert.py|trisonica-alert.service|trisonica-alert.timer)
            echo trisonica-alert ;;
        *)  echo "" ;;
    esac
}

# Hash of everything a unit actually runs, so the marker above can answer
# "is the running service this code?" rather than merely "is this file here?".
unit_hash () {
    local u="$1" acc="" f
    for f in "${FILES[@]}"; do
        [ "$(unit_for "$f")" = "$u" ] || continue
        acc="$acc$(sha256sum "$f" | cut -d' ' -f1)"
    done
    printf '%s' "$acc" | sha256sum | cut -d' ' -f1
}

FILES=(
  trisonica_field_logger.py
  trisonica_usb_export.py
  trisonica_status_server.py
  trisonica_hdmi_status.py
  trisonica_alert.py
  test_field_logger.py
  test_status_server.py
  test_hdmi_status.py
  trisonica-logger.service
  trisonica-usb-export.service
  trisonica-status.service
  trisonica-hdmi.service
  trisonica-alert.service
  trisonica-alert.timer
  README.md
  GPS_SETUP.md
)

# --------------------------------------------------------------------------
say "1. Local checks"

cd "$HERE" || fail "cannot enter $HERE"

for f in "${FILES[@]}"; do
    [ -f "$f" ] || fail "missing file: $f"
done

: >"$WORK/local_tests.log"
for suite in test_field_logger.py test_status_server.py test_hdmi_status.py; do
    python3 "$suite" >>"$WORK/local_tests.log" 2>&1 \
      || { tail -20 "$WORK/local_tests.log"
           fail "local tests failed ($suite) - not deploying"; }
done
ok "local tests pass ($(grep -c '^OK' "$WORK/local_tests.log") suites, \
$(grep -o 'Ran [0-9]* tests' "$WORK/local_tests.log" | awk '{n+=$2} END {print n" tests"}'))"

# The export service must never be able to power the unit off. This killed a
# deployment once; keep it as a hard gate rather than a memory.
#
# Checked against the PARSED code of every module that runs on the device,
# not the raw text. A plain grep is tripped by the word appearing in a comment
# explaining the rule, and a unit that cannot be deployed because its source
# documents why is its own outage. The unit file carries the structural
# backstop; this catches it earlier, and across every module rather than one
# hard-coded filename. The test file is excluded: naming the banned calls is
# precisely its job.
MODULES=()
for f in "$HERE"/*.py; do
    case "$(basename "$f")" in test_*.py) continue ;; esac
    MODULES+=("$f")
done
[ ${#MODULES[@]} -gt 0 ] || fail "no python modules found in $HERE"

python3 - "${MODULES[@]}" <<'PY' || exit 1
import ast, sys

BANNED = ("poweroff", "shutdown_marker")
problems = []

# ast.Str was deprecated in 3.8 and REMOVED in 3.12. The previous
# isinstance(node, ast.Str) raises AttributeError on a newer interpreter, which
# exits this gate non-zero and blocks EVERY deploy - including an urgent fix to
# a unit already in the field. Resolve the node types at runtime so this works
# on the Pi's 3.7 (string literals parse to ast.Str, value on .s) and on a
# modern laptop (ast.Constant, value on .value) alike.
STR_NODES = tuple(t for t in (getattr(ast, "Constant", None),
                              getattr(ast, "Str", None)) if t is not None)


def string_literal(node):
    """Return the value of a string-literal node, or None if it is not one."""
    if not isinstance(node, STR_NODES):
        return None
    value = getattr(node, "value", None)
    if value is None:
        value = getattr(node, "s", None)
    return value if isinstance(value, str) else None

for path in sys.argv[1:]:
    try:
        tree = ast.parse(open(path).read(), path)
    except SyntaxError as exc:
        problems.append("%s: does not parse (%s)" % (path, exc))
        continue

    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None:
                docs.add(id(node.body[0].value))

    for node in ast.walk(tree):
        text = string_literal(node)
        if text is not None:
            if id(node) in docs:
                continue
            hay = text
        elif isinstance(node, ast.Name):
            hay = node.id
        elif isinstance(node, ast.Attribute):
            hay = node.attr
        else:
            continue
        for word in BANNED:
            if word in str(hay).lower():
                problems.append("%s:%s: %r reachable from code"
                                % (path, getattr(node, "lineno", "?"), word))

if problems:
    sys.stderr.write("  x refusing to deploy - a module could power the unit off:\n")
    for p in problems:
        sys.stderr.write("      " + p + "\n")
    sys.exit(1)
PY
ok "no module can power the unit off"

# --------------------------------------------------------------------------
say "2. Reaching the unit"

$SSH "$HOST" true 2>/dev/null \
  || fail "cannot reach $HOST - is the Pi powered on and connected to a network?"
ok "$HOST reachable"
info "$($SSH "$HOST" 'echo "$(hostname), up $(uptime -p)"')"

# --------------------------------------------------------------------------
say "3. Staging files"

# What is already live, so only the changed services get restarted. One round
# trip for the whole list.
REMOTE_SUMS="$($SSH "$HOST" "cd $REMOTE_DIR 2>/dev/null && sha256sum ${FILES[*]} 2>/dev/null")"

CHANGED=()
for f in "${FILES[@]}"; do
    L=$(sha256sum "$f" | cut -d' ' -f1)
    R=$(printf '%s\n' "$REMOTE_SUMS" | awk -v f="$f" '$2 == f {print $1}')
    [ "$L" = "$R" ] || CHANGED+=("$f")
done

# EVERY file is staged, not just the changed ones: the tests import their
# neighbours, so the staging area has to be a complete tree. At ~150 kB the
# copy is not worth optimising, and a full copy cannot leave a stale file
# behind for the tests to import.
$SSH "$HOST" "mkdir -p $STAGE_DIR $REMOTE_DIR && rm -rf $STAGE_DIR/__pycache__" \
  || fail "cannot create $STAGE_DIR"
$SCP -q "${FILES[@]}" "$HOST:$STAGE_DIR/" || fail "scp to the staging area failed"

# Prove they arrived intact rather than trusting scp's exit code. One round
# trip, and an absent remote digest can no longer match an absent local one.
VERIFY="$($SSH "$HOST" "cd $STAGE_DIR && sha256sum ${FILES[*]} 2>&1")" \
  || fail "cannot read back the staged files"
for f in "${FILES[@]}"; do
    L=$(sha256sum "$f" | cut -d' ' -f1)
    R=$(printf '%s\n' "$VERIFY" | awk -v f="$f" '$2 == f {print $1}')
    [ -n "$R" ]      || fail "missing on the Pi after copy: $f"
    [ "$L" = "$R" ]  || fail "checksum mismatch after copy: $f"
done
if [ ${#CHANGED[@]} -eq 0 ]; then
    ok "staged ${#FILES[@]} files, checksums match; none differ from live"
else
    ok "staged ${#FILES[@]} files, checksums match; ${#CHANGED[@]} differ: ${CHANGED[*]}"
fi

# --------------------------------------------------------------------------
say "4. Tests on the Pi"

# This is not ceremony. The Pi runs Python 3.7 on ARM; a test has passed
# locally and failed there before, over exactly that gap. Run against the
# STAGED tree, so a failure here leaves the live directory untouched.
$SSH "$HOST" "cd $STAGE_DIR && python3 test_field_logger.py \
                            && python3 test_status_server.py \
                            && python3 test_hdmi_status.py" \
      >"$WORK/pi_tests.log" 2>&1 \
  || { tail -20 "$WORK/pi_tests.log"; fail "tests failed ON THE PI - nothing promoted, nothing restarted"; }
ok "tests pass on the Pi ($(grep -o 'Ran [0-9]* tests' "$WORK/pi_tests.log" \
      | awk '{n+=$2} END {print n" tests"}'))"

# --------------------------------------------------------------------------
say "5. Promoting, installing units and restarting"

# Promote the staged tree. Only now, with the tests green on this hardware,
# does anything the services read actually change.
if [ ${#CHANGED[@]} -gt 0 ]; then
    $SSH "$HOST" "cd $STAGE_DIR && cp -p ${CHANGED[*]} $REMOTE_DIR/" \
      || fail "promoting the staged files failed"
    ok "promoted ${#CHANGED[@]} file(s) into $REMOTE_DIR"
else
    ok "live directory already matches the staged tree"
fi

# Only restart what actually changed. The logger is recording: restarting it
# ends the current session and starts a new file, so an export-service fix
# must not cost the researcher a split dataset.
#
# A changed file is not the only reason to restart. The marker records the
# code that was last verified RUNNING, so a unit whose files are already in
# place but which never got restarted - an aborted deploy, a hand-copied
# file - is caught here rather than running stale code indefinitely.
DEPLOYED="$($SSH "$HOST" "cat $STATE_FILE 2>/dev/null")"

RESTART=""
add_restart () {
    case " $RESTART " in *" $1 "*) ;; *) RESTART="$RESTART $1" ;; esac
}
for f in "${CHANGED[@]:-}"; do
    u="$(unit_for "$f")"
    [ -n "$u" ] && add_restart "$u"
done
for u in trisonica-logger trisonica-usb-export trisonica-status trisonica-hdmi trisonica-alert; do
    have="$(printf '%s\n' "$DEPLOYED" | awk -v u="$u" '$1 == u {print $2}')"
    if [ "$(unit_hash "$u")" != "$have" ]; then
        add_restart "$u"
        if [ -n "$have" ]; then
            info "$u is not running the code on disk - restarting"
        else
            info "$u has no recorded deployment - restarting to establish one"
        fi
    fi
done
if [ "$FORCE_ALL" = "1" ]; then
    RESTART="trisonica-logger trisonica-usb-export trisonica-status trisonica-hdmi"
fi
RESTART="${RESTART# }"

# The heartbeat is a oneshot behind a timer, so "restart" would only run one
# check early. What has to be (re)started is the TIMER, and only when its
# code changed - restarting it resets the interval, not the recording.
ALERT_CHANGED=0
case " $RESTART " in *" trisonica-alert "*) ALERT_CHANGED=1 ;; esac
RESTART="$(printf '%s' "$RESTART" | sed 's/trisonica-alert//; s/  */ /g; s/^ //; s/ $//')"

# enable, not just restart: without it the units carry no multi-user.target
# symlink and do not come back after a power cut - which is how this device
# is stopped every single time.
$SSH "$HOST" "
  sudo cp $REMOTE_DIR/trisonica-logger.service $REMOTE_DIR/trisonica-usb-export.service $REMOTE_DIR/trisonica-status.service $REMOTE_DIR/trisonica-hdmi.service \
      $REMOTE_DIR/trisonica-alert.service $REMOTE_DIR/trisonica-alert.timer \
      /etc/systemd/system/ &&
  sudo systemctl daemon-reload &&
  sudo systemctl enable trisonica-logger trisonica-usb-export trisonica-status trisonica-hdmi &&
  sudo systemctl start trisonica-hdmi &&
  sudo systemctl enable --now trisonica-alert.timer
" >/dev/null 2>&1 || fail "installing the unit files failed"
ok "units installed and enabled (they come back after a power cut)"

if [ "$ALERT_CHANGED" = "1" ]; then
    $SSH "$HOST" "sudo systemctl restart trisonica-alert.timer" >/dev/null 2>&1 \
      || fail "could not restart the heartbeat timer"
    ok "heartbeat timer restarted"
fi

if [ -z "$RESTART" ]; then
    ok "no service code changed - nothing restarted, recording undisturbed"
else
    $SSH "$HOST" "sudo systemctl restart $RESTART" >/dev/null 2>&1 \
      || fail "restart failed: $RESTART"
    ok "restarted:$(printf ' %s' $RESTART)"
    sleep 12
fi

# --------------------------------------------------------------------------
say "6. Verifying"

STATE="$($SSH "$HOST" '
  for s in trisonica-logger trisonica-usb-export trisonica-status trisonica-hdmi trisonica-alert.timer gpsd chrony tailscaled; do
    printf "%s=%s/%s " "$s" "$(systemctl is-active $s)" "$(systemctl show $s -p NRestarts --value)"
  done')" || fail "lost contact with $HOST while verifying - it may be fine; re-run"
[ -n "$STATE" ] || fail "no status returned from $HOST - cannot confirm anything"
info "$STATE"

# active/N, where N is restarts since the last explicit start. Non-zero means
# the unit is crash-looping: both units are Restart=always with no rate
# limit, so a dying service still samples as "active" and used to pass here.
check_unit () {
    local unit="$1" field state restarts
    field=$(printf '%s\n' "$STATE" | tr ' ' '\n' | grep "^$unit=" | head -1)
    [ -n "$field" ] || fail "$unit did not report a status"
    state="${field#*=}"; restarts="${state#*/}"; state="${state%%/*}"
    [ "$state" = "active" ] || fail "$unit is $state after deploy"
    if [ -n "$restarts" ] && [ "$restarts" != "0" ]; then
        fail "$unit is active but has restarted $restarts times - it is crash-looping"
    fi
    ok "$unit running (0 restarts)"
}
check_unit trisonica-logger
check_unit trisonica-usb-export
check_unit trisonica-status
check_unit trisonica-hdmi
# The heartbeat itself is a oneshot: between runs it is correctly inactive,
# so what has to be alive is the timer that fires it.
check_unit trisonica-alert.timer

# Data actually flowing, not merely a process that started. awk rather than
# bc: bc is absent from a Raspberry Pi OS Lite image, and its absence used to
# be reported as "the anemometer may be unplugged".
#
# The window is 30 s, and it used to be 10. The logger writes through an 8 kB
# buffer - about 55 rows - so the file's line count advances in steps, and
# whichever window you choose, the count can be off by up to one step. That
# error is a CONSTANT number of rows, so what a longer window buys is not a
# smaller error but a smaller error RELATIVE to the count:
#
#   10 s ->  100 rows +/- 55  =  4.5 .. 15.5 Hz   can fall under the threshold
#   30 s ->  300 rows +/- 55  =  8.2 .. 11.8 Hz   cannot
#
# Both ranges were measured on the station. On 2026-08-24 a unit recording
# perfectly at 10.1 Hz measured exactly 5.00 Hz here and was reported as "the
# anemometer may be unplugged" at the end of a flawless deploy; a check that
# cries wolf at the one step meant to confirm the deploy is worse than no
# check, because it is the line everyone learns to skip.
#
# W is defined once and used for both the sleep and the divisor, so the two
# can never drift apart.
RATE="$($SSH "$HOST" '
  W=30
  F=$(ls -t /home/pi/trisonica-data/*.csv 2>/dev/null | head -1)
  if [ -z "$F" ]; then echo "nofile"; exit 0; fi
  A=$(wc -l < "$F"); sleep $W; B=$(wc -l < "$F")
  if [ "$F" != "$(ls -t /home/pi/trisonica-data/*.csv 2>/dev/null | head -1)" ]; then
      echo "rotated"; exit 0            # the logger opened a new file mid-sample
  fi
  awk -v a="$A" -v b="$B" -v w="$W" "BEGIN{printf \"%.2f\", (b-a)/w}"')"

case "$RATE" in
    nofile)  bad "no data file yet - the anemometer may be unplugged (the logger is still fine)" ;;
    rotated) ok  "data is flowing (the logger rotated its file during the check)" ;;
    "")      bad "could not measure the sample rate (the logger is still fine)" ;;
    *)
        info "sample rate: ${RATE} Hz (30 s window)"
        if awk -v r="$RATE" 'BEGIN{exit !(r > 5)}'; then
            ok "data is flowing"
        else
            bad "only ${RATE} Hz over 30 s - the anemometer may be unplugged (the logger is still fine)"
        fi ;;
esac

$SSH "$HOST" 'sudo journalctl -u trisonica-logger -n 6 --no-pager' 2>/dev/null \
  | grep -oE "(started|status:|schema:).*" | tail -3 | sed 's/^/    /'

# Record what is now verified running. Written last, and only here: the marker
# has to mean "this exact code was deployed AND observed healthy", because
# that is the property the next deploy trusts when it decides whether a
# restart is needed.
$SSH "$HOST" "printf '%s %s\n%s %s\n%s %s\n%s %s\n%s %s\n' \
    trisonica-logger '$(unit_hash trisonica-logger)' \
    trisonica-usb-export '$(unit_hash trisonica-usb-export)' \
    trisonica-status '$(unit_hash trisonica-status)' \
    trisonica-hdmi '$(unit_hash trisonica-hdmi)' \
    trisonica-alert '$(unit_hash trisonica-alert)' > $STATE_FILE" \
  || bad "could not record the deployed state (the next run will restart both units)"
ok "recorded the deployed state"

say "Done."
echo "  ssh $HOST"
echo "  ssh $HOST 'journalctl -u trisonica-logger -f'"
echo ""

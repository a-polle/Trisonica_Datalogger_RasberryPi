#!/bin/sh
# Compatibility entry point for the off-site collector.
#
# The backup is implemented in trisonica_backup.py. This wrapper exists so the
# resilience suite beside it, and anything else that learned this path, keep
# working. Arguments pass straight through.
#
# The collector sits next to this file in a deployment and one level up in the
# repository, so look in both rather than assuming a layout.
#
# It replaced a standalone shell implementation. Two of that script's ideas
# were kept -- an flock so two runs cannot write the same partial file, and
# treating an unreachable station as a non-event rather than a failure -- and
# two of its assumptions had to go: it probed liveness with `ssh pi@host true`,
# which the station's forced command now correctly refuses, and it used an
# absolute remote path, which rrsync rewrites. Both would have stopped the
# backup silently, while the journal went on saying it had run.
here=$(dirname "$0")
for candidate in "$here/trisonica_backup.py" "$here/../trisonica_backup.py"; do
    if [ -f "$candidate" ]; then
        exec /usr/bin/python3 "$candidate" "$@"
    fi
done
echo "sync_trisonica.sh: cannot find trisonica_backup.py near $here" >&2
exit 1

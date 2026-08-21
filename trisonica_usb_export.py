#!/usr/bin/env python3
"""Copy logged data onto any USB stick plugged into the Pi.

Exists so a researcher with no terminal, no SSH and no network can retrieve
measurements: insert a stick, wait for the status LED to return to its normal
single flash, remove the stick. Nothing to type.

Runs as a SEPARATE root service rather than inside the logger. Mounting needs
privileges the logger deliberately does not have, and more importantly the
logger's storage path is the part that must never be destabilised - an export
bug can cost a copy, never the recording.

Safety rules, in order of importance:

  1. Never touch the Pi's own SD card. Candidates must be USB transport AND
     must not be the device carrying the root filesystem. Both are checked.
  2. Never write into the logger's data directory. Data flows one way.
  3. Never delete anything from either side.
  4. Any failure leaves the stick unmounted and the logger untouched.
"""

import errno
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

DATA_DIR = "/home/pi/trisonica-data"
MOUNT_POINT = "/mnt/trisonica-export"
# Read by the logger to drive the status LED. Presence means "copying, do not
# remove the stick". Its mtime is refreshed as the copy progresses: a process
# killed mid-copy cannot delete the file, and the logger gives this marker
# priority over every other LED state, so a stale one would otherwise hide a
# full disk or a missing anemometer for the rest of the deployment.
MARKER_BUSY = "/run/trisonica-export-busy"
# Remembers which sticks have already been exported to, so re-inserting the
# same one does not silently recopy on every poll.
STATE_FILE = "/run/trisonica-export-seen.json"

POLL_INTERVAL_S = 5.0
# Copy granularity. Also the interval at which the busy marker is refreshed,
# so "the export is alive" stays observable through a multi-gigabyte copy.
COPY_CHUNK = 1 << 20
SUPPORTED_FS = ("vfat", "exfat", "ntfs", "ext2", "ext3", "ext4")
MIN_FREE_MARGIN = 1.10          # need 10% more space than the payload
# Anything smaller than this is a boot, EFI or recovery partition, not a
# researcher's data stick. Observed live: a Ventoy stick presents a 32 MB
# VTOYEFI partition next to its 117 GB data partition.
MIN_DEVICE_BYTES = 512 * 1024 * 1024
SYSTEM_LABELS = ("VTOYEFI", "EFI", "BOOT", "RECOVERY", "SYSTEM")


def log(message):
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def run(args, timeout=60):
    """Run a command, returning (rc, stdout). Never raises."""
    try:
        p = subprocess.Popen(args, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        try:
            p.kill()
        except Exception:
            pass
        return 124, "timeout"
    except Exception as exc:
        return 1, str(exc)


def root_source():
    """Disk carrying '/', so we can never export onto the Pi's own card."""
    rc, out = run(["findmnt", "-no", "SOURCE", "/"])
    if rc != 0:
        return ""
    dev = out.strip()
    name = os.path.basename(dev)

    # Ask the kernel first. /sys/class/block/<name>/partition exists only for
    # partitions, and the parent directory is the disk that carries it. Exact,
    # with no string guessing and no extra subprocess on a path that runs on
    # every poll.
    node = os.path.join("/sys/class/block", name)
    try:
        if os.path.exists(os.path.join(node, "partition")):
            return os.path.basename(os.path.realpath(os.path.join(node, "..")))
    except OSError:
        pass

    # Fallback where sysfs is unavailable. The previous version stripped only
    # p1..p4, so /dev/mmcblk0p10 came back as "mmcblk0p" - a name matching no
    # disk, which silently disabled the "never the root disk" rule that is the
    # most important guarantee this service makes.
    m = re.match(r"^(.+\d)p(\d+)$", name)       # mmcblk0p12, nvme0n1p2
    if m:
        return m.group(1)
    m = re.match(r"^([a-zA-Z]+)\d+$", name)     # sda2, vda1
    if m:
        return m.group(1)
    return name


def candidates_checked():
    """(enumeration_ok, partitions).

    The flag matters because 'lsblk failed' and 'no sticks attached' both
    produce an empty list, and the watch loop treats an absent stick as one
    that was deliberately unplugged - it forgets it, so re-inserting exports
    again. Without the distinction a single lsblk timeout would forget every
    attached stick and copy the whole dataset a second time.
    """
    rc, out = run(["lsblk", "-J", "-b", "-o",
                   "NAME,PATH,TRAN,TYPE,FSTYPE,SIZE,MOUNTPOINT,PKNAME,UUID,LABEL"])
    if rc != 0:
        return False, []
    try:
        tree = json.loads(out)
    except ValueError:
        return False, []

    root_disk = root_source()
    found = []

    def walk(nodes, parent_tran=None):
        for node in nodes:
            tran = node.get("tran") or parent_tran
            children = node.get("children") or []
            if children:
                walk(children, tran)
            if node.get("type") != "part":
                continue
            if tran != "usb":                       # rule 1: USB only
                continue
            top = node.get("pkname") or ""
            if top and top == root_disk:            # rule 1: never the root disk
                continue
            if node.get("fstype") not in SUPPORTED_FS:
                continue
            size = node.get("size") or 0
            try:
                size = int(size)
            except (TypeError, ValueError):
                size = 0
            # Rule 5: ignore boot/EFI/recovery partitions. A Ventoy stick, a
            # Windows installer or any bootable medium carries a small system
            # partition alongside the usable one, and an export attempted
            # there would either fail or corrupt something that matters.
            if size < MIN_DEVICE_BYTES:
                continue
            label = (node.get("label") or "").upper()
            if any(tag in label for tag in SYSTEM_LABELS):
                continue
            found.append({
                "path": node.get("path"),
                "uuid": node.get("uuid") or node.get("path"),
                "fstype": node.get("fstype"),
                "size": size,
                "disk": top,
                "label": node.get("label") or "",
                "mountpoint": node.get("mountpoint"),
            })

    walk(tree.get("blockdevices") or [])
    # Largest first: on a multi-partition stick the usable partition is the
    # big one, and we only ever want to export once per stick.
    found.sort(key=lambda d: d["size"], reverse=True)
    return True, found


def candidates():
    """USB partitions with a mountable filesystem, excluding the root disk."""
    return candidates_checked()[1]


def load_seen():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_seen(seen):
    try:
        with open(STATE_FILE, "w") as fh:
            json.dump(seen, fh)
    except Exception:
        pass


def mark_busy():
    """Raise the 'copying, do not remove the stick' flag for the logger's LED."""
    try:
        open(MARKER_BUSY, "w").close()
    except OSError as exc:
        log("could not raise the busy marker: %s" % exc)


def touch_busy():
    """Refresh the busy marker so the logger can tell live from abandoned."""
    try:
        os.utime(MARKER_BUSY, None)
    except OSError:
        pass


def clear_busy():
    try:
        os.remove(MARKER_BUSY)
    except OSError:
        pass


def sha256(path, chunk=COPY_CHUNK, heartbeat=None):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
            if heartbeat:
                heartbeat()
    return h.hexdigest()


def copy_verified(src, dst, heartbeat=None):
    """Copy src to dst and confirm the stick holds what we read. Returns bool.

    The source is hashed WHILE it is being copied, not afterwards. The logger
    appends to its newest CSV continuously and fsyncs every few seconds, so a
    hash taken after the copy covers bytes the copy never saw and reports a
    mismatch on a copy that is in fact perfect. The guarantee that matters to
    the researcher is that the bytes written to the stick are exactly the
    bytes read from the card, and that is what this compares.
    """
    h_src = hashlib.sha256()
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            block = fin.read(COPY_CHUNK)
            if not block:
                break
            h_src.update(block)
            fout.write(block)
            if heartbeat:
                heartbeat()
        fout.flush()
        os.fsync(fout.fileno())
    shutil.copystat(src, dst)
    return h_src.hexdigest() == sha256(dst, heartbeat=heartbeat)


def data_files():
    try:
        names = sorted(n for n in os.listdir(DATA_DIR) if n.endswith(".csv"))
    except OSError:
        return []
    return [os.path.join(DATA_DIR, n) for n in names]


def export_to(dev):
    """Mount, copy, verify, unmount. Returns (ok, message)."""
    files = data_files()
    if not files:
        return False, "no data files to copy"

    payload = sum(os.path.getsize(f) for f in files)

    if not os.path.isdir(MOUNT_POINT):
        os.makedirs(MOUNT_POINT)

    already = dev.get("mountpoint")
    mounted_here = False
    target_root = already
    if not already:
        rc, out = run(["mount", "-o", "rw,nosuid,nodev,noexec",
                       dev["path"], MOUNT_POINT], timeout=30)
        if rc != 0:
            return False, "mount failed: " + out.strip()
        mounted_here = True
        target_root = MOUNT_POINT

    try:
        st = os.statvfs(target_root)
        free = st.f_bavail * st.f_frsize
        if free < payload * MIN_FREE_MARGIN:
            return False, ("not enough space: need %.0f MB, have %.0f MB"
                           % (payload / 1e6, free / 1e6))

        stamp = time.strftime("%Y-%m-%d_%H%M%S", time.gmtime())
        outdir = os.path.join(target_root, "TrisonicaData_" + stamp + "Z")
        os.makedirs(outdir)

        copied = 0
        verified = 0
        failures = []
        for src in files:
            dst = os.path.join(outdir, os.path.basename(src))
            try:
                if copy_verified(src, dst, heartbeat=touch_busy):
                    copied += 1
                    verified += 1
                else:
                    copied += 1
                    failures.append(os.path.basename(src) + ": checksum mismatch")
            except Exception as exc:
                failures.append(os.path.basename(src) + ": " + str(exc))

        summary = [
            "TriSonica data export",
            "exported (UTC): " + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source        : %s:%s" % (os.uname()[1], DATA_DIR),
            "files copied  : %d of %d" % (copied, len(files)),
            "checksum OK   : %d of %d" % (verified, copied),
            "total bytes   : %d" % payload,
            "",
            "Each file was hashed with SHA-256 as it was read, and the copy on",
            "this stick was then re-read and hashed again. 'checksum OK' equal",
            "to 'files copied' means every copy is byte-for-byte identical to",
            "what was read from the logger.",
            "",
            "The newest file is the session that was still being recorded, so",
            "it ends at the moment of the copy. Everything up to that point is",
            "complete; insert the stick again later for the rest.",
            "",
            "Nothing was deleted from the logger. The data remains on the Pi.",
        ]
        if failures:
            summary += ["", "PROBLEMS:"] + ["  " + f for f in failures]

        with open(os.path.join(outdir, "EXPORT_STATUS.txt"), "w") as fh:
            fh.write("\n".join(summary) + "\n")

        os.sync()
        ok = (verified == len(files)) and not failures
        return ok, "%d/%d files verified into %s" % (
            verified, len(files), os.path.basename(outdir))
    finally:
        if mounted_here:
            os.sync()
            for attempt in range(5):
                rc, _ = run(["umount", MOUNT_POINT], timeout=30)
                if rc == 0:
                    break
                time.sleep(1.0)


def install_signal_handlers():
    """Drop the busy marker on a clean stop.

    systemctl restart sends SIGTERM, whose default action kills the process
    without running the `finally` that clears the marker. The logger gives
    the marker priority over every other LED state, so an orphaned one would
    mask a full disk or a missing anemometer for the rest of the deployment.
    """
    def stop(signum, frame):
        clear_busy()
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, stop)
        except (ValueError, OSError, RuntimeError):
            pass          # not the main thread; the startup sweep still covers us


def main(list_devices=None, exporter=None, sleeper=time.sleep, max_passes=None):
    log("usb export watcher started (polling every %.0fs)" % POLL_INTERVAL_S)
    log("data source: %s" % DATA_DIR)
    log("root disk (never exported to): %s" % root_source())

    install_signal_handlers()
    # A predecessor killed mid-copy cannot have removed its own marker, and a
    # stale one blinds the LED. Nothing is copying at startup by definition.
    clear_busy()

    # Per-BOOT memory, deliberately.
    #
    # STATE_FILE lives on /run (tmpfs), so it is empty after a reboot and
    # populated within one. That gives both behaviours we need:
    #
    #   fresh boot with a stick attached  -> not in `seen` -> exported.
    #     The natural researcher action is plug in, power on, expect a copy;
    #     an earlier version pre-marked these and produced silence instead.
    #
    #   service restart within a boot     -> in `seen` -> NOT re-exported.
    #     The unit runs Restart=always with no rate limit, so without this a
    #     crash loop would litter the stick with duplicate folders.
    seen = load_seen()

    passes = 0
    while max_passes is None or passes < max_passes:
        try:
            poll_once(seen, list_devices=list_devices, exporter=exporter)
        except Exception as exc:
            log("watcher error (continuing): %s" % exc)
            clear_busy()
        passes += 1
        sleeper(POLL_INTERVAL_S)
    return seen


def poll_once(seen, list_devices=None, exporter=None):
    """One pass of the watch loop, factored out so it can be tested.

    `list_devices` returns (enumeration_ok, devices); `exporter` returns
    (ok, message). Both are injectable because the real ones need a mounted
    USB stick and root. Without a seam here the only testable thing is the
    source text, which cannot distinguish "exports a stick present at
    startup" from "contains no mention of startup".
    """
    if list_devices is None:
        list_devices = candidates_checked
    if exporter is None:
        exporter = export_to

    enumerated, present = list_devices()
    present_uuids = set(d["uuid"] for d in present)

    # Forget sticks that have been removed, so re-inserting one deliberately
    # exports again. Only when the enumeration is trustworthy: a failed lsblk
    # also reports nothing present, and acting on that would re-export every
    # attached stick on the next pass.
    if enumerated:
        forgotten = [u for u in seen if u not in present_uuids]
        for uuid in forgotten:
            del seen[uuid]
        if forgotten:
            save_seen(seen)

    for dev in present:
        if dev["uuid"] in seen:
            continue
        log("USB stick detected: %s (%s, %s)"
            % (dev["path"], dev["fstype"], dev["size"]))
        mark_busy()
        try:
            ok, message = exporter(dev)
        except OSError as exc:
            # By far the most likely export failure: the researcher pulled the
            # stick before the copy finished. Name it, rather than reporting a
            # bare errno that reads like a fault in the unit.
            if exc.errno in (errno.EIO, errno.ENODEV, errno.ENXIO,
                             errno.ESTALE):
                ok, message = False, ("stick removed during the copy (%s) - "
                                      "nothing was lost from the Pi; insert "
                                      "it again to retry" % exc.strerror)
            else:
                ok, message = False, "error: %s" % exc
        except Exception as exc:
            ok, message = False, "unexpected error: %s" % exc
        finally:
            clear_busy()

        log(("export OK: " if ok else "export FAILED: ") + message)
        seen[dev["uuid"]] = message
        if ok:
            # Mark every partition on the same physical stick, so a successful
            # export is never followed by an attempt on a sibling partition.
            for other in present:
                if other["disk"] and other["disk"] == dev["disk"]:
                    seen[other["uuid"]] = "sibling of an exported partition"
        save_seen(seen)
        return dev          # one export per pass; re-poll for the rest

    return None


if __name__ == "__main__":
    # main() only returns when a pass limit is set, which nothing but the
    # tests does; the service form loops until SIGTERM raises SystemExit.
    main()
    sys.exit(0)

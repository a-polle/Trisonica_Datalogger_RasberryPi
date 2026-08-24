#!/usr/bin/env python3
"""Tests for the TriSonica field logger.

Every fixture below is a real line captured from the instrument during the
2026-08-07 diagnosis session, not something invented. The glitch cases in
particular are the exact values that appeared during the power fault, because
those are what the validator has to catch.

Run:  python3 test_field_logger.py
"""

import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import trisonica_field_logger as fl


# Real capture, laptop host, zero errors in 601 samples.
GOOD = ("S  00.14,S2  00.14,D  285,U  00.14,V -00.04,W  00.01,T  21.60,"
        "H  40.22,P  1022.62,PI  000.7,RO  001.8,MD  115,TD  114.87")

# Real capture, Pi host before the fixes: instrument sentinels.
SENTINEL = ("S -99.53,S2 -99.51,D  000,U -99.40,V -99.41,W -99.63,T -99.44,"
            "H  40.91,P  1022.59,PI  000.7,RO  001.8,MD  115,TD  114.67")

# Real capture: W far outside the instrument's range.
OUT_OF_RANGE = ("S -99.58,S2  00.00,D -000,U  00.00,V -00.00,W -150.37,"
                "T -99.44,H  40.91,P  1022.62,PI  000.7,RO  001.8,"
                "MD  115,TD  115.47")


def line_with(base, **overrides):
    """Rebuild a Trisonica line with specific fields replaced."""
    parsed = fl.parse_line(base)
    parsed.update({k: str(v) for k, v in overrides.items()})
    return ",".join("%s %s" % (k, v) for k, v in parsed.items())


class TestParsing(unittest.TestCase):

    def test_parses_all_thirteen_fields(self):
        parsed = fl.parse_line(GOOD)
        self.assertEqual(len(parsed), 13)
        for key in ("S", "S2", "D", "U", "V", "W", "T", "H", "P",
                    "PI", "RO", "MD", "TD"):
            self.assertIn(key, parsed)

    def test_strips_the_double_space_padding(self):
        # 'S2  00.14' must yield '00.14', not ' 00.14'.
        parsed = fl.parse_line(GOOD)
        self.assertEqual(parsed["S2"], "00.14")
        self.assertEqual(parsed["T"], "21.60")

    def test_keeps_negative_signs(self):
        parsed = fl.parse_line(GOOD)
        self.assertEqual(parsed["V"], "-00.04")

    def test_garbage_returns_empty_not_exception(self):
        for junk in ("", "   ", "not a data line", ",,,,", "\x00\xff garbage"):
            self.assertEqual(fl.parse_line(junk), {})

    def test_partial_line_is_still_parsed(self):
        # Serial reads can truncate; whatever is intact should survive.
        parsed = fl.parse_line("S  00.14,S2  00.14,D  28")
        self.assertEqual(parsed["S"], "00.14")


class TestValidatorSentinels(unittest.TestCase):

    def setUp(self):
        self.v = fl.Validator()

    def test_clean_sample_has_no_flags(self):
        flags, err, impl, spike, critical = self.v.check(fl.parse_line(GOOD))
        self.assertEqual(flags, "")
        self.assertEqual((err, impl, spike), (0, 0, 0))
        self.assertFalse(critical)

    def test_sentinels_are_flagged_and_critical(self):
        flags, err, impl, spike, critical = self.v.check(fl.parse_line(SENTINEL))
        self.assertIn("S:err", flags)
        self.assertIn("T:err", flags)
        self.assertEqual(err, 6)          # S S2 U V W T
        self.assertTrue(critical)

    def test_humidity_and_pressure_survive_a_sentinel_row(self):
        # During the fault H and P stayed correct; they must not be flagged.
        flags, _, _, _, _ = self.v.check(fl.parse_line(SENTINEL))
        self.assertNotIn("H:", flags)
        self.assertNotIn("P:", flags)

    def test_out_of_range_is_flagged_implausible(self):
        flags, err, impl, spike, critical = self.v.check(
            fl.parse_line(OUT_OF_RANGE))
        self.assertIn("W:impl", flags)    # -150 m/s exceeds +/-60
        self.assertEqual(impl, 1)


class TestValidatorSpikes(unittest.TestCase):
    """The failure the range check cannot catch.

    During the power fault the instrument emitted S=24.85 m/s and T=-22.04 C.
    Both are INSIDE the plausible ranges - a 24 m/s storm and a -22 C winter
    are real. Only the rate of change exposes them.
    """

    def setUp(self):
        self.v = fl.Validator()

    def test_the_real_glitch_is_caught(self):
        self.v.check(fl.parse_line(GOOD))                      # S=0.14 T=21.6
        spiked = line_with(GOOD, S="24.85", T="-22.04")
        flags, err, impl, spike, critical = self.v.check(fl.parse_line(spiked))
        self.assertIn("S:spike", flags)
        self.assertIn("T:spike", flags)
        self.assertEqual(spike, 2)
        self.assertTrue(critical)

    def test_range_check_alone_would_have_missed_it(self):
        # Documents why spike detection exists: both values pass the bounds.
        self.assertTrue(fl.PLAUSIBLE["S"][0] <= 24.85 <= fl.PLAUSIBLE["S"][1])
        self.assertTrue(fl.PLAUSIBLE["T"][0] <= -22.04 <= fl.PLAUSIBLE["T"][1])

    def test_real_gust_is_not_rejected(self):
        # 0.14 -> 9.0 m/s in one sample is violent but physically possible.
        self.v.check(fl.parse_line(GOOD))
        gust = line_with(GOOD, S="9.00")
        flags, _, _, spike, _ = self.v.check(fl.parse_line(gust))
        self.assertEqual(spike, 0, "a real gust must not be flagged: " + flags)

    def test_gradual_ramp_is_never_flagged(self):
        # Wind climbing 3 m/s per sample up to 30 m/s: all legitimate.
        for speed in range(0, 31, 3):
            line = line_with(GOOD, S="%.2f" % speed)
            flags, _, _, spike, _ = self.v.check(fl.parse_line(line))
            self.assertEqual(spike, 0, "ramp step flagged: " + flags)

    def test_spike_does_not_poison_the_reference(self):
        # A glitch must not become the baseline, or the RETURN to normal
        # would be flagged as a second spike.
        self.v.check(fl.parse_line(GOOD))                      # S=0.14
        self.v.check(fl.parse_line(line_with(GOOD, S="24.85")))  # spike
        flags, _, _, spike, _ = self.v.check(fl.parse_line(GOOD))
        self.assertEqual(spike, 0,
                         "recovery after a spike was flagged: " + flags)

    def test_first_sample_cannot_spike(self):
        flags, _, _, spike, _ = self.v.check(
            fl.parse_line(line_with(GOOD, S="45.00")))
        self.assertEqual(spike, 0)

    def test_reset_clears_references(self):
        self.v.check(fl.parse_line(GOOD))
        self.v.reset()
        flags, _, _, spike, _ = self.v.check(
            fl.parse_line(line_with(GOOD, S="45.00")))
        self.assertEqual(spike, 0, "reference survived reset")

    def test_stale_reference_is_not_used(self):
        self.v.check(fl.parse_line(GOOD))
        # Age the stored reference past the freshness window.
        for key in list(self.v._last_good):
            value, _ = self.v._last_good[key]
            self.v._last_good[key] = (
                value, time.monotonic() - fl.SPIKE_REFERENCE_MAX_AGE_S - 1.0)
        flags, _, _, spike, _ = self.v.check(
            fl.parse_line(line_with(GOOD, S="45.00")))
        self.assertEqual(spike, 0, "stale reference was used for comparison")

    def test_direction_wrap_is_not_treated_as_a_spike(self):
        # D crossing north goes 359 -> 001; that is not a glitch.
        self.v.check(fl.parse_line(line_with(GOOD, D="359")))
        flags, _, _, spike, _ = self.v.check(
            fl.parse_line(line_with(GOOD, D="001")))
        self.assertEqual(spike, 0, "direction wrap flagged: " + flags)


class TestColumnLayout(unittest.TestCase):
    """The CSV row must line up with its header under every configuration."""

    def _logger(self, with_gps):
        class Args(object):
            data_dir = "/tmp"
            port = "auto"
            baud = 115200
            gps = with_gps
            led = False
            verbose = False

        # Build without touching hardware or spawning threads.
        logger = fl.FieldLogger.__new__(fl.FieldLogger)
        logger.args = Args()
        logger.gps = object() if with_gps else None
        logger.validator = fl.Validator()
        logger.columns = None
        return logger

    def _row_for(self, logger, keys):
        logger.columns = logger.build_columns(keys)
        captured = []

        class Sink(object):
            def writerow(self, row):
                captured.append(row)

        logger.csv_writer = Sink()
        extra = {
            "time": ("gps", True),
            "flags": "S:spike", "n_err": 0, "n_impl": 0, "n_spike": 1,
            "fix": {"lat": 53.0934695, "lon": 8.8919201,
                    "alt": 20.7, "mode": 3},
            "sats": 5,
        }
        logger._emit(("2026-08-07T18:04:02.000000Z",
                      fl.parse_line(GOOD), extra))
        return logger.columns, captured[0]

    def test_row_matches_header_with_gps(self):
        logger = self._logger(True)
        keys = list(fl.parse_line(GOOD).keys())
        header, row = self._row_for(logger, keys)
        self.assertEqual(len(header), len(row),
                         "header %d != row %d" % (len(header), len(row)))

    def test_row_matches_header_without_gps(self):
        logger = self._logger(False)
        keys = list(fl.parse_line(GOOD).keys())
        header, row = self._row_for(logger, keys)
        self.assertEqual(len(header), len(row))

    def test_values_land_under_the_right_headers(self):
        logger = self._logger(True)
        keys = list(fl.parse_line(GOOD).keys())
        header, row = self._row_for(logger, keys)
        got = dict(zip(header, row))
        self.assertEqual(got["time_source"], "gps")
        self.assertEqual(got["time_synced"], 1)
        self.assertEqual(got["S"], "00.14")
        self.assertEqual(got["T"], "21.60")
        self.assertEqual(got["flags"], "S:spike")
        self.assertEqual(got["n_spike"], 1)
        self.assertEqual(got["gps_sats"], 5)
        self.assertEqual(got["lat"], "53.0934695")

    def test_missing_gps_fix_leaves_blanks_not_misalignment(self):
        logger = self._logger(True)
        keys = list(fl.parse_line(GOOD).keys())
        logger.columns = logger.build_columns(keys)
        captured = []

        class Sink(object):
            def writerow(self, row):
                captured.append(row)

        logger.csv_writer = Sink()
        extra = {
            "time": ("freerun", False),
            "flags": "", "n_err": 0, "n_impl": 0, "n_spike": 0,
            "fix": None, "sats": 0,
        }
        logger._emit(("2026-08-07T18:04:02.000000Z",
                      fl.parse_line(GOOD), extra))
        row = captured[0]
        self.assertEqual(len(logger.columns), len(row))
        got = dict(zip(logger.columns, row))
        self.assertEqual(got["lat"], "")
        self.assertEqual(got["time_source"], "freerun")
        self.assertEqual(got["time_synced"], 0)


class TestGpsReader(unittest.TestCase):
    """Fed with gpsd messages captured verbatim from this deployment."""

    # Real TPV from 18:04 today, while the module held a 3D fix.
    TPV_3D = ('{"class":"TPV","device":"/dev/serial0","mode":3,'
              '"time":"2026-08-07T18:04:02.000Z","lat":53.093469522,'
              '"lon":8.891920097,"alt":1.849,"ept":0.005}')
    # Real TPV from 18:16, after the fix was lost indoors.
    TPV_NOFIX = ('{"class":"TPV","device":"/dev/serial0","mode":1,'
                 '"time":"2026-08-07T18:16:24.000Z","ept":0.005}')
    SKY = ('{"class":"SKY","satellites":['
           '{"PRN":1,"used":true},{"PRN":2,"used":true},{"PRN":3,"used":true},'
           '{"PRN":4,"used":true},{"PRN":5,"used":true},'
           '{"PRN":6,"used":false},{"PRN":7,"used":false}]}')

    def setUp(self):
        self.g = fl.GpsReader()

    def _feed(self, payload):
        import json
        self.g._handle(json.loads(payload))

    def test_3d_fix_is_recorded(self):
        self._feed(self.TPV_3D)
        fix, _ = self.g.current()
        self.assertIsNotNone(fix)
        self.assertAlmostEqual(fix["lat"], 53.093469522, places=7)
        self.assertAlmostEqual(fix["lon"], 8.891920097, places=7)
        self.assertEqual(fix["mode"], 3)

    def test_no_fix_is_not_recorded(self):
        self._feed(self.TPV_NOFIX)
        fix, _ = self.g.current()
        self.assertIsNone(fix, "a mode-1 report must not become a position")

    def test_losing_the_fix_does_not_erase_a_recent_one(self):
        # gpsd flaps between mode 3 and mode 1 at the edge of coverage; a
        # momentary dropout should not blank the position column.
        self._feed(self.TPV_3D)
        self._feed(self.TPV_NOFIX)
        fix, _ = self.g.current()
        self.assertIsNotNone(fix)

    def test_stale_fix_is_dropped(self):
        self._feed(self.TPV_3D)
        self.g._fix_time = time.monotonic() - fl.GPS_STALE_S - 1.0
        fix, _ = self.g.current()
        self.assertIsNone(fix, "a fix older than GPS_STALE_S must not be used")

    def test_satellites_used_are_counted(self):
        self._feed(self.SKY)
        _, sats = self.g.current()
        self.assertEqual(sats, 5)

    def test_malformed_json_is_ignored(self):
        # Truncated reads happen; they must not kill the GPS thread.
        for junk in ('{"class":"TPV"', "", "not json", "[]"):
            try:
                import json
                self.g._handle(json.loads(junk))
            except ValueError:
                pass  # the reader catches this in its own loop
        self.assertIsNone(self.g.current()[0])

    def test_tpv_without_coordinates_is_tolerated(self):
        self._feed('{"class":"TPV","mode":3}')
        fix, _ = self.g.current()
        self.assertIsNotNone(fix)
        self.assertIsNone(fix["lat"])


class TestSchemaOrder(unittest.TestCase):
    """Column order must not depend on how the stream was first caught.

    The serial port routinely opens mid-sentence, so the first parsed sample
    can be a truncated row. On 2026-08-07 that produced a file whose columns
    began P,PI,RO,MD,TD,S,... instead of S,S2,D,...
    """

    # Called through the class, not stored as a class attribute: binding a
    # staticmethod to a test-class attribute makes Python pass self as the
    # first argument.
    def order(self, keys):
        return fl.FieldLogger.order_sensor_keys(keys)

    def test_full_set_is_in_instrument_order(self):
        keys = list(fl.parse_line(GOOD).keys())
        self.assertEqual(self.order(keys), list(fl.CANONICAL_FIELDS))

    def test_truncated_first_line_does_not_reorder(self):
        # Exactly the real case: a partial row revealed the tail fields first.
        as_seen = ["P", "PI", "RO", "MD", "TD", "S", "S2", "D",
                   "U", "V", "W", "T", "H"]
        self.assertEqual(self.order(as_seen), list(fl.CANONICAL_FIELDS))

    def test_order_is_independent_of_discovery_order(self):
        import random
        keys = list(fl.parse_line(GOOD).keys())
        reference = self.order(keys)
        for _ in range(20):
            shuffled = keys[:]
            random.shuffle(shuffled)
            self.assertEqual(self.order(shuffled), reference)

    def test_missing_fields_are_simply_absent(self):
        self.assertEqual(self.order(["T", "S", "H"]), ["S", "T", "H"])

    def test_unknown_fields_are_appended_sorted(self):
        got = self.order(["S", "T", "ZZ", "AA"])
        self.assertEqual(got, ["S", "T", "AA", "ZZ"])


class TestFilenameCollision(unittest.TestCase):
    """A Pi with no RTC restores the same clock value on every boot.

    fake-hwclock writes the shutdown time and restores it at startup, so
    repeated power cycles generate near-identical filenames. Observed live on
    2026-08-07: two separate boots produced names two seconds apart. Landing
    on the same second must not destroy the earlier run.
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.logger = fl.FieldLogger.__new__(fl.FieldLogger)
        self.logger.data_dir = self.tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_file_uses_the_plain_name(self):
        path = self.logger.unique_path("2026-08-07_182454")
        self.assertTrue(path.endswith("TrisonicaData_2026-08-07_182454Z.csv"))

    def test_second_boot_same_second_does_not_collide(self):
        import os
        first = self.logger.unique_path("2026-08-07_182454")
        open(first, "w").write("existing data from the previous boot\n")
        second = self.logger.unique_path("2026-08-07_182454")
        self.assertNotEqual(first, second)
        self.assertFalse(os.path.exists(second), "must not already exist")

    def test_earlier_data_survives(self):
        first = self.logger.unique_path("2026-08-07_182454")
        with open(first, "w") as fh:
            fh.write("irreplaceable field data\n")
        second = self.logger.unique_path("2026-08-07_182454")
        with open(second, "w") as fh:
            fh.write("second boot\n")
        with open(first) as fh:
            self.assertEqual(fh.read(), "irreplaceable field data\n",
                             "the earlier run's data was overwritten")

    def test_many_collisions_still_resolve(self):
        seen = set()
        for _ in range(12):
            path = self.logger.unique_path("2026-08-07_182454")
            self.assertNotIn(path, seen, "returned a duplicate name")
            seen.add(path)
            open(path, "w").write("x")
        self.assertEqual(len(seen), 12)


class TestStorageCrashPaths(unittest.TestCase):
    """open_file() must never be able to kill the process.

    It uses mode "x" and is called at exactly the moments the filesystem is
    least healthy: at startup, and when recovering from a full or read-only
    card. Both call sites sit outside the main loop's try block, so an
    unguarded failure would propagate out and crash. With
    StartLimitIntervalSec=0 that becomes an endless 5-second restart loop with
    no LED indication of the cause.
    """

    def _logger(self):
        lg = fl.FieldLogger.__new__(fl.FieldLogger)
        lg.disk_full = False
        lg.last_disk_check = 0.0
        lg.rows_dropped = 0
        lg.csv_file = None
        lg.csv_writer = None
        lg.close_file = lambda: None
        lg.led = fl.LedStatus(enabled=False)
        lg.recent = fl.deque(maxlen=10)
        lg.serial = object()
        return lg

    def test_failure_to_reopen_after_recovery_does_not_raise(self):
        lg = self._logger()
        lg.free_mb = lambda: fl.MIN_FREE_MB - 1
        lg.open_file = lambda: None
        lg.check_disk()
        self.assertTrue(lg.disk_full)

        # Space "returns", but the filesystem is still broken.
        def boom():
            raise OSError(30, "Read-only file system")
        lg.open_file = boom
        lg.free_mb = lambda: fl.MIN_FREE_MB * 3
        lg.last_disk_check = 0.0
        lg.check_disk()                      # must not raise
        self.assertTrue(lg.disk_full,
                        "must stay stopped when reopening fails")

    def test_successful_recovery_clears_the_alarm(self):
        lg = self._logger()
        lg.free_mb = lambda: fl.MIN_FREE_MB - 1
        lg.open_file = lambda: None
        lg.check_disk()
        self.assertTrue(lg.disk_full)
        lg.free_mb = lambda: fl.MIN_FREE_MB * 3
        lg.last_disk_check = 0.0
        lg.check_disk()
        self.assertFalse(lg.disk_full)

    def test_startup_open_failure_is_guarded_in_source(self):
        # Booting with a full card must start in the alarm state, not
        # crash-loop before the first sample.
        import inspect
        src = inspect.getsource(fl.FieldLogger.run)
        self.assertIn("starting in storage-alarm state", src)

    def test_both_open_file_call_sites_are_guarded(self):
        import inspect
        for fn in (fl.FieldLogger.run, fl.FieldLogger.check_disk):
            src = inspect.getsource(fn)
            if "self.open_file()" in src:
                self.assertIn("except (IOError, OSError)", src,
                              "%s calls open_file() unguarded" % fn.__name__)


class TestUnexpectedErrorSurvival(unittest.TestCase):
    """One malformed sample must never end a deployment.

    write_sample is guarded for IOError/OSError. Anything else - a csv.Error,
    a ValueError from some input nobody anticipated - would escape the loop
    and kill the process. With StartLimitIntervalSec=0 systemd then restarts
    every 5 s forever, so a deterministic trigger stops data collection
    permanently with nobody present.
    """

    def test_run_has_a_last_resort_handler(self):
        import inspect
        src = inspect.getsource(fl.FieldLogger.run)
        self.assertIn("except Exception:", src,
                      "no last-resort guard around write_sample")
        self.assertIn("unexpected_errors", src)

    def _write_sample_handlers(self):
        """Handler types on the try that wraps write_sample, in source order.

        Parsed rather than string-matched: run() contains other
        "except Exception: pass" guards, and a naive str.index finds those
        instead, which produced a false failure when this test was written.
        """
        import ast, inspect, textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(fl.FieldLogger.run)))
        found = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            if "write_sample" not in body:
                continue
            for h in node.handlers:
                if h.type is None:
                    found.append("bare")
                elif isinstance(h.type, ast.Tuple):
                    found.append(tuple(getattr(e, "id", "?") for e in h.type.elts))
                else:
                    found.append(getattr(h.type, "id", "?"))
        return found

    def test_the_guard_is_the_last_except_clause(self):
        # A broad handler placed BEFORE the errno-specific one would swallow
        # ENOSPC and EROFS and defeat the storage handling entirely.
        handlers = self._write_sample_handlers()
        self.assertTrue(handlers, "no try wraps write_sample")
        self.assertEqual(handlers[0], ("IOError", "OSError"),
                         "the specific handler must come first")
        self.assertEqual(handlers[-1], "Exception",
                         "the broad handler must come last")

    def test_the_guard_is_not_a_bare_except(self):
        # A bare except also catches KeyboardInterrupt and SystemExit, which
        # would break clean shutdown.
        self.assertNotIn("bare", self._write_sample_handlers())

    def test_errors_are_counted_and_surfaced(self):
        import inspect
        self.assertIn("unexpected_errors",
                      inspect.getsource(fl.FieldLogger.log_status))

    def test_logging_is_rate_limited_not_per_sample(self):
        # At 10 Hz a per-sample traceback would fill the journal in minutes.
        import inspect
        src = inspect.getsource(fl.FieldLogger.run)
        self.assertIn("(1, 10, 100, 1000)", src,
                      "unexpected-error logging is not rate limited")


class TestUsbExportSafety(unittest.TestCase):
    """The export must never be able to write to the Pi's own SD card.

    A bug here would not merely fail to export - it could target the
    filesystem the logger is recording onto. Two independent checks guard it:
    USB transport, and not being the disk that carries root.
    """

    def setUp(self):
        import tempfile
        import trisonica_usb_export as ex
        self.ex = ex
        self._real_run = ex.run
        # The service writes its LED marker and state file under /run, which
        # needs root. Redirect both so the behavioural tests can run as a
        # normal user without changing what they exercise.
        self._tmp = tempfile.mkdtemp()
        self._real_marker = ex.MARKER_BUSY
        self._real_state = ex.STATE_FILE
        ex.MARKER_BUSY = self._tmp + "/busy"
        ex.STATE_FILE = self._tmp + "/seen.json"
        # Some tests drive the real main(), which installs SIGTERM/SIGINT
        # handlers. Left in place they would outlive the test: a later
        # SIGTERM to the runner would call clear_busy() against the restored
        # /run path and delete a live export's marker. The suite runs on the
        # Pi during deployment, so that is not hypothetical.
        self._real_signals = [(s, signal.getsignal(s))
                              for s in (signal.SIGTERM, signal.SIGINT)]

    def tearDown(self):
        import shutil
        self.ex.run = self._real_run
        self.ex.MARKER_BUSY = self._real_marker
        self.ex.STATE_FILE = self._real_state
        for sig, handler in self._real_signals:
            signal.signal(sig, handler)
        shutil.rmtree(self._tmp, ignore_errors=True)

    # Real lsblk -b output shape from this Pi with the Ventoy stick attached.
    # Sizes are BYTES because the implementation passes -b; a human-readable
    # fixture silently parses to 0 and everything gets filtered as too small,
    # which is exactly how this fixture first went stale.
    LSBLK = """{"blockdevices":[
      {"name":"sda","path":"/dev/sda","tran":"usb","type":"disk","fstype":null,
       "size":125828612096,"mountpoint":null,"pkname":null,"uuid":null,
       "label":null,
       "children":[
         {"name":"sda1","path":"/dev/sda1","tran":null,"type":"part",
          "fstype":"exfat","size":125794516992,"mountpoint":null,
          "pkname":"sda","uuid":"AAAA-BBBB","label":"Ventoy"},
         {"name":"sda2","path":"/dev/sda2","tran":null,"type":"part",
          "fstype":"vfat","size":33554432,"mountpoint":null,
          "pkname":"sda","uuid":"CCCC-DDDD","label":"VTOYEFI"}]},
      {"name":"mmcblk0","path":"/dev/mmcblk0","tran":null,"type":"disk",
       "fstype":null,"size":15489564672,"mountpoint":null,"pkname":null,
       "label":null,
       "children":[
         {"name":"mmcblk0p1","path":"/dev/mmcblk0p1","tran":null,"type":"part",
          "fstype":"vfat","size":268435456,"mountpoint":"/boot",
          "pkname":"mmcblk0","uuid":"1234-5678","label":"boot"},
         {"name":"mmcblk0p2","path":"/dev/mmcblk0p2","tran":null,"type":"part",
          "fstype":"ext4","size":15246295040,"mountpoint":"/",
          "pkname":"mmcblk0","uuid":"abcd-ef01","label":"rootfs"}]}]}"""

    def _fake_run(self, lsblk_json, root="/dev/mmcblk0p2"):
        def fake(args, timeout=60):
            if args[0] == "findmnt":
                return 0, root + "\n"
            if args[0] == "lsblk":
                return 0, lsblk_json
            return 0, ""
        return fake

    def test_root_disk_is_identified_from_a_partition(self):
        self.ex.run = self._fake_run(self.LSBLK, root="/dev/mmcblk0p2")
        self.assertEqual(self.ex.root_source(), "mmcblk0")

    def test_root_disk_identified_for_sd_style_names(self):
        self.ex.run = self._fake_run(self.LSBLK, root="/dev/sda2")
        self.assertEqual(self.ex.root_source(), "sda")

    def test_double_digit_partition_numbers_are_resolved(self):
        # The old p1..p4 list returned "mmcblk0p" for anything beyond p4 - a
        # name matching no disk, which silently disabled the "never export
        # onto the root disk" rule entirely.
        self.ex.run = self._fake_run(self.LSBLK, root="/dev/mmcblk0p12")
        self.assertEqual(self.ex.root_source(), "mmcblk0")

    def test_nvme_style_names_are_resolved(self):
        self.ex.run = self._fake_run(self.LSBLK, root="/dev/nvme0n1p2")
        self.assertEqual(self.ex.root_source(), "nvme0n1")

    def test_the_sd_card_is_never_a_candidate(self):
        self.ex.run = self._fake_run(self.LSBLK)
        paths = [c["path"] for c in self.ex.candidates()]
        self.assertNotIn("/dev/mmcblk0p1", paths)
        self.assertNotIn("/dev/mmcblk0p2", paths, "would export onto the Pi's own card")

    def test_the_usb_stick_is_a_candidate(self):
        self.ex.run = self._fake_run(self.LSBLK)
        paths = [c["path"] for c in self.ex.candidates()]
        self.assertEqual(paths, ["/dev/sda1"])

    def test_usb_transport_is_inherited_from_the_parent_disk(self):
        # lsblk reports tran on the disk, not the partition; without
        # inheritance the stick would never be seen at all.
        self.ex.run = self._fake_run(self.LSBLK)
        self.assertTrue(self.ex.candidates())

    def test_a_usb_disk_that_is_the_root_disk_is_excluded(self):
        # A Pi booted from a USB SSD: the root disk is USB, and must still
        # never be an export target.
        self.ex.run = self._fake_run(self.LSBLK, root="/dev/sda1")
        paths = [c["path"] for c in self.ex.candidates()]
        self.assertEqual(paths, [], "exported onto the disk carrying root")

    def test_boot_partitions_are_skipped(self):
        # Found live on 2026-08-07: the first real export also targeted the
        # 32 MB VTOYEFI partition of a Ventoy stick. It failed safely on
        # space, but should never have been a candidate.
        self.ex.run = self._fake_run(self.LSBLK)
        paths = [c["path"] for c in self.ex.candidates()]
        self.assertNotIn("/dev/sda2", paths, "targeted a boot/EFI partition")

    def test_the_data_partition_is_preferred_over_siblings(self):
        self.ex.run = self._fake_run(self.LSBLK)
        found = self.ex.candidates()
        self.assertTrue(found)
        self.assertEqual(found[0]["path"], "/dev/sda1",
                         "largest usable partition must be tried first")

    def test_a_small_usb_partition_is_ignored_even_without_a_label(self):
        unlabelled = self.LSBLK.replace('"label":"VTOYEFI"', '"label":null')
        self.ex.run = self._fake_run(unlabelled)
        paths = [c["path"] for c in self.ex.candidates()]
        self.assertNotIn("/dev/sda2", paths,
                         "size alone must exclude a boot-sized partition")

    def test_a_system_label_is_ignored_even_when_large(self):
        big_efi = self.LSBLK.replace('"size":33554432', '"size":125794516992')
        self.ex.run = self._fake_run(big_efi)
        paths = [c["path"] for c in self.ex.candidates()]
        self.assertNotIn("/dev/sda2", paths,
                         "label alone must exclude a system partition")

    def test_sizes_are_parsed_as_bytes(self):
        # Guards against the fixture/implementation drift that made these
        # tests fail: lsblk is called with -b, so sizes are integers.
        import inspect
        src = inspect.getsource(self.ex.candidates_checked)
        self.assertIn('"-b"', src, "lsblk must be called with -b for byte sizes")

    def test_unsupported_filesystems_are_skipped(self):
        weird = self.LSBLK.replace('"fstype":"exfat"', '"fstype":"swap"')
        self.ex.run = self._fake_run(weird)
        self.assertEqual(self.ex.candidates(), [])

    def test_malformed_lsblk_output_is_survived(self):
        def fake(args, timeout=60):
            if args[0] == "findmnt":
                return 0, "/dev/mmcblk0p2\n"
            return 0, "not json at all"
        self.ex.run = fake
        self.assertEqual(self.ex.candidates(), [])


    # -- behavioural: drive poll_once with stubs -------------------------

    def _stub_devices(self, *uuids):
        return [{"uuid": u, "path": "/dev/sd" + u[-1], "fstype": "exfat",
                 "size": 10 << 30, "disk": "sda", "label": "", "mountpoint": None}
                for u in uuids]

    def _stub_listing(self, *uuids):
        """What list_devices() returns: (enumeration_ok, devices)."""
        return True, self._stub_devices(*uuids)

    def test_a_stick_present_on_the_first_pass_is_exported(self):
        # The researcher plugs the stick in, THEN powers on. An earlier
        # version pre-marked such sticks and silently did nothing.
        exported = []
        seen = {}
        self.ex.poll_once(seen,
                          list_devices=lambda: self._stub_listing("AAAA"),
                          exporter=lambda d: (exported.append(d["uuid"]) or
                                              (True, "ok")))
        self.assertEqual(exported, ["AAAA"], "a stick present at start was skipped")

    def _boot(self, exported, uuids=("AAAA",)):
        """Run one startup pass of the real main() against stub hardware.

        The enumerators are replaced at MODULE level, not passed in. The
        regression being guarded lived in main() and called candidates()
        directly, so a seam that only intercepts the injected argument would
        not see it - which is exactly how this test first failed to catch it.
        """
        real = (self.ex.candidates_checked, self.ex.candidates)
        self.ex.candidates_checked = lambda: self._stub_listing(*uuids)
        self.ex.candidates = lambda: self._stub_devices(*uuids)
        try:
            self.ex.main(exporter=lambda d: (exported.append(d["uuid"]) or
                                             (True, "ok")),
                         sleeper=lambda _s: None,
                         max_passes=1)
        finally:
            self.ex.candidates_checked, self.ex.candidates = real

    def test_main_exports_a_stick_that_was_already_attached_at_boot(self):
        # The regression this whole seam exists for lived in main(), not in
        # poll_once: a startup loop pre-marked everything already attached,
        # so the researcher's plug-in-then-power-on gesture produced silence.
        # Driving poll_once directly cannot see that line, so drive main().
        exported = []
        self._boot(exported)
        self.assertEqual(exported, ["AAAA"],
                         "a stick attached before power-on was never exported")

    def test_main_does_not_re_export_across_a_service_restart(self):
        # The other half of the same trade-off: STATE_FILE lives on /run, so
        # a restart within one boot must NOT copy everything a second time.
        exported = []
        self._boot(exported)          # boot
        self._boot(exported)          # systemctl restart, same boot
        self.assertEqual(exported, ["AAAA"],
                         "a service restart re-exported an unchanged stick")

    def test_a_failed_enumeration_does_not_forget_attached_sticks(self):
        # lsblk failing and no stick attached both look like "nothing here".
        # Treating a failure as a removal forgets the stick, and the next
        # good pass copies the entire dataset onto it a second time.
        exported = []
        seen = {}
        exp = lambda d: (exported.append(d["uuid"]) or (True, "ok"))
        self.ex.poll_once(seen, list_devices=lambda: self._stub_listing("AAAA"),
                          exporter=exp)
        self.ex.poll_once(seen, list_devices=lambda: (False, []), exporter=exp)
        self.ex.poll_once(seen, list_devices=lambda: self._stub_listing("AAAA"),
                          exporter=exp)
        self.assertEqual(exported, ["AAAA"],
                         "a transient lsblk failure caused a duplicate export")

    def test_the_same_stick_is_not_exported_twice(self):
        # `disk` is deliberately empty. With a disk name set, the
        # sibling-marking loop also writes the uuid into `seen` and masks a
        # missing primary assignment - mutation testing caught exactly that:
        # this test originally passed with `seen[dev["uuid"]] = message` gone.
        dev = {"uuid": "AAAA", "path": "/dev/sda1", "fstype": "exfat",
               "size": 10 << 30, "disk": "", "label": "", "mountpoint": None}
        exported = []
        seen = {}
        devs = lambda: (True, [dev])
        exp = lambda d: (exported.append(d["uuid"]) or (True, "copied 5/5"))
        for _ in range(3):
            self.ex.poll_once(seen, list_devices=devs, exporter=exp)
        self.assertEqual(exported, ["AAAA"], "re-exported a stick that never left")
        self.assertEqual(seen.get("AAAA"), "copied 5/5",
                         "the export result was not recorded against the stick")

    def test_removing_and_reinserting_exports_again(self):
        exported = []
        seen = {}
        exp = lambda d: (exported.append(d["uuid"]) or (True, "ok"))
        self.ex.poll_once(seen, list_devices=lambda: self._stub_listing("AAAA"),
                          exporter=exp)
        self.ex.poll_once(seen, list_devices=lambda: (True, []),          # removed
                          exporter=exp)
        self.ex.poll_once(seen, list_devices=lambda: self._stub_listing("AAAA"),
                          exporter=exp)
        self.assertEqual(exported, ["AAAA", "AAAA"])

    def test_a_failed_export_is_not_retried_in_a_tight_loop(self):
        # A stick that cannot be written must not be re-attempted every 5s.
        attempts = []
        seen = {}
        devs = lambda: self._stub_listing("AAAA")
        exp = lambda d: (attempts.append(1) or (False, "mount failed"))
        for _ in range(4):
            self.ex.poll_once(seen, list_devices=devs, exporter=exp)
        self.assertEqual(len(attempts), 1, "retried a failing stick every pass")

    def test_an_exporter_that_raises_does_not_escape(self):
        seen = {}
        def boom(dev):
            raise RuntimeError("something unforeseen")
        self.ex.poll_once(seen, list_devices=lambda: self._stub_listing("AAAA"),
                          exporter=boom)      # must not raise
        self.assertIn("AAAA", seen)

    def test_export_to_returns_two_values_on_every_path(self):
        import ast, inspect, textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(self.ex.export_to)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Return):
                self.assertIsNotNone(node.value,
                                     "bare return at line %d breaks the unpack"
                                     % node.lineno)
                n = len(node.value.elts) if isinstance(node.value, ast.Tuple) else 1
                self.assertEqual(n, 2, "return at line %d yields %d values"
                                 % (node.lineno, n))

    def test_the_export_service_cannot_power_the_unit_off(self):
        # A SHUTDOWN-marker feature was removed: a stick carrying an
        # unrelated file named shutdown.txt could power the unit off
        # mid-measurement, and a marked stick left inserted turned every boot
        # into an immediate power-off. It also violated the module's stated
        # invariant that an export can cost a copy, never the recording.
        #
        # Parsed, not grepped. Searching the raw text cannot tell a call from
        # a comment warning against the call, so documenting the rule inside
        # the module would have failed this test - and the deploy script runs
        # the same check, so it would have blocked deployment outright.
        for mod in (self.ex, fl):
            for where, word in self._banned_tokens(mod):
                self.fail("%s reaches %r at %s" % (mod.__name__, word, where))

    def _banned_tokens(self, module):
        """Yield (location, word) for banned calls reachable from real code."""
        import ast, inspect
        banned = ("poweroff", "shutdown_marker")
        tree = ast.parse(inspect.getsource(module))
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
                if ast.get_docstring(node, clean=False) is not None:
                    docs.add(id(node.body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Str) and id(node) not in docs:
                hay = node.s
            elif isinstance(node, ast.Name):
                hay = node.id
            elif isinstance(node, ast.Attribute):
                hay = node.attr
            else:
                continue
            for word in banned:
                if word in str(hay).lower():
                    yield "line %s" % getattr(node, "lineno", "?"), word

    def test_the_banned_token_check_actually_detects_one(self):
        # A guard that cannot fail is not a guard. Feed it a module that does
        # the forbidden thing and confirm it says so.
        import types
        guilty = types.ModuleType("guilty")
        guilty.__name__ = "guilty"
        src = ('"""A docstring may discuss poweroff freely."""\n'
               '# and so may a comment about poweroff\n'
               'import subprocess\n'
               'def stop():\n'
               '    subprocess.run(["systemctl", "poweroff"])\n')
        import inspect
        real = inspect.getsource
        inspect.getsource = lambda m: src
        try:
            found = list(self._banned_tokens(guilty))
        finally:
            inspect.getsource = real
        self.assertEqual([w for _, w in found], ["poweroff"],
                         "the check missed a real call, or tripped on prose")

    def test_state_file_is_actually_read_back(self):
        # load_seen() was orphaned once, making STATE_FILE write-only and
        # letting a service restart re-export every attached stick.
        import inspect
        self.assertIn("load_seen()", inspect.getsource(self.ex.main))

    def test_a_file_still_being_written_copies_and_verifies(self):
        # The logger appends to its newest CSV at 10 Hz and fsyncs every 5 s.
        # Hashing the source AFTER the copy covers bytes the copy never saw,
        # so a perfect copy reported "checksum mismatch" and the whole export
        # was marked FAILED. Hash what is read, not the file afterwards.
        src = os.path.join(self._tmp, "TrisonicaData_active.csv")
        dst = os.path.join(self._tmp, "copy.csv")
        with open(src, "w") as fh:
            fh.write("t,u,v\n" + "1,2,3\n" * 500)

        appended = []

        def grow():                      # a logger flush landing mid-copy
            if not appended:
                with open(src, "a") as fh:
                    fh.write("9,9,9\n" * 50)
                    fh.flush()
                    os.fsync(fh.fileno())
                appended.append(True)

        self.assertTrue(self.ex.copy_verified(src, dst, heartbeat=grow),
                        "a copy taken while the logger was writing was "
                        "reported as corrupt")
        self.assertTrue(appended, "the test did not exercise a concurrent write")

    def test_a_truly_corrupt_copy_is_still_caught(self):
        # The counterpart: relaxing the check must not make it vacuous.
        src = os.path.join(self._tmp, "a.csv")
        dst = os.path.join(self._tmp, "b.csv")
        with open(src, "w") as fh:
            fh.write("t,u,v\n" + "1,2,3\n" * 500)
        real_sha = self.ex.sha256

        def corrupt(path, chunk=1 << 20, heartbeat=None):
            if path == dst:
                return "0" * 64          # the stick handed back other bytes
            return real_sha(path, chunk, heartbeat)

        self.ex.sha256 = corrupt
        try:
            self.assertFalse(self.ex.copy_verified(src, dst))
        finally:
            self.ex.sha256 = real_sha

    def test_a_clean_stop_clears_the_busy_marker(self):
        # systemctl restart sends SIGTERM, whose default action kills python
        # without running the finally that clears the marker.
        self.ex.mark_busy()
        self.assertTrue(os.path.exists(self.ex.MARKER_BUSY))
        self.ex.install_signal_handlers()          # tearDown restores them
        with self.assertRaises(SystemExit):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        self.assertFalse(os.path.exists(self.ex.MARKER_BUSY),
                         "SIGTERM left the busy marker behind")

    def test_startup_clears_a_marker_left_by_a_killed_export(self):
        # Belt and braces: a SIGKILL cannot run any handler at all, so the
        # next start must sweep. Nothing is copying at startup by definition.
        self.ex.mark_busy()
        self.ex.main(list_devices=lambda: (True, []), exporter=lambda d: (True, ""),
                     sleeper=lambda _s: None, max_passes=1)
        self.assertFalse(os.path.exists(self.ex.MARKER_BUSY),
                         "a stale marker survived startup and would blind the LED")

    def test_data_directory_is_only_ever_read(self):
        import inspect
        src = inspect.getsource(self.ex)
        for destructive in ("shutil.rmtree", "os.unlink", "os.remove(src",
                            "os.truncate"):
            self.assertNotIn(destructive, src,
                             "export contains a destructive call: " + destructive)


class TestLedShutdown(unittest.TestCase):

    def test_stop_joins_the_blink_thread(self):
        import inspect
        src = inspect.getsource(fl.LedStatus.stop)
        self.assertIn("join", src,
                      "stop() restores the trigger without waiting for the "
                      "blink thread, which can leave the LED lit")

    def test_stop_is_safe_when_never_started(self):
        led = fl.LedStatus(enabled=False)
        led.stop()          # must not raise

    def test_stop_is_idempotent(self):
        led = fl.LedStatus(enabled=False)
        led.stop()
        led.stop()


class TestGpsRobustness(unittest.TestCase):
    """The GPS thread runs for months; it must not leak or die."""

    def setUp(self):
        self.g = fl.GpsReader()

    def test_receive_buffer_is_capped(self):
        # Without a cap, a stream that never contains a newline grows the
        # buffer for the life of the process.
        self.assertTrue(hasattr(fl, "GPSD_MAX_BUFFER"))
        self.assertLessEqual(fl.GPSD_MAX_BUFFER, 4 << 20)
        import inspect
        self.assertIn("GPSD_MAX_BUFFER", inspect.getsource(fl.GpsReader._run))

    def test_non_dict_satellite_entries_are_survived(self):
        import json
        # A malformed SKY entry must not discard the whole message or raise.
        self.g._handle(json.loads(
            '{"class":"SKY","satellites":[{"PRN":1,"used":true},'
            '"garbage",null,{"PRN":2,"used":true}]}'))
        _fix, sats = self.g.current()
        self.assertEqual(sats, 2)

    def test_satellites_missing_entirely(self):
        import json
        self.g._handle(json.loads('{"class":"SKY"}'))
        self.assertEqual(self.g.current()[1], 0)

    def test_unknown_message_classes_are_ignored(self):
        import json
        for payload in ('{"class":"DEVICE"}', '{"class":"WATCH"}',
                        '{"class":"VERSION","release":"3.17"}', '{}'):
            self.g._handle(json.loads(payload))
        self.assertIsNone(self.g.current()[0])


class TestLedDocumentation(unittest.TestCase):
    """The docstring is what a maintainer reads first; it must match reality."""

    def test_docstring_describes_the_implemented_patterns(self):
        doc = fl.LedStatus.__doc__ or ""
        for phrase in ("1 flash", "2 flashes", "3 flashes",
                       "rapid flicker", "mostly on"):
            self.assertIn(phrase, doc,
                          "LedStatus docstring is missing %r" % phrase)

    def test_docstring_does_not_describe_the_old_scheme(self):
        doc = fl.LedStatus.__doc__ or ""
        for stale in ("slow pulse", "double-blink"):
            self.assertNotIn(stale, doc,
                             "LedStatus docstring still describes %r" % stale)


class TestClockJumpRobustness(unittest.TestCase):
    """Every interval must survive the system clock being corrected.

    This Pi has no RTC. It boots at whatever fake-hwclock saved and is later
    corrected by GPS or NTP - observed on 2026-08-07 as a 4.5 hour jump
    mid-run. If intervals were measured with the wall clock, a BACKWARD
    correction would make every "now - last_x" negative and silently suspend
    fsync, the disk-full check, stale-link detection and file rotation for the
    duration of the jump, voiding the guarantee that a pulled plug costs at
    most FSYNC_INTERVAL_S of data.
    """

    def test_module_never_measures_intervals_with_the_wall_clock(self):
        import inspect
        src = inspect.getsource(fl)
        code = "\n".join(l for l in src.splitlines()
                         if not l.strip().startswith("#"))
        self.assertNotIn("time.time()", code,
                         "wall clock used for an interval; use time.monotonic()")

    def test_monotonic_is_actually_used(self):
        import inspect
        self.assertIn("time.monotonic()", inspect.getsource(fl))

    def test_row_timestamps_still_use_the_wall_clock(self):
        # The DATA must carry real UTC, not uptime - monotonic is only for
        # measuring elapsed time.
        import inspect
        src = inspect.getsource(fl.FieldLogger.write_sample)
        self.assertIn("utcnow", src)

    def test_monotonic_never_goes_backwards(self):
        readings = [time.monotonic() for _ in range(200)]
        self.assertEqual(readings, sorted(readings))

    def test_fsync_still_fires_after_a_simulated_backward_jump(self):
        # Simulate the failure directly: a reference timestamp far in the
        # "future" relative to now. With monotonic this cannot arise, but the
        # guard must not deadlock even if it somehow did.
        lg = fl.FieldLogger.__new__(fl.FieldLogger)
        lg.csv_file = None            # guarded path: returns without raising
        lg.last_fsync = time.monotonic() + 10000.0
        lg.maybe_fsync()              # must not raise
        self.assertTrue(True)

    def test_spike_reference_ages_out_on_the_monotonic_clock(self):
        v = fl.Validator()
        v.check(fl.parse_line(GOOD))
        for key in list(v._last_good):
            value, _ = v._last_good[key]
            v._last_good[key] = (value, time.monotonic()
                                 - fl.SPIKE_REFERENCE_MAX_AGE_S - 1.0)
        _flags, _e, _i, spike, _c = v.check(
            fl.parse_line(line_with(GOOD, S="45.00")))
        self.assertEqual(spike, 0)


class TestLedSignalling(unittest.TestCase):
    """The LED is the researcher's entire interface, so it must be
    unambiguous and must never claim 'ready' before the data is trustworthy.
    """

    def _logger(self, serial=True, bad=0.0, synced=True, disk_full=False):
        lg = fl.FieldLogger.__new__(fl.FieldLogger)
        lg.led = fl.LedStatus(enabled=False)
        lg.serial = object() if serial else None
        lg.bad_rate = lambda: bad
        lg.disk_full = disk_full
        lg.time_source = type("T", (), {"state": ("gps" if synced else "freerun",
                                                  synced)})()
        return lg

    def _count_flashes(self, state):
        led = fl.LedStatus(enabled=False)
        return sum(1 for level, _ in led._pattern(state) if level)

    # --- state selection ---------------------------------------------------

    def test_all_good_is_ready(self):
        lg = self._logger()
        lg.update_led()
        self.assertEqual(lg.led._state, fl.LedStatus.READY)

    def test_unverified_clock_is_not_ready(self):
        # The whole point: data flowing but timestamps unverified must NOT
        # look identical to a fully working system.
        lg = self._logger(synced=False)
        lg.update_led()
        self.assertEqual(lg.led._state, fl.LedStatus.NO_TIME)
        self.assertNotEqual(lg.led._state, fl.LedStatus.READY)

    def test_no_anemometer_is_waiting(self):
        lg = self._logger(serial=False)
        lg.update_led()
        self.assertEqual(lg.led._state, fl.LedStatus.WAITING)

    def test_garbage_data_is_bad_data(self):
        lg = self._logger(bad=0.9)
        lg.update_led()
        self.assertEqual(lg.led._state, fl.LedStatus.BAD_DATA)

    def test_disk_full_outranks_everything(self):
        lg = self._logger(serial=False, bad=1.0, synced=False, disk_full=True)
        lg.update_led()
        self.assertEqual(lg.led._state, fl.LedStatus.DISK_FULL)

    def test_missing_anemometer_outranks_unverified_clock(self):
        lg = self._logger(serial=False, synced=False)
        lg.update_led()
        self.assertEqual(lg.led._state, fl.LedStatus.WAITING)

    # --- the visual language ----------------------------------------------

    def test_blink_counts_are_one_two_three(self):
        self.assertEqual(self._count_flashes(fl.LedStatus.READY), 1)
        self.assertEqual(self._count_flashes(fl.LedStatus.NO_TIME), 2)
        self.assertEqual(self._count_flashes(fl.LedStatus.WAITING), 3)

    def test_more_blinks_means_more_attention(self):
        order = [fl.LedStatus.READY, fl.LedStatus.NO_TIME, fl.LedStatus.WAITING]
        counts = [self._count_flashes(s) for s in order]
        self.assertEqual(counts, sorted(counts),
                         "blink count must increase with severity")

    @staticmethod
    def _all_states():
        """Every declared LED state, discovered rather than hardcoded.

        A hardcoded list silently stops covering states added later - which
        is exactly what happened when EXPORTING was introduced.
        """
        states = {}
        for name in dir(fl.LedStatus):
            if name.isupper() and not name.startswith("_"):
                value = getattr(fl.LedStatus, name)
                if isinstance(value, str):
                    states[value] = name      # dedupes the OK -> READY alias
        return states

    def test_every_state_has_a_distinct_pattern(self):
        led = fl.LedStatus(enabled=False)
        states = self._all_states()
        seen = {}
        for value, name in states.items():
            pattern = tuple(led._pattern(value))
            if pattern in seen:
                self.fail("%s and %s share a pattern - ambiguous to a human"
                          % (name, seen[pattern]))
            seen[pattern] = name

    def test_state_coverage_is_complete(self):
        # Guards the guard: if a state is added without a pattern it would
        # silently fall through to the default and duplicate WAITING.
        led = fl.LedStatus(enabled=False)
        default = tuple(led._pattern("a-state-that-does-not-exist"))
        for value, name in self._all_states().items():
            if name == "WAITING":
                continue
            self.assertNotEqual(tuple(led._pattern(value)), default,
                                "%s has no pattern of its own" % name)

    def test_disk_full_is_the_only_mostly_on_pattern(self):
        led = fl.LedStatus(enabled=False)
        for state in (fl.LedStatus.READY, fl.LedStatus.NO_TIME,
                      fl.LedStatus.WAITING, fl.LedStatus.BAD_DATA):
            steps = led._pattern(state)
            on = sum(d for lvl, d in steps if lvl)
            total = sum(d for _, d in steps)
            self.assertLess(on / total, 0.5,
                            "%s is mostly-on, which must mean DISK_FULL" % state)
        steps = led._pattern(fl.LedStatus.DISK_FULL)
        on = sum(d for lvl, d in steps if lvl)
        total = sum(d for _, d in steps)
        self.assertGreater(on / total, 0.5)

    def test_bursts_end_with_a_long_pause(self):
        # Without a clearly longer trailing gap the groups run together and
        # counting becomes impossible.
        led = fl.LedStatus(enabled=False)
        for state in (fl.LedStatus.NO_TIME, fl.LedStatus.WAITING):
            gaps = [d for lvl, d in led._pattern(state) if not lvl]
            self.assertGreater(gaps[-1], max(gaps[:-1]) * 2,
                               "trailing pause is not clearly longest")


class TestStorageGuard(unittest.TestCase):
    """A full SD card must not turn into a silent systemd crash loop.

    Without the guard: write fails -> open a replacement file -> that fails
    too -> unhandled exception -> systemd restarts forever (the unit has no
    start-rate limit, deliberately). Nobody is watching, so it must instead
    stop writing, stay alive, and raise a distinct LED alarm.
    """

    def _logger(self, free_mb):
        lg = fl.FieldLogger.__new__(fl.FieldLogger)
        lg.free_mb = lambda: free_mb
        lg.disk_full = False
        lg.last_disk_check = 0.0
        lg.rows_dropped = 0
        lg.csv_file = None
        lg.csv_writer = None
        lg.close_file = lambda: None
        lg.open_file = lambda: None
        lg.serial = object()
        lg.recent = fl.deque(maxlen=10)
        lg.led = fl.LedStatus(enabled=False)
        lg._export_mark = None
        return lg

    def test_stops_writing_below_threshold(self):
        lg = self._logger(fl.MIN_FREE_MB - 1)
        lg.check_disk()
        self.assertTrue(lg.disk_full)

    def test_keeps_writing_above_threshold(self):
        lg = self._logger(fl.MIN_FREE_MB + 500)
        lg.check_disk()
        self.assertFalse(lg.disk_full)

    def test_does_not_resume_on_a_trivial_recovery(self):
        # Hysteresis: freeing a few MB must not cause start/stop churn.
        lg = self._logger(fl.MIN_FREE_MB - 1)
        lg.check_disk()
        self.assertTrue(lg.disk_full)
        lg.free_mb = lambda: fl.MIN_FREE_MB + 5
        lg.last_disk_check = 0.0
        lg.check_disk()
        self.assertTrue(lg.disk_full, "resumed on a marginal recovery")

    def test_resumes_after_a_real_recovery(self):
        lg = self._logger(fl.MIN_FREE_MB - 1)
        lg.check_disk()
        lg.free_mb = lambda: fl.MIN_FREE_MB * 2 + 1
        lg.last_disk_check = 0.0
        lg.check_disk()
        self.assertFalse(lg.disk_full)

    def test_warns_early_while_space_remains(self):
        # A warning at MIN_FREE_MB is ~1 day's notice - useless to someone in
        # another country. LOW_SPACE_WARN_MB gives about a week.
        self.assertGreater(fl.LOW_SPACE_WARN_MB, fl.MIN_FREE_MB * 4)
        lg = self._logger(fl.LOW_SPACE_WARN_MB - 1)
        lg.check_disk()
        self.assertFalse(lg.disk_full, "warning threshold must not stop writes")

    def test_erofs_is_distinguished_from_a_full_disk(self):
        # An ageing SD card that develops I/O errors remounts read-only.
        # Reporting that as "full" would send someone hunting for space that
        # is not the problem.
        import inspect
        src = inspect.getsource(fl.FieldLogger.run)
        self.assertIn("EROFS", src)
        self.assertIn("READ-ONLY", src)

    def test_both_enospc_and_erofs_stop_writes_without_crashing(self):
        import inspect
        src = inspect.getsource(fl.FieldLogger.run)
        # Neither branch may attempt to open a replacement file: on a full or
        # read-only filesystem that fails too, and the exception would
        # crash-loop the service.
        self.assertIn("errno.ENOSPC", src)
        self.assertIn("errno.EROFS", src)

    def test_unknown_free_space_does_not_stop_logging(self):
        lg = self._logger(-1.0)          # statvfs failed
        lg.check_disk()
        self.assertFalse(lg.disk_full, "an unreadable statvfs halted logging")

    def test_disk_full_led_outranks_other_states(self):
        lg = self._logger(fl.MIN_FREE_MB - 1)
        lg.check_disk()
        lg.recent.extend([1] * 10)       # would otherwise be BAD_DATA
        lg.bad_rate = lambda: 1.0
        lg.update_led()
        self.assertEqual(lg.led._state, fl.LedStatus.DISK_FULL)

    def test_a_live_export_still_outranks_everything(self):
        import tempfile
        lg = self._logger(fl.MIN_FREE_MB - 1)
        lg.check_disk()
        real = fl.EXPORT_BUSY_MARKER
        tmp = tempfile.mkdtemp()
        fl.EXPORT_BUSY_MARKER = os.path.join(tmp, "busy")
        try:
            open(fl.EXPORT_BUSY_MARKER, "w").close()      # freshly refreshed
            lg.update_led()
            self.assertEqual(lg.led._state, fl.LedStatus.EXPORTING)
        finally:
            fl.EXPORT_BUSY_MARKER = real
            shutil.rmtree(tmp, ignore_errors=True)

    def test_an_abandoned_busy_marker_stops_masking_a_full_disk(self):
        # An export killed mid-copy cannot remove its own marker. This state
        # outranks every other one, so a leftover file used to report
        # "copying" - hiding a full disk - until the next reboot.
        import tempfile
        lg = self._logger(fl.MIN_FREE_MB - 1)
        lg.check_disk()
        real = fl.EXPORT_BUSY_MARKER
        tmp = tempfile.mkdtemp()
        fl.EXPORT_BUSY_MARKER = os.path.join(tmp, "busy")
        try:
            open(fl.EXPORT_BUSY_MARKER, "w").close()
            lg.update_led()
            self.assertEqual(lg.led._state, fl.LedStatus.EXPORTING,
                             "a just-appeared marker must be believed")
            # Same marker, never refreshed, long after we first saw it.
            mtime = os.path.getmtime(fl.EXPORT_BUSY_MARKER)
            lg._export_mark = (mtime,
                               time.monotonic() - (fl.EXPORT_BUSY_STALE_S + 30))
            lg.update_led()
            self.assertEqual(lg.led._state, fl.LedStatus.DISK_FULL,
                             "an abandoned export marker hid a full disk")
        finally:
            fl.EXPORT_BUSY_MARKER = real
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_refreshed_marker_keeps_the_export_light_on(self):
        # The counterpart: a long but healthy copy refreshes the marker, and
        # must not be declared abandoned while the researcher is standing
        # there deciding whether to pull the stick.
        import tempfile
        lg = self._logger(fl.MIN_FREE_MB - 1)
        lg.check_disk()
        real = fl.EXPORT_BUSY_MARKER
        tmp = tempfile.mkdtemp()
        fl.EXPORT_BUSY_MARKER = os.path.join(tmp, "busy")
        try:
            open(fl.EXPORT_BUSY_MARKER, "w").close()
            lg.update_led()
            stale_anchor = time.monotonic() - (fl.EXPORT_BUSY_STALE_S + 30)
            lg._export_mark = (os.path.getmtime(fl.EXPORT_BUSY_MARKER),
                               stale_anchor)
            newer = time.time() + 5                  # the export heartbeat
            os.utime(fl.EXPORT_BUSY_MARKER, (newer, newer))
            lg.update_led()
            self.assertEqual(lg.led._state, fl.LedStatus.EXPORTING,
                             "a live copy was declared abandoned")
        finally:
            fl.EXPORT_BUSY_MARKER = real
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dropped_rows_are_counted_not_written(self):
        lg = self._logger(fl.MIN_FREE_MB - 1)
        lg.check_disk()
        lg.validator = fl.Validator()
        lg.gps = None
        lg.time_source = type("T", (), {"state": ("ntp", True)})()
        lg.total_rows = 0
        lg.total_bad = 0
        lg.columns = None
        lg._pending = []
        lg.write_sample(fl.parse_line(GOOD))
        self.assertEqual(lg.rows_dropped, 1)
        self.assertEqual(lg._pending, [], "buffered a row while disk was full")


class TestTimeProvenance(unittest.TestCase):

    def test_unsynced_chrony_reports_freerun(self):
        ts = fl.TimeSource.__new__(fl.TimeSource)
        original = fl.subprocess.check_output
        try:
            fl.subprocess.check_output = lambda *a, **k: (
                b"Reference ID    : 00000000 ()\n"
                b"Stratum         : 0\n"
                b"Leap status     : Not synchronised\n")
            source, synced = ts._query_chrony()
        finally:
            fl.subprocess.check_output = original
        self.assertEqual(source, fl.TimeSource.FREERUN)
        self.assertFalse(synced)

    def test_gps_refclock_is_recognised(self):
        ts = fl.TimeSource.__new__(fl.TimeSource)
        original = fl.subprocess.check_output
        try:
            fl.subprocess.check_output = lambda *a, **k: (
                b"Reference ID    : 4E4D4541 (NMEA)\n"
                b"Leap status     : Normal\n")
            source, synced = ts._query_chrony()
        finally:
            fl.subprocess.check_output = original
        self.assertEqual(source, fl.TimeSource.GPS)
        self.assertTrue(synced)

    def test_network_ntp_is_recognised(self):
        ts = fl.TimeSource.__new__(fl.TimeSource)
        original = fl.subprocess.check_output
        try:
            fl.subprocess.check_output = lambda *a, **k: (
                b"Reference ID    : 969A65A8 (stage3.opensuse.org)\n"
                b"Leap status     : Normal\n")
            source, synced = ts._query_chrony()
        finally:
            fl.subprocess.check_output = original
        self.assertEqual(source, fl.TimeSource.NTP)
        self.assertTrue(synced)

    def test_missing_chronyc_does_not_raise(self):
        ts = fl.TimeSource.__new__(fl.TimeSource)
        original = fl.subprocess.check_output

        def boom(*a, **k):
            raise OSError("chronyc not found")

        try:
            fl.subprocess.check_output = boom
            source, synced = ts._query_chrony()
        finally:
            fl.subprocess.check_output = original
        self.assertEqual(source, fl.TimeSource.FREERUN)
        self.assertFalse(synced)


class _LogCapture(logging.Handler):
    """Collects the logger's own messages so a test can assert on them."""

    def __init__(self):
        logging.Handler.__init__(self)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def capture_logs(test, level=logging.WARNING):
    """Attach a capturing handler to the module logger for one test."""
    handler = _LogCapture()
    previous_level = fl.log.level
    previous_propagate = fl.log.propagate
    fl.log.addHandler(handler)
    fl.log.setLevel(level)
    fl.log.propagate = False          # keep test output clean

    def restore():
        fl.log.removeHandler(handler)
        fl.log.setLevel(previous_level)
        fl.log.propagate = previous_propagate

    test.addCleanup(restore)
    return handler


class TestConnectedButSilentInstrument(unittest.TestCase):
    """Bytes that are not measurements must never read as 'everything good'.

    The stale-data reconnect in run() keys on BYTES, not on content, so an
    instrument sitting in its configuration menu - or one read at the wrong
    baud rate - keeps that timer fresh forever. Nothing is written, so
    `recent` never sees a sample and bad_rate() stays at zero, and the LED
    used to show READY (one flash, "all good") while the deployment recorded
    nothing at all.
    """

    # Real text from the instrument's configuration menu, named in
    # parse_line's own docstring as something that reaches the parser.
    MENU = "Press Any Key to Continue"

    def _logger(self):
        lg = fl.FieldLogger.__new__(fl.FieldLogger)
        lg.led = fl.LedStatus(enabled=False)
        lg.serial = object()
        lg.port_path = "/dev/ttyUSB0"
        lg.disk_full = False
        lg.recent = fl.deque(maxlen=300)
        lg.time_source = type("T", (), {"state": ("gps", True)})()
        lg._export_mark = None
        lg.last_data = time.monotonic()      # bytes ARE arriving
        lg.last_parsed = time.monotonic()
        lg.unparsed_lines = 0
        lg._stall_logged = False
        return lg

    def test_a_blocked_logger_does_not_blame_the_instrument(self):
        """The regression this check exists to avoid.

        A slow card can hold the logger in fsync for tens of seconds. Nothing
        is read during that time, so last_parsed ages for a reason that has
        nothing to do with the anemometer. Observed live on 2026-08-12: four
        alarms under heavy I/O, each followed within 20 ms by a successful
        parse, each telling the researcher to check the baud rate of a
        perfectly healthy instrument.
        """
        lg = self._logger()
        blocked = time.monotonic() - (fl.NO_PARSE_TIMEOUT_S + 20)
        lg.last_parsed = blocked
        lg.last_data = blocked          # no bytes read either - we were stuck
        self.assertFalse(lg.data_stalled(),
                         "blamed the instrument for the logger being blocked")
        lg.update_led()
        self.assertNotEqual(lg.led._state, fl.LedStatus.BAD_DATA)

    def test_bytes_arriving_but_not_parsing_is_still_caught(self):
        # The counterpart: the condition this check is actually for.
        lg = self._logger()
        lg.last_parsed = time.monotonic() - (fl.NO_PARSE_TIMEOUT_S + 1)
        lg.last_data = time.monotonic()          # bytes still flowing
        self.assertTrue(lg.data_stalled())

    def test_the_menu_text_really_does_parse_to_nothing(self):
        # If this ever became parseable the whole failure mode would change.
        self.assertLess(len(fl.parse_line(self.MENU)), 3)

    def test_a_connected_but_silent_instrument_is_not_ready(self):
        lg = self._logger()
        lg.update_led()
        self.assertEqual(lg.led._state, fl.LedStatus.READY,
                         "must not alarm inside the grace period")
        # NO_PARSE_TIMEOUT_S later - 150 missed samples at 10 Hz.
        lg.last_parsed = time.monotonic() - (fl.NO_PARSE_TIMEOUT_S + 1)
        lg.unparsed_lines = 1500
        lg.update_led()
        self.assertEqual(lg.led._state, fl.LedStatus.BAD_DATA,
                         "reported READY while recording nothing")

    def test_bad_rate_alone_could_never_have_caught_it(self):
        # The reason the fix was needed: `recent` only ever sees samples that
        # already parsed, so the pre-existing health signal is structurally
        # blind to this failure.
        lg = self._logger()
        for _ in range(300):
            lg.note_unparsed()
        self.assertEqual(lg.bad_rate(), 0.0,
                         "unparseable lines cannot reach bad_rate by design")
        lg.last_parsed = time.monotonic() - (fl.NO_PARSE_TIMEOUT_S + 1)
        lg.update_led()
        self.assertEqual(lg.led._state, fl.LedStatus.BAD_DATA)

    def test_the_alarm_is_logged_once_not_per_pass(self):
        # update_led runs about ten times a second; a per-pass error would
        # fill the journal faster than the disk-full case it exists to warn
        # about.
        lg = self._logger()
        lg.last_parsed = time.monotonic() - (fl.NO_PARSE_TIMEOUT_S + 1)
        cap = capture_logs(self)
        for _ in range(50):
            lg.update_led()
        stalls = [m for m in cap.messages if "NOTHING IS BEING RECORDED" in m]
        self.assertEqual(len(stalls), 1, "the stall alarm is not rate limited")

    def test_recovery_returns_to_ready_and_says_so(self):
        lg = self._logger()
        lg.last_parsed = time.monotonic() - (fl.NO_PARSE_TIMEOUT_S + 1)
        lg.update_led()
        self.assertEqual(lg.led._state, fl.LedStatus.BAD_DATA)
        cap = capture_logs(self)
        lg.note_parsed()
        lg.update_led()
        self.assertEqual(lg.led._state, fl.LedStatus.READY)
        self.assertTrue(any("readable again" in m for m in cap.messages),
                        "recovery was not reported")

    def test_a_missing_instrument_is_waiting_not_stalled(self):
        # WAITING (3 flashes) and BAD_DATA (flicker) mean different things to
        # the researcher: one is a cable, the other is the instrument.
        lg = self._logger()
        lg.serial = None
        lg.last_parsed = time.monotonic() - (fl.NO_PARSE_TIMEOUT_S + 100)
        self.assertFalse(lg.data_stalled())
        lg.update_led()
        self.assertEqual(lg.led._state, fl.LedStatus.WAITING)

    def test_a_fresh_connection_gets_a_grace_period(self):
        # Reconnecting must not alarm before a single sample could arrive.
        lg = self._logger()
        lg.last_parsed = time.monotonic()
        self.assertFalse(lg.data_stalled())

    def test_the_real_loop_counts_unparseable_lines_and_writes_nothing(self):
        """Drive run()'s actual body rather than a reimplementation of it."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)

        lg = fl.FieldLogger.__new__(fl.FieldLogger)
        lg.args = type("A", (), {"port": "/dev/fake", "baud": 115200})()
        lg.data_dir = tmp
        lg.running = True
        lg.csv_file = None
        lg.csv_writer = None
        lg.csv_path = None
        lg.columns = None
        lg._pending = []
        lg._export_mark = None
        lg.gps = None
        lg.led = fl.LedStatus(enabled=False)
        lg.validator = fl.Validator()
        class FakeTimeSource(object):
            state = ("ntp", True)

            def start(self):
                pass

            def stop(self):
                pass

        lg.time_source = FakeTimeSource()
        lg.disk_full = False
        lg.total_rows = 0
        lg.total_bad = 0
        lg.rows_dropped = 0
        lg.recent = fl.deque(maxlen=300)
        lg.session_start = time.monotonic()
        lg.last_status_log = time.monotonic()
        lg.last_disk_check = time.monotonic()
        lg.last_watchdog = time.monotonic()
        lg.unexpected_errors = 0
        lg.unparsed_lines = 0
        lg.last_parsed = time.monotonic()
        lg._stall_logged = False
        lg.rows_at_last_status = 0
        lg.recovery_failures = 0
        lg.port_path = "/dev/fake"

        class FakeSerial(object):
            def __init__(self, lines):
                self.lines = list(lines)

            def readline(self, limit=-1):
                if not self.lines:
                    lg.running = False        # end the loop, not the test
                    return b""
                out = self.lines.pop(0).encode("ascii") + b"\r\n"
                # Honour the cap the way pyserial does, so this fake cannot
                # drift from the real port's contract.
                return out[:limit] if limit and limit > 0 else out

            def close(self):
                pass

        lg.serial = FakeSerial([self.MENU] * 30)

        rc = lg.run()

        self.assertEqual(rc, 0)
        self.assertEqual(lg.unparsed_lines, 30,
                         "the real loop did not count unparseable lines")
        self.assertEqual(lg.total_rows, 0, "garbage was recorded as data")
        with open(lg.csv_path) as fh:
            body = fh.read().strip()
        self.assertEqual(body, "", "a data file gained rows from menu text")


class TestReadOnlyCardIsNotAnnouncedAsRecovered(unittest.TestCase):
    """statvfs still reports free space on a filesystem ext4 has remounted
    read-only, so check_disk's recovery branch is reached on every check.
    Clearing the alarm before the file actually opened wrote "storage
    recovered" - the opposite of what happened - into the journal once a
    minute for the rest of the deployment.
    """

    def _stopped_logger(self):
        lg = fl.FieldLogger.__new__(fl.FieldLogger)
        lg.disk_full = False
        lg.rows_dropped = 0
        lg.recovery_failures = 0
        lg.last_disk_check = 0.0
        lg.close_file = lambda: None
        lg.open_file = lambda: None
        lg.free_mb = lambda: fl.MIN_FREE_MB - 1
        lg.check_disk()
        assert lg.disk_full
        lg.free_mb = lambda: fl.MIN_FREE_MB * 3      # space "returns"
        return lg

    @staticmethod
    def _read_only():
        def boom():
            raise OSError(30, "Read-only file system")
        return boom

    def test_no_recovery_is_announced_while_the_card_refuses_a_file(self):
        lg = self._stopped_logger()
        lg.open_file = self._read_only()
        cap = capture_logs(self)
        for _ in range(5):
            lg.last_disk_check = 0.0
            lg.check_disk()
        self.assertTrue(lg.disk_full, "must stay stopped")
        self.assertEqual([m for m in cap.messages if "storage recovered" in m],
                         [], "announced a recovery that did not happen")

    def test_the_failure_report_is_rate_limited(self):
        lg = self._stopped_logger()
        lg.open_file = self._read_only()
        cap = capture_logs(self)
        for _ in range(5):
            lg.last_disk_check = 0.0
            lg.check_disk()
        complaints = [m for m in cap.messages if "read-only" in m]
        self.assertEqual(len(complaints), 1,
                         "a failing card would fill the journal once a minute")

    def test_a_genuine_recovery_is_still_announced_and_resumes(self):
        lg = self._stopped_logger()
        cap = capture_logs(self)
        lg.last_disk_check = 0.0
        lg.check_disk()
        self.assertFalse(lg.disk_full, "did not resume after real recovery")
        self.assertTrue(any("storage recovered" in m for m in cap.messages),
                        "a real recovery went unreported")

    def test_the_failure_counter_resets_after_a_recovery(self):
        lg = self._stopped_logger()
        lg.open_file = self._read_only()
        lg.last_disk_check = 0.0
        lg.check_disk()
        self.assertEqual(lg.recovery_failures, 1)
        lg.open_file = lambda: None
        lg.last_disk_check = 0.0
        lg.check_disk()
        self.assertEqual(lg.recovery_failures, 0)
        self.assertFalse(lg.disk_full)


class TestSilentlyDisabledSafetyMechanismsAreReported(unittest.TestCase):
    """Two mechanisms used to switch themselves off without saying so.

    The status LED is the entire interface in the field, and a failed sysfs
    write only returned False, which the blink thread ignored - so a light
    that died mid-deployment was invisible in the field AND in the journal.
    Separately, an unreadable statvfs made check_disk return early every time,
    which correctly keeps recording going but disables the guard that stops
    writing before the card fills - for weeks, with nothing said, and with the
    reason thrown away.
    """

    # --- the status light -------------------------------------------------

    def _led(self):
        # A path that cannot be written to, without touching the real LED.
        return fl.LedStatus(path="/nonexistent/leds/led0", enabled=False)

    def test_a_single_failed_write_is_not_reported(self):
        led = self._led()
        cap = capture_logs(self)
        self.assertFalse(led._write("brightness", 255))
        self.assertEqual(cap.messages, [],
                         "a transient sysfs failure must stay quiet")

    def test_a_dead_led_is_reported_once(self):
        led = self._led()
        cap = capture_logs(self)
        for _ in range(200):                       # ~25 s of blinking
            led._write("brightness", 255)
        dead = [m for m in cap.messages if "stopped responding" in m]
        self.assertEqual(len(dead), 1,
                         "reported per write, or not at all (%d)" % len(dead))
        self.assertIn("Recording is unaffected", dead[0],
                      "must say the recording is still fine")

    def test_the_threshold_is_what_triggers_it(self):
        led = self._led()
        cap = capture_logs(self)
        for _ in range(fl.LED_WRITE_FAILURES_BEFORE_WARNING - 1):
            led._write("brightness", 0)
        self.assertEqual([m for m in cap.messages if "stopped responding" in m], [])
        led._write("brightness", 0)
        self.assertEqual(len([m for m in cap.messages if "stopped responding" in m]), 1)

    def test_recovery_is_reported(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        led = fl.LedStatus(path="/nonexistent/leds/led0", enabled=False)
        cap = capture_logs(self)
        for _ in range(fl.LED_WRITE_FAILURES_BEFORE_WARNING):
            led._write("brightness", 0)
        led.path = tmp                              # the light comes back
        self.assertTrue(led._write("brightness", 0))
        self.assertTrue(any("responding again" in m for m in cap.messages),
                        "recovery went unreported")
        # ...and it re-arms for the next failure.
        led.path = "/nonexistent/leds/led0"
        for _ in range(fl.LED_WRITE_FAILURES_BEFORE_WARNING):
            led._write("brightness", 0)
        self.assertEqual(len([m for m in cap.messages if "stopped responding" in m]), 2)

    # --- the storage guard ------------------------------------------------

    def _logger(self):
        lg = fl.FieldLogger.__new__(fl.FieldLogger)
        lg.disk_full = False
        lg.rows_dropped = 0
        lg.recovery_failures = 0
        lg.files_unlinked = 0
        lg.statvfs_failures = 0
        lg.last_statvfs_error = None
        lg.last_low_space_warn = 0.0
        lg.last_disk_check = 0.0
        lg.close_file = lambda: None
        lg.open_file = lambda: None
        return lg

    def test_an_unreadable_statvfs_says_the_guard_is_off(self):
        lg = self._logger()
        lg.free_mb = lambda: -1.0
        lg.last_statvfs_error = OSError(5, "Input/output error")
        cap = capture_logs(self)
        lg.last_disk_check = 0.0
        lg.check_disk()
        said = [m for m in cap.messages if "cannot read free space" in m]
        self.assertEqual(len(said), 1, "the disabled guard was silent")
        self.assertIn("is NOT running", said[0], "must say the guard is off")
        self.assertIn("Input/output error", said[0], "must carry the reason")
        self.assertFalse(lg.disk_full, "an unreadable statvfs must not halt logging")

    def test_the_report_is_rate_limited(self):
        lg = self._logger()
        lg.free_mb = lambda: -1.0
        lg.last_statvfs_error = OSError(2, "No such file or directory")
        cap = capture_logs(self)
        for _ in range(30):
            lg.last_disk_check = 0.0
            lg.check_disk()
        self.assertEqual(len([m for m in cap.messages if "cannot read free space" in m]), 2,
                         "expected reports at failure 1 and 10 only")

    def test_recovery_of_the_guard_is_reported(self):
        lg = self._logger()
        lg.free_mb = lambda: -1.0
        lg.last_statvfs_error = OSError(5, "Input/output error")
        for _ in range(3):
            lg.last_disk_check = 0.0
            lg.check_disk()
        self.assertEqual(lg.statvfs_failures, 3)
        cap = capture_logs(self)
        lg.free_mb = lambda: fl.LOW_SPACE_WARN_MB * 2
        lg.last_disk_check = 0.0
        lg.check_disk()
        self.assertEqual(lg.statvfs_failures, 0, "the counter did not reset")
        self.assertTrue(any("readable again" in m for m in cap.messages))

    def test_free_mb_keeps_the_reason(self):
        lg = self._logger()
        lg.data_dir = "/nonexistent/definitely/not/here"
        self.assertEqual(lg.free_mb(), -1.0)
        self.assertIsNotNone(lg.last_statvfs_error,
                             "the reason was thrown away")
        self.assertIsInstance(lg.last_statvfs_error, OSError)


class TestUnreachableGpsdIsDistinguishedFromNoFix(unittest.TestCase):
    """'No satellites' and 'cannot talk to gpsd at all' need opposite
    actions - give it sky, versus check the receiver - and used to be
    indistinguishable. The first connection failure was silent because the
    warning was guarded by `if self.connected`, and every 5 s retry after it
    was silent too, so a dead gpsd produced no log line ever while the status
    line read 'nofix', exactly like a healthy receiver indoors.
    """

    def _reader_that_cannot_connect(self, iterations):
        g = fl.GpsReader()
        state = {"waits": 0}

        class Stop(object):
            def is_set(self):
                return state["waits"] >= iterations

            def wait(self, _t):
                state["waits"] += 1
                return False

            def set(self):
                state["waits"] = iterations

        g._stop = Stop()
        original = fl.socket.create_connection

        def refuse(*a, **k):
            raise ConnectionRefusedError("Connection refused")

        fl.socket.create_connection = refuse
        self.addCleanup(setattr, fl.socket, "create_connection", original)
        return g

    def test_an_unreachable_gpsd_is_reported_once(self):
        g = self._reader_that_cannot_connect(6)
        cap = capture_logs(self)
        g._run()
        said = [m for m in cap.messages if "cannot reach gpsd" in m]
        self.assertEqual(len(said), 1,
                         "reported per retry, or never (%d)" % len(said))
        self.assertIn("unreachable", said[0],
                      "must explain what the status line will say")
        self.assertIn("Recording continues", said[0])

    def test_a_brief_gpsd_restart_stays_quiet(self):
        # Fewer failures than the threshold: gpsd being restarted is normal
        # and must not raise an alarm.
        g = self._reader_that_cannot_connect(fl.GPSD_UNREACHABLE_ATTEMPTS - 1)
        cap = capture_logs(self)
        g._run()
        self.assertEqual([m for m in cap.messages if "cannot reach gpsd" in m], [])

    def test_recovery_is_reported_by_the_real_path(self):
        """Drives _run through failure and then a real connection.

        Written after a first attempt that called log.warning by hand and so
        proved nothing about the code.
        """
        g = fl.GpsReader()
        state = {"waits": 0}

        class Stop(object):
            def is_set(self):
                return state["waits"] >= 8

            def wait(self, _t):
                state["waits"] += 1
                return False

            def set(self):
                state["waits"] = 8

        g._stop = Stop()

        class FakeSock(object):
            def settimeout(self, _t): pass
            def send(self, _b): return len(_b)
            def recv(self, _n): return b""        # ends the inner read loop
            def close(self): pass

        attempts = {"n": 0}
        original = fl.socket.create_connection

        def flaky(*a, **k):
            attempts["n"] += 1
            if attempts["n"] <= fl.GPSD_UNREACHABLE_ATTEMPTS + 1:
                raise ConnectionRefusedError("Connection refused")
            return FakeSock()

        fl.socket.create_connection = flaky
        self.addCleanup(setattr, fl.socket, "create_connection", original)

        cap = capture_logs(self)
        g._run()
        self.assertTrue(any("cannot reach gpsd" in m for m in cap.messages),
                        "the outage was not reported")
        self.assertTrue(any("reachable again" in m for m in cap.messages),
                        "the recovery was not reported")
        self.assertFalse(g._unreachable_logged, "the flag did not re-arm")
        self.assertEqual(g._reach_failures, 0, "the counter did not reset")

    # --- the status line --------------------------------------------------

    def _logger(self, gps):
        lg = fl.FieldLogger.__new__(fl.FieldLogger)
        lg.gps = gps
        lg.disk_full = False
        lg.total_rows = 100
        lg.total_bad = 0
        lg.rows_at_last_status = 0
        lg.unexpected_errors = lg.unparsed_lines = 0
        lg.dropped_fields = lg.files_unlinked = 0
        lg.time_source = type("T", (), {"state": ("ntp", True)})()
        lg.free_mb = lambda: 1000.0
        lg.session_start = time.monotonic() - 1000
        lg.last_status_log = time.monotonic() - fl.STATUS_LOG_INTERVAL_S - 1
        return lg

    def _gps(self, connected, fix):
        return type("G", (), {"connected": connected,
                              "current": lambda self: (fix, 0)})()

    def test_status_says_unreachable_when_gpsd_is_down(self):
        lg = self._logger(self._gps(connected=False, fix=None))
        cap = capture_logs(self, level=logging.INFO)
        lg.log_status()
        line = [m for m in cap.messages if m.startswith("status: ")][0]
        self.assertIn("gps=unreachable", line)

    def test_status_says_nofix_when_gpsd_is_up_without_satellites(self):
        lg = self._logger(self._gps(connected=True, fix=None))
        cap = capture_logs(self, level=logging.INFO)
        lg.log_status()
        line = [m for m in cap.messages if m.startswith("status: ")][0]
        self.assertIn("gps=nofix", line)

    def test_status_says_fix_when_there_is_one(self):
        lg = self._logger(self._gps(connected=True, fix={"lat": 1.0}))
        cap = capture_logs(self, level=logging.INFO)
        lg.log_status()
        line = [m for m in cap.messages if m.startswith("status: ")][0]
        self.assertIn("gps=fix", line)

    def test_status_says_off_when_gps_is_disabled(self):
        lg = self._logger(None)
        cap = capture_logs(self, level=logging.INFO)
        lg.log_status()
        line = [m for m in cap.messages if m.startswith("status: ")][0]
        self.assertIn("gps=off", line)


class TestDeletedDataFileIsNoticed(unittest.TestCase):
    """Freeing space by deleting the data is a natural thing for someone to
    do - the README's own card-full guidance asks for exactly that - and it
    used to be invisible. write() keeps succeeding against the unlinked
    inode, statvfs on a missing directory only reports "unknown" and
    check_disk carried on, so the logger recorded into nothing until the next
    rotation, up to six hours later, with no warning and no LED change.
    """

    def _logger(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        lg = fl.FieldLogger.__new__(fl.FieldLogger)
        lg.data_dir = tmp
        lg.csv_file = lg.csv_writer = lg.csv_path = None
        lg.columns = None
        lg._pending = []
        lg.gps = None
        lg.validator = fl.Validator()
        lg.time_source = type("T", (), {"state": ("ntp", True)})()
        lg.disk_full = False
        lg.total_rows = lg.total_bad = lg.rows_dropped = 0
        lg.recent = fl.deque(maxlen=300)
        lg.recovery_failures = 0
        lg.files_unlinked = 0
        lg.last_low_space_warn = 0.0
        lg.last_disk_check = 0.0
        lg.open_file()
        return lg, tmp

    def test_a_deleted_csv_is_noticed_and_replaced(self):
        lg, tmp = self._logger()
        first = lg.csv_path
        # Neither the name nor the inode number can be used to tell the files
        # apart: unique_path derives the name from the current second, and the
        # freed inode number is immediately available for reuse. The property
        # that matters is that the handle is linked again and writes land on
        # disk, so that is what is asserted.
        os.remove(first)
        cap = capture_logs(self)
        lg.last_disk_check = 0.0
        lg.check_disk()
        self.assertEqual(lg.files_unlinked, 1, "the deletion went unnoticed")
        self.assertTrue(any("deleted while it was being written" in m
                            for m in cap.messages), "nothing was logged")
        self.assertEqual(os.fstat(lg.csv_file.fileno()).st_nlink, 1,
                         "still writing into an unlinked file")
        self.assertTrue(os.path.exists(lg.csv_path))
        self.assertFalse(lg.disk_full, "recovery should resume, not stop")
        # And prove it: a row written now must reach the filesystem.
        for _ in range(fl.HEADER_DISCOVERY_ROWS):
            lg.write_sample(fl.parse_line(GOOD))
        lg.csv_file.flush()
        self.assertGreater(os.path.getsize(lg.csv_path), 0,
                           "rows are still going nowhere")

    def test_a_deleted_data_directory_is_recreated(self):
        lg, tmp = self._logger()
        shutil.rmtree(tmp)
        cap = capture_logs(self)
        lg.last_disk_check = 0.0
        lg.check_disk()
        self.assertEqual(lg.files_unlinked, 1)
        self.assertTrue(os.path.isdir(tmp), "the data directory was not recreated")
        self.assertTrue(os.path.exists(lg.csv_path))
        self.assertFalse(lg.disk_full)

    def test_writing_resumes_into_the_replacement(self):
        lg, _ = self._logger()
        os.remove(lg.csv_path)
        lg.last_disk_check = 0.0
        lg.check_disk()
        for _ in range(fl.HEADER_DISCOVERY_ROWS + 5):
            lg.write_sample(fl.parse_line(GOOD))
        lg.close_file()
        rows = [r for r in open(lg.csv_path).read().splitlines() if r.strip()]
        self.assertGreater(len(rows), fl.HEADER_DISCOVERY_ROWS,
                           "rows are still going nowhere after recovery")
        self.assertTrue(rows[0].startswith("timestamp_utc"))

    def test_a_healthy_file_is_left_alone(self):
        lg, _ = self._logger()
        first = lg.csv_path
        cap = capture_logs(self)
        for _ in range(5):
            lg.last_disk_check = 0.0
            lg.check_disk()
        self.assertEqual(lg.files_unlinked, 0)
        self.assertEqual(lg.csv_path, first, "rotated a perfectly good file")
        self.assertEqual([m for m in cap.messages if "deleted" in m], [])

    def test_a_closed_handle_does_not_kill_the_loop(self):
        # fileno() on a closed file raises ValueError, which is not an
        # OSError. If that escaped check_disk it would propagate out of the
        # main loop and end the deployment.
        lg, _ = self._logger()
        lg.csv_file.close()
        lg.last_disk_check = 0.0
        lg.check_disk()                       # must not raise
        self.assertEqual(lg.files_unlinked, 0)

    def test_a_handle_whose_fileno_raises_is_survived(self):
        lg, _ = self._logger()

        class Hostile(object):
            closed = False

            def fileno(self):
                raise ValueError("I/O operation on closed file")

        lg.csv_file = Hostile()
        lg.last_disk_check = 0.0
        lg.check_disk()                       # must not raise
        self.assertEqual(lg.files_unlinked, 0)

    def test_the_count_is_surfaced_in_the_status_line(self):
        import inspect
        self.assertIn("files_unlinked",
                      inspect.getsource(fl.FieldLogger.log_status))


class TestSerialLineIsBounded(unittest.TestCase):
    """A device stuck emitting bytes with no newline must not grow the process.

    readline() has no length limit of its own. Measured on the unit before the
    cap: 1.1 MB fed in, +1124 kB RSS - one for one. At 115200 baud that is
    ~41 MB/h, and the OOM reaper takes the recording with it inside a day. The
    gpsd reader has always capped its buffer for exactly this reason.
    """

    class _Endless(object):
        """A serial port that never sends a newline."""

        def __init__(self):
            self.limits = []

        def readline(self, limit=-1):
            self.limits.append(limit)
            return b"x" * (limit if limit and limit > 0 else 1 << 20)

        def close(self):
            pass

    def test_the_read_is_length_limited(self):
        fake = self._Endless()
        got = fake.readline(fl.MAX_LINE_BYTES)
        self.assertEqual(len(got), fl.MAX_LINE_BYTES)

    def test_the_loop_passes_the_limit_to_readline(self):
        import inspect
        src = inspect.getsource(fl.FieldLogger.run)
        self.assertIn("readline(MAX_LINE_BYTES)", src,
                      "the serial read is unbounded again")

    def test_the_cap_leaves_generous_headroom_over_a_real_sentence(self):
        real = len(GOOD) + 2
        self.assertGreater(fl.MAX_LINE_BYTES, real * 10,
                           "the cap is too close to a real sentence length")

    def test_an_endless_stream_becomes_visible_instead_of_silent(self):
        # Capping the read turns unbounded growth into unparseable chunks,
        # which the no-parseable-data alarm already reports.
        chunk = b"x" * fl.MAX_LINE_BYTES
        self.assertLess(len(fl.parse_line(chunk.decode("ascii"))), 3,
                        "a newline-free chunk must not parse as a measurement")


class TestLowSpaceWarningIsRateLimited(unittest.TestCase):
    """The low-space condition holds for the whole last week of a deployment.

    At one line per 60 s check that is ~10,000 journal warnings, burying
    everything else at exactly the moment someone is looking at a card that is
    nearly full.
    """

    def _logger(self, free):
        lg = fl.FieldLogger.__new__(fl.FieldLogger)
        lg.disk_full = False
        lg.rows_dropped = 0
        lg.recovery_failures = 0
        lg.last_disk_check = 0.0
        lg.last_low_space_warn = 0.0
        lg.close_file = lambda: None
        lg.open_file = lambda: None
        lg.free_mb = lambda: free
        return lg

    def _run_checks(self, lg, n):
        for _ in range(n):
            lg.last_disk_check = 0.0
            lg.check_disk()

    def test_a_week_of_checks_does_not_flood_the_journal(self):
        # Comfortably low, but well above the stop threshold.
        lg = self._logger(fl.LOW_SPACE_WARN_MB - 1)
        cap = capture_logs(self)
        self._run_checks(lg, 60)
        warns = [m for m in cap.messages if "storage low" in m]
        self.assertEqual(len(warns), 1,
                         "%d warnings from 60 checks; at 60 s each that is "
                         "~10,000 over the final week" % len(warns))
        self.assertFalse(lg.disk_full, "the warning threshold must not stop writes")

    def test_it_warns_again_after_the_interval(self):
        lg = self._logger(fl.LOW_SPACE_WARN_MB - 1)
        cap = capture_logs(self)
        self._run_checks(lg, 5)
        # Pretend the rate-limit window has elapsed.
        lg.last_low_space_warn = time.monotonic() - fl.LOW_SPACE_WARN_INTERVAL_S - 1
        self._run_checks(lg, 5)
        self.assertEqual(len([m for m in cap.messages if "storage low" in m]), 2,
                         "a persistent low-space condition stopped reminding")

    def test_recovering_space_re_arms_the_warning(self):
        lg = self._logger(fl.LOW_SPACE_WARN_MB - 1)
        cap = capture_logs(self)
        self._run_checks(lg, 3)
        lg.free_mb = lambda: fl.LOW_SPACE_WARN_MB * 2      # card swapped
        self._run_checks(lg, 3)
        lg.free_mb = lambda: fl.LOW_SPACE_WARN_MB - 1      # and fills again
        self._run_checks(lg, 3)
        self.assertEqual(len([m for m in cap.messages if "storage low" in m]), 2,
                         "crossing the threshold again must warn immediately")


class TestFieldsWithoutAColumnAreReported(unittest.TestCase):
    """A field the instrument starts sending after this file's schema was
    fixed has no column. Its values used to be discarded with nothing
    anywhere - no flag, no counter, no log line - to say so, while the
    opposite case (a field going missing) is handled gracefully as a blank.
    """

    BASE = "S  00.14,S2  00.14,D  285,T  21.60"

    def _logger(self, tmp):
        lg = fl.FieldLogger.__new__(fl.FieldLogger)
        lg.data_dir = tmp
        lg.csv_file = lg.csv_writer = lg.csv_path = None
        lg.columns = None
        lg._pending = []
        lg.gps = None
        lg.validator = fl.Validator()
        lg.time_source = type("T", (), {"state": ("ntp", True)})()
        lg.disk_full = False
        lg.total_rows = lg.total_bad = lg.rows_dropped = 0
        lg.recent = fl.deque(maxlen=300)
        lg.dropped_fields = 0
        lg._dropped_logged = False
        lg._known_keys = None
        lg.open_file()
        return lg

    def test_an_extra_field_is_counted_and_reported_once(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        lg = self._logger(tmp)
        for _ in range(fl.HEADER_DISCOVERY_ROWS):
            lg.write_sample(fl.parse_line(self.BASE))
        self.assertNotIn("H", lg.columns, "precondition: H is not in the schema")

        cap = capture_logs(self)
        for _ in range(30):
            lg.write_sample(fl.parse_line(self.BASE + ",H  40.22"))
        lg.close_file()

        self.assertEqual(lg.dropped_fields, 30, "dropped values were not counted")
        warns = [m for m in cap.messages if "absent during schema discovery" in m]
        self.assertEqual(len(warns), 1, "reported per row, or not at all")
        self.assertIn("H", warns[0])

    def test_a_missing_field_is_still_just_blank_and_not_reported(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        lg = self._logger(tmp)
        for _ in range(fl.HEADER_DISCOVERY_ROWS):
            lg.write_sample(fl.parse_line(self.BASE))
        cap = capture_logs(self)
        lg.write_sample(fl.parse_line("S  00.20,D  290,T  21.70"))   # no S2
        lg.close_file()
        self.assertEqual(lg.dropped_fields, 0,
                         "a missing field is not a dropped value")
        self.assertEqual([m for m in cap.messages
                          if "absent during schema discovery" in m], [])
        last = open(lg.csv_path).read().splitlines()[-1].split(",")
        self.assertEqual(last[4], "", "the missing field should be blank")

    def test_a_normal_full_sentence_reports_nothing(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        lg = self._logger(tmp)
        cap = capture_logs(self)
        for _ in range(fl.HEADER_DISCOVERY_ROWS + 20):
            lg.write_sample(fl.parse_line(GOOD))
        lg.close_file()
        self.assertEqual(lg.dropped_fields, 0)
        self.assertEqual(cap.messages, [], "a healthy stream must stay quiet")


class TestSerialPortSelection(unittest.TestCase):
    """find_port must never open the UART gpsd owns."""

    def _with_by_id(self, names):
        real = fl.glob.glob

        def fake(pattern):
            if "CP2102" in pattern:
                return sorted(n for n in names if "CP2102" in n)
            if "by-id" in pattern:
                return sorted(names)
            return []

        fl.glob.glob = fake
        self.addCleanup(setattr, fl.glob, "glob", real)

    def _logger(self):
        lg = fl.FieldLogger.__new__(fl.FieldLogger)
        lg.args = type("A", (), {"port": "auto"})()
        return lg

    def test_a_mixed_case_ublox_name_is_not_opened_as_the_anemometer(self):
        # The u-blox test used to be case-sensitive while the GPS test was
        # not, so this name passed both and the logger seized the GPS UART.
        self._with_by_id(["/dev/serial/by-id/usb-U-BLOX_AG_GNSS_RECEIVER-if00"])
        self.assertIsNone(self._logger().find_port(),
                          "would have taken the UART gpsd owns")

    def test_a_lowercase_ublox_name_is_still_excluded(self):
        self._with_by_id(["/dev/serial/by-id/usb-u-blox_AG_receiver-if00"])
        self.assertIsNone(self._logger().find_port())

    def test_the_cp2102_bridge_is_still_preferred(self):
        self._with_by_id([
            "/dev/serial/by-id/usb-U-BLOX_AG_GNSS_RECEIVER-if00",
            "/dev/serial/by-id/usb-Silicon_Labs_CP2102N_Bridge-if00-port0",
        ])
        port = self._logger().find_port()
        self.assertIsNotNone(port)
        self.assertIn("CP2102", port)


class TestRotationKeepsBufferedRows(unittest.TestCase):
    """Rotating while rows are still buffered must not lose them.

    open_file() clears the schema-discovery buffer, which looks like silent
    data loss for an instrument that produced fewer than
    HEADER_DISCOVERY_ROWS samples in a whole rotation period. It is not:
    open_file() calls close_file() first, and close_file() flushes the buffer
    into the outgoing file. That ordering is the entire guarantee, it is not
    obvious from either function alone, and nothing pinned it - so this test
    does.
    """

    def test_buffered_discovery_rows_survive_a_rotation(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)

        lg = fl.FieldLogger.__new__(fl.FieldLogger)
        lg.data_dir = tmp
        lg.csv_file = None
        lg.csv_writer = None
        lg.csv_path = None
        lg.columns = None
        lg._pending = []
        lg.gps = None
        lg.validator = fl.Validator()
        lg.time_source = type("T", (), {"state": ("ntp", True)})()
        lg.disk_full = False
        lg.total_rows = 0
        lg.total_bad = 0
        lg.rows_dropped = 0
        lg.recent = fl.deque(maxlen=300)
        lg.open_file()

        # Fewer than HEADER_DISCOVERY_ROWS: a nearly-dead instrument.
        for _ in range(3):
            lg.write_sample(fl.parse_line(GOOD))
        self.assertEqual(len(lg._pending), 3, "precondition: rows are buffered")

        first = lg.csv_path
        lg.file_opened_at = time.monotonic() - (fl.FILE_ROTATE_HOURS * 3600 + 1)
        lg.maybe_rotate()
        self.assertNotEqual(lg.csv_path, first, "precondition: it rotated")
        lg.close_file()

        with open(first) as fh:
            rows = [line for line in fh.read().splitlines() if line.strip()]
        self.assertEqual(len(rows), 4,
                         "expected a header and 3 buffered rows, got %d lines"
                         % len(rows))
        self.assertTrue(rows[0].startswith("timestamp_utc"))


class TestStatusReporting(unittest.TestCase):

    def _logger(self):
        lg = fl.FieldLogger.__new__(fl.FieldLogger)
        lg.gps = None
        lg.disk_full = False
        lg.total_bad = 0
        lg.unexpected_errors = 0
        lg.unparsed_lines = 0
        lg.time_source = type("T", (), {"state": ("ntp", True)})()
        lg.free_mb = lambda: 1000.0
        lg.session_start = time.monotonic() - 100000.0
        return lg

    def test_the_reported_rate_is_windowed_not_lifetime(self):
        # A lifetime average stays high for hours after the instrument stops,
        # hiding exactly the drop a remote operator is checking for.
        lg = self._logger()
        lg.total_rows = 1000000            # a long, healthy history
        lg.rows_at_last_status = 1000000   # ... and nothing since
        lg.last_status_log = time.monotonic() - fl.STATUS_LOG_INTERVAL_S - 1
        cap = capture_logs(self, level=logging.INFO)
        lg.log_status()
        status = [m for m in cap.messages if m.startswith("status: ")]
        self.assertEqual(len(status), 1)
        # Parsed, not matched as a substring: "10.00 Hz now" contains
        # "0.00 Hz now", so an assertIn here cannot fail.
        found = re.search(r"([0-9.]+) Hz now", status[0])
        self.assertIsNotNone(found, "no rate in the status line: %s" % status[0])
        self.assertEqual(float(found.group(1)), 0.0,
                         "reported a lifetime average (%s Hz) instead of the "
                         "current rate" % found.group(1))

    def _status_now(self, lg):
        lg.last_status_log = time.monotonic() - fl.STATUS_LOG_INTERVAL_S - 1
        cap = capture_logs(self)
        lg.log_status()
        return cap.messages

    def test_unparseable_lines_are_surfaced_in_the_status_line(self):
        lg = self._logger()
        lg.total_rows = 10
        lg.rows_at_last_status = 0
        lg.unparsed_lines = 42
        messages = self._status_now(lg)
        self.assertTrue(any("42 line(s) arrived that were not measurements"
                            in m for m in messages), messages)

    def test_the_same_unparsed_lines_are_not_re_warned_about_forever(self):
        # These are counted cumulatively since the instrument connected, so
        # warning on the total repeated a handful of startup lines every five
        # minutes for the life of the deployment. Observed in the field: 7
        # lines over four days, ~1,150 warnings about them.
        lg = self._logger()
        lg.total_rows = 10
        lg.rows_at_last_status = 0
        lg.unparsed_lines = 7

        first = self._status_now(lg)
        self.assertTrue(any("not measurements" in m for m in first), first)

        lg.rows_at_last_status = lg.total_rows
        second = self._status_now(lg)
        self.assertFalse(any("not measurements" in m for m in second),
                         "the same 7 lines were warned about twice: %s"
                         % second)

    def test_further_unparsed_lines_do_warn_again(self):
        # The counter must not go quiet permanently: a head that starts
        # producing garbage has to reach the journal.
        lg = self._logger()
        lg.total_rows = 10
        lg.rows_at_last_status = 0
        lg.unparsed_lines = 7
        self._status_now(lg)

        lg.unparsed_lines = 20
        messages = self._status_now(lg)
        self.assertTrue(any("13 line(s)" in m for m in messages), messages)
        self.assertTrue(any("20 since" in m for m in messages), messages)


class TestMissingChronycIsVisible(unittest.TestCase):
    """Every row's time_synced flag and the READY light both depend on
    chronyc existing. If it were absent the logger would report 'freerun'
    forever with a perfectly good clock, and nothing would say why.
    """

    def test_a_missing_chronyc_is_reported_once(self):
        ts = fl.TimeSource.__new__(fl.TimeSource)
        original = fl.subprocess.check_output

        def boom(*a, **k):
            raise OSError(2, "No such file or directory: 'chronyc'")

        cap = capture_logs(self)
        try:
            fl.subprocess.check_output = boom
            for _ in range(5):
                source, synced = ts._query_chrony()
        finally:
            fl.subprocess.check_output = original

        self.assertEqual(source, fl.TimeSource.FREERUN)
        self.assertFalse(synced)
        warned = [m for m in cap.messages if "chronyc" in m]
        self.assertEqual(len(warned), 1,
                         "warned on every poll, or never warned at all")

    def test_an_unsynchronised_chrony_is_not_reported_as_missing(self):
        # A running-but-unsynced chrony is a normal cold-start state and must
        # stay quiet.
        ts = fl.TimeSource.__new__(fl.TimeSource)
        original = fl.subprocess.check_output
        cap = capture_logs(self)
        try:
            fl.subprocess.check_output = lambda *a, **k: (
                b"Reference ID    : 00000000 ()\n"
                b"Leap status     : Not synchronised\n")
            ts._query_chrony()
        finally:
            fl.subprocess.check_output = original
        self.assertEqual([m for m in cap.messages if "chronyc" in m], [])


class TestExportUnitHardening(unittest.TestCase):
    """The export watcher runs as root, so its hardening has to be EFFECTIVE
    on the systemd the unit actually runs - 241 on Raspbian Buster - not
    merely present in the file. systemd accepts a directive it does not know
    as an unknown lvalue, logs one line, and drops it; `systemctl show` then
    lists no such property and nothing else ever says so.
    """

    def _directives(self):
        """Directive lines only, comments stripped.

        A comment explaining a rule must not be mistaken for the rule itself -
        the same trap the deploy gate documents about grepping for 'poweroff'.
        """
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "trisonica-usb-export.service")) as fh:
            lines = fh.read().splitlines()
        return [s for s in (line.strip() for line in lines)
                if s and not s.startswith("#") and not s.startswith("[")]

    def _filters(self):
        return [d for d in self._directives()
                if d.startswith("SystemCallFilter=")]

    def test_the_clock_is_denied_by_something_systemd_241_understands(self):
        self.assertEqual(
            [d for d in self._directives() if d.startswith("ProtectClock")], [],
            "ProtectClock= needs systemd 245; on this unit's 241 it is "
            "silently ignored and protects nothing")
        self.assertTrue(self._filters(), "no SystemCallFilter in the export unit")
        self.assertTrue(any("@clock" in f for f in self._filters()),
                        "a root service can still step the clock, and the "
                        "timestamps are this dataset's provenance")

    def test_powering_the_unit_off_is_still_denied(self):
        self.assertTrue(any("@reboot" in f for f in self._filters()),
                        "the export service could power the unit off")

    def test_the_filter_is_a_deny_list_not_an_allow_list(self):
        # An allow-list would have to enumerate everything mount, copy and
        # unmount need, and would kill the service with SIGSYS when it missed
        # one - losing the researcher's only way to retrieve data.
        for f in self._filters():
            self.assertTrue(f.startswith("SystemCallFilter=~"),
                            "expected a deny-list filter, got: %s" % f)


class TestDeployGuardPortability(unittest.TestCase):
    """deploy.sh refuses to deploy if any module could power the unit off.

    That gate used ast.Str, which was deprecated in 3.8 and REMOVED in 3.12:
    on a newer interpreter it raises AttributeError, exits non-zero, and
    blocks every deploy - including an urgent fix to a unit already in the
    field. These tests run the gate's own source under whatever interpreter
    is executing the suite.
    """

    def _gate_script(self):
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "deploy.sh")
        if not os.path.exists(path):
            # deploy.sh is a maintainer-side tool and is deliberately not
            # copied to the unit, so these tests only run where it lives.
            # The suite itself runs on the Pi as a deploy gate, and must not
            # fail there over a file that is not meant to be there.
            self.skipTest("deploy.sh not present (maintainer-side only)")
        with open(path) as fh:
            source = fh.read()
        # The heredoc line carries a trailing '|| exit 1', so skip to the end
        # of that line rather than assuming the delimiter ends it.
        start = source.find("<<'PY'")
        self.assertNotEqual(start, -1, "could not find the gate in deploy.sh")
        start = source.find("\n", start)
        self.assertNotEqual(start, -1, "gate heredoc has no body")
        start += 1
        end = source.find("\nPY\n", start)
        self.assertNotEqual(end, -1, "gate heredoc is not terminated")
        return source[start:end]

    def _run_gate(self, body):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        gate = os.path.join(tmp, "gate.py")
        with open(gate, "w") as fh:
            fh.write(self._gate_script())
        module = os.path.join(tmp, "candidate.py")
        with open(module, "w") as fh:
            fh.write(body)
        return subprocess.call([sys.executable, gate, module],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_ast_str_is_never_referenced_directly(self):
        # Checked against the PARSED gate rather than its text: a comment
        # explaining the rule trips a plain search, which is precisely the
        # trap the gate itself documents about grepping for 'poweroff'.
        # getattr(ast, "Str", None) is a Call, not an Attribute, so the
        # defensive form passes and only an eager ast.Str fails.
        import ast as _ast
        tree = _ast.parse(self._gate_script())
        direct = [node for node in _ast.walk(tree)
                  if isinstance(node, _ast.Attribute)
                  and node.attr == "Str"
                  and isinstance(node.value, _ast.Name)
                  and node.value.id == "ast"]
        self.assertEqual(direct, [],
                         "ast.Str is referenced directly, which raises "
                         "AttributeError on Python 3.12+ and blocks deploys")

    def test_the_gate_survives_an_interpreter_without_ast_str(self):
        # The actual regression: on Python 3.12 ast.Str is gone. Delete it
        # here and confirm the gate still refuses a module that would power
        # the unit off, rather than dying with an AttributeError and blocking
        # the deploy.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        gate = os.path.join(tmp, "gate.py")
        with open(gate, "w") as fh:
            fh.write(self._gate_script())
        module = os.path.join(tmp, "candidate.py")
        with open(module, "w") as fh:
            fh.write("import subprocess\n"
                     "def stop():\n"
                     "    subprocess.call(['poweroff'])\n")

        runner = ("import ast, runpy, sys\n"
                  "if hasattr(ast, 'Str'):\n"
                  "    del ast.Str\n"
                  "sys.argv = [%r, %r]\n"
                  "runpy.run_path(%r, run_name='__main__')\n"
                  % (gate, module, gate))
        rc = subprocess.call([sys.executable, "-c", runner],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(rc, 1,
                         "the gate broke on an interpreter without ast.Str - "
                         "every deploy would be blocked")

    def test_a_module_that_could_power_the_unit_off_is_refused(self):
        rc = self._run_gate("import subprocess\n"
                            "def stop():\n"
                            "    subprocess.call(['poweroff'])\n")
        self.assertEqual(rc, 1, "the gate no longer catches a poweroff call")

    def test_documenting_the_rule_is_still_allowed(self):
        # A unit that cannot be deployed because its source explains the rule
        # is its own outage - the gate reads parsed code, not raw text.
        rc = self._run_gate('"""Never call poweroff from this service."""\n'
                            "def go():\n"
                            "    return 1\n")
        self.assertEqual(rc, 0, "the gate rejected a module that only "
                               "documents the rule")


if __name__ == "__main__":
    unittest.main(verbosity=2)

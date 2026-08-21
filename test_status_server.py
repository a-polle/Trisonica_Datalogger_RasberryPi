#!/usr/bin/env python3
"""Tests for the TriSonica status dashboard and data server.

The dashboard exists so that someone who cannot get onto the roof can decide,
from a browser, whether the station needs a maintenance appointment. That
makes a WRONG GREEN the worst defect this file can have, and most of what is
below is about the ways the page could claim everything is fine while nothing
is being recorded.

Run:  python3 test_status_server.py
"""

import http.client
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest

import trisonica_alert as al
import trisonica_status_server as ss


HEADER = ("timestamp_utc,time_source,time_synced,S,S2,D,U,V,W,T,H,P,"
          "PI,RO,MD,TD,flags,n_err,n_impl,n_spike,lat,lon,alt_m,"
          "gps_mode,gps_sats")

# A real row, laptop capture, no flags.
ROW = ("2026-08-20T04:29:26.708670Z,ntp,1,00.04,00.04,303,00.03,-00.02,"
       "00.02,21.15,44.19,1002.93,006.4,003.7,119,119.46,,0,0,0,,,,,0")

# The logger's 5-minutely journal line, exactly as it is formatted.
STATUS_LINE = ("2026-08-20 04:26:04,914 INFO status: 678169 rows, 10.00 Hz "
               "now, 0.00% bad, time=ntp(synced=True), gps=nofix sats=0, "
               "3614 MB free")


def healthy_status(data_age_s=1.0, status_age_s=30.0, **overrides):
    """A status dict for a unit that is recording normally.

    Ages are given relative to now so a test can move one of them and change
    nothing else.
    """
    now = time.time()
    status = {
        "system": {"hostname": "raspberrypi", "uptime": "up 1 day",
                   "timestamp_utc": "2026-08-20 04:29:24 UTC"},
        "services": {
            "trisonica-logger": "active",
            "trisonica-usb-export": "active",
            "gpsd": "active",
            "chrony": "active",
            "tailscaled": "active",
        },
        "disk": {"free_mb": 3613.4, "total_mb": 14203.5, "used_mb": 10590.1,
                 "free_pct": 25.4, "estimated_days": 29.6},
        "logger": {
            "status_line": STATUS_LINE,
            "status_time_epoch": now - status_age_s,
            "total_rows": 678169,
            "sample_rate_hz": 10.0,
            "bad_pct": 0.0,
            "gps_state": "nofix",
            "gps_sats": 0,
            "time_source": "ntp",
            "time_synced": True,
            "recent_log": [STATUS_LINE],
        },
        "recording": {
            "newest_file": "TrisonicaData_2026-08-20_033541Z.csv",
            "newest_mtime_epoch": now - data_age_s,
            "newest_size_bytes": 4185181,
        },
        "files": [{"name": "TrisonicaData_2026-08-20_033541Z.csv",
                   "size_bytes": 4185181,
                   "mtime_epoch": now - data_age_s,
                   "mtime_utc": "2026-08-20 04:31 UTC"}],
    }
    for section, values in overrides.items():
        status.setdefault(section, {}).update(values)
    return status


class TestTheAnemometerFallingOff(unittest.TestCase):
    """The failure this dashboard exists to catch.

    Unplug the anemometer and the logger service stays 'active' - that is
    deliberate, it waits and reconnects - but log_status() is only reached on
    the branch where a serial port is open, so the last journal line keeps
    saying '10.00 Hz' for as long as anyone cares to look. Every check that
    asks a component how it is doing answers 'fine'.
    """

    def test_stale_data_is_red_even_with_every_service_active(self):
        status = healthy_status(data_age_s=7200.0, status_age_s=7200.0)
        level, reasons = ss.health_report(status)
        self.assertEqual(level, "bad",
                         "the dashboard showed green while nothing had been "
                         "recorded for two hours")
        self.assertTrue(any("Nothing recorded" in r for r in reasons), reasons)

    def test_the_reason_names_the_thing_to_go_and_check(self):
        # The person reading this is deciding whether to book access to a
        # roof. "Health: bad" does not help them; a cable does.
        _level, reasons = ss.health_report(healthy_status(data_age_s=7200.0))
        self.assertTrue(any("anemometer" in r.lower() for r in reasons),
                        reasons)

    def test_fresh_data_with_everything_running_is_green(self):
        level, reasons = ss.health_report(healthy_status())
        self.assertEqual(level, "ok")
        self.assertEqual(reasons, [])

    def test_a_gap_shorter_than_the_threshold_is_not_an_alarm(self):
        # Rotation, a reconnect, a slow flush: seconds of silence are normal
        # and a page that cries wolf gets ignored.
        self.assertEqual(ss.overall_health(healthy_status(data_age_s=30.0)),
                         "ok")

    def test_never_having_recorded_is_red(self):
        status = healthy_status()
        status["recording"] = {}
        status["files"] = []
        level, reasons = ss.health_report(status)
        self.assertEqual(level, "bad")
        self.assertTrue(any("never" in r.lower() for r in reasons), reasons)

    def test_the_dashboard_shows_how_long_ago_data_arrived(self):
        page = ss.render_dashboard(healthy_status(data_age_s=7200.0))
        self.assertIn("since last data", page)
        self.assertIn("2 h 0 min", page)

    def test_the_age_advances_while_a_cached_snapshot_does_not(self):
        # gather_status() is cached for 10 s. An age computed at gather time
        # would sit frozen at "1 s" for the whole of it.
        status = healthy_status(data_age_s=1.0)
        first = ss.data_age_s(status)
        time.sleep(0.05)
        self.assertGreater(ss.data_age_s(status), first)


class TestStaleFiguresAreNotShownAsCurrent(unittest.TestCase):

    def test_parses_the_loggers_timestamp_format(self):
        stamp = ss._parse_log_time(STATUS_LINE)
        self.assertIsNotNone(stamp)
        # Formatted with %(asctime)s, i.e. local time; round-tripping through
        # localtime is what keeps this correct in any timezone.
        self.assertEqual(
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)),
            "2026-08-20 04:26:04")

    def test_a_line_without_a_timestamp_is_not_guessed_at(self):
        self.assertIsNone(ss._parse_log_time("status: 10.00 Hz now"))
        self.assertIsNone(ss._parse_log_time(""))

    def test_an_impossible_timestamp_does_not_crash_the_page(self):
        self.assertIsNone(ss._parse_log_time("2026-13-45 99:99:99 INFO x"))

    def test_an_old_status_line_is_flagged_rather_than_believed(self):
        status = healthy_status(data_age_s=1.0, status_age_s=3600.0)
        level, reasons = ss.health_report(status)
        self.assertEqual(level, "warn")
        self.assertTrue(any("not reported" in r for r in reasons), reasons)

    def test_the_stale_rate_is_blanked_out_on_the_page(self):
        # The big number is what gets read; the quoted journal below it still
        # says 10.00 Hz, but every line there carries its own timestamp.
        page = ss.render_dashboard(
            healthy_status(data_age_s=1.0, status_age_s=3600.0))
        self.assertNotIn('<div class="v">10.00</div>', page,
                         "an hour-old sample rate was rendered as if it were "
                         "the current one")
        self.assertIn("last reported", page)

    def test_a_current_rate_is_shown(self):
        self.assertIn('<div class="v">10.00</div>',
                      ss.render_dashboard(healthy_status()))


class TestHealthLevels(unittest.TestCase):

    def test_a_full_card_is_red(self):
        status = healthy_status(disk={"free_mb": 40.0, "estimated_days": 0.3})
        level, reasons = ss.health_report(status)
        self.assertEqual(level, "bad")
        self.assertTrue(any("full" in r for r in reasons), reasons)

    def test_a_card_with_under_a_week_left_is_yellow(self):
        status = healthy_status(disk={"free_mb": 600.0,
                                      "estimated_days": 4.9})
        self.assertEqual(ss.overall_health(status), "warn")

    def test_a_dead_logger_service_is_red(self):
        status = healthy_status(services={"trisonica-logger": "failed"})
        level, reasons = ss.health_report(status)
        self.assertEqual(level, "bad")
        self.assertTrue(any("logger service" in r for r in reasons), reasons)

    def test_a_dropped_sample_rate_is_yellow(self):
        status = healthy_status(logger={"sample_rate_hz": 2.0})
        self.assertEqual(ss.overall_health(status), "warn")

    def test_flagged_readings_are_yellow(self):
        status = healthy_status(logger={"bad_pct": 8.0})
        level, reasons = ss.health_report(status)
        self.assertEqual(level, "warn")
        self.assertTrue(any("obstruction" in r for r in reasons), reasons)

    def test_an_unverified_clock_is_yellow(self):
        # The data is still good; its timestamps are not, and that is exactly
        # the kind of thing nobody notices until analysis.
        status = healthy_status(logger={"time_synced": False})
        level, reasons = ss.health_report(status)
        self.assertEqual(level, "warn")
        self.assertTrue(any("clock" in r.lower() for r in reasons), reasons)

    def test_a_stopped_export_service_is_yellow_not_red(self):
        # Nothing is lost - it only means a stick plugged in on site would
        # not be filled.
        status = healthy_status(services={"trisonica-usb-export": "inactive"})
        self.assertEqual(ss.overall_health(status), "warn")

    def test_red_outranks_yellow_and_still_lists_everything(self):
        status = healthy_status(data_age_s=7200.0,
                                services={"trisonica-usb-export": "inactive"})
        level, reasons = ss.health_report(status)
        self.assertEqual(level, "bad")
        self.assertGreaterEqual(len(reasons), 2)
        self.assertIn("Nothing recorded", reasons[0],
                      "the recording failure has to be the first thing read")

    def test_anything_not_green_says_why(self):
        for status in (healthy_status(data_age_s=7200.0),
                       healthy_status(logger={"bad_pct": 8.0}),
                       healthy_status(disk={"free_mb": 40.0}),
                       healthy_status(services={"gpsd": "failed",
                                                "trisonica-logger": "failed"})):
            level, reasons = ss.health_report(status)
            if level != "ok":
                self.assertTrue(reasons, "%s with no reason given" % level)

    def test_missing_information_does_not_invent_an_alarm(self):
        # A journal that has rotated away leaves no status line at all. The
        # data files still say whether recording is happening.
        status = healthy_status()
        status["logger"] = {}
        self.assertEqual(ss.overall_health(status), "ok")


class TestAgeFormatting(unittest.TestCase):

    def test_reads_like_a_person_wrote_it(self):
        self.assertEqual(ss._fmt_age(9), "9 s")
        self.assertEqual(ss._fmt_age(90), "1 min")
        self.assertEqual(ss._fmt_age(3600), "1 h 0 min")
        self.assertEqual(ss._fmt_age(7380), "2 h 3 min")
        self.assertEqual(ss._fmt_age(90000), "1 d 1 h")
        self.assertEqual(ss._fmt_age(None), "never")


class TestTailingTheOpenFile(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "TrisonicaData_2026-08-20_0000Z.csv")

    def _write(self, rows):
        with open(self.path, "w") as fh:
            fh.write(HEADER + "\n")
            for i in range(rows):
                fh.write(ROW + ",row%d\n" % i)

    def test_returns_the_last_lines_oldest_first(self):
        self._write(50)
        lines = ss._tail_lines(self.path, 5)
        self.assertEqual(len(lines), 5)
        self.assertIn("row45", lines[0])
        self.assertIn("row49", lines[-1])

    def test_a_file_shorter_than_the_request_is_returned_whole(self):
        self._write(2)
        self.assertEqual(len(ss._tail_lines(self.path, 25)), 3)  # + header

    def test_a_row_the_logger_is_still_writing_is_not_shown(self):
        # The live view reads a file that is being appended to. A row without
        # its newline yet is half a row.
        self._write(30)
        with open(self.path, "a") as fh:
            fh.write("2026-08-20T04:30:00.000000Z,ntp,1,00.0")
        lines = ss._tail_lines(self.path, 5)
        self.assertTrue(all(line.endswith("29") or line.count(",") ==
                            ROW.count(",") + 1 for line in lines), lines)
        self.assertNotIn("04:30:00.000000Z,ntp,1,00.0", lines[-1])

    def test_a_missing_file_is_not_an_exception(self):
        self.assertEqual(ss._tail_lines(os.path.join(self.dir, "no.csv"), 5),
                         [])

    def test_an_empty_file_is_not_an_exception(self):
        open(self.path, "w").close()
        self.assertEqual(ss._tail_lines(self.path, 5), [])

    def test_no_truncated_first_row_from_the_middle_of_a_block(self):
        # Reading backwards in 8 KB blocks lands mid-row; a half row on the
        # live page looks like corrupted data.
        self._write(500)
        for line in ss._tail_lines(self.path, 25):
            self.assertEqual(line.count(","), ROW.count(",") + 1,
                             "partial row served: %r" % line)

    def test_the_live_text_carries_the_header(self):
        self._write(50)
        text = ss.live_text(self.dir)
        self.assertTrue(text.startswith(HEADER),
                        "columns are unlabelled without the header row")
        self.assertIn("row49", text)

    def test_the_header_is_not_repeated_when_it_is_the_only_line(self):
        with open(self.path, "w") as fh:
            fh.write(HEADER + "\n")
        self.assertEqual(ss.live_text(self.dir).count(HEADER), 1)

    def test_an_empty_directory_says_so_rather_than_failing(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty)
        self.assertIn("No data files", ss.live_text(empty))


class TestSubprocessesCannotPileUp(unittest.TestCase):

    def test_a_wedged_command_is_killed_not_left_running(self):
        killed = []

        class FakeProc(object):
            def communicate(self, timeout=None):
                if not killed:
                    raise subprocess.TimeoutExpired("cmd", timeout)
                return (b"", b"")

            def kill(self):
                killed.append(True)

        real_popen = ss.subprocess.Popen
        ss.subprocess.Popen = lambda *a, **k: FakeProc()
        self.addCleanup(setattr, ss.subprocess, "Popen", real_popen)

        self.assertEqual(ss._run(["journalctl"], timeout=0.1), "")
        self.assertTrue(killed,
                        "a timed-out journalctl survived the request; a "
                        "browser refreshing every 60 s would spawn more")

    def test_a_command_that_does_not_exist_is_not_an_exception(self):
        self.assertEqual(ss._run(["definitely-not-a-command-here"]), "")


class TestParsingTheStatusLine(unittest.TestCase):

    def test_pulls_out_every_field(self):
        fields = ss._parse_status_line(STATUS_LINE)
        self.assertEqual(fields["total_rows"], 678169)
        self.assertEqual(fields["sample_rate_hz"], 10.0)
        self.assertEqual(fields["bad_pct"], 0.0)
        self.assertEqual(fields["gps_state"], "nofix")
        self.assertEqual(fields["time_source"], "ntp")
        self.assertTrue(fields["time_synced"])
        self.assertEqual(fields["logger_free_mb"], 3614.0)

    def test_an_unsynced_clock_is_read_as_false_not_as_a_string(self):
        line = STATUS_LINE.replace("time=ntp(synced=True)",
                                   "time=freerun(synced=False)")
        self.assertIs(ss._parse_status_line(line)["time_synced"], False)

    def test_a_line_from_a_future_format_yields_what_it_can(self):
        self.assertEqual(ss._parse_status_line("status: nothing here"), {})


# ---------------------------------------------------------------------------
# Live server
# ---------------------------------------------------------------------------

class StubCache(object):
    """Stands in for StatusCache so tests never shell out to systemctl."""

    def __init__(self, status):
        self.status = status

    def get(self):
        return self.status


class ServerTestCase(unittest.TestCase):
    """Runs the real handler over a real socket on the loopback address."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.names = []
        for stamp in ("2026-08-20_033541Z", "2026-08-20_093541Z"):
            name = "TrisonicaData_%s.csv" % stamp
            with open(os.path.join(self.dir, name), "w") as fh:
                fh.write(HEADER + "\n" + ROW + "\n")
            self.names.append(name)

        self.server = ss.StatusHTTPServer(("127.0.0.1", 0), self.dir)
        self.addCleanup(self.server.server_close)
        status = healthy_status()
        status["files"] = ss.get_data_files(self.dir)
        status["recording"] = ss.get_recording_info(status["files"])
        self.server.cache = StubCache(status)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def get(self, path):
        """Request *path* verbatim - no client-side normalisation."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()


class TestReadOnlyByConstruction(unittest.TestCase):
    """Ole gets a URL; the code stays mine. This is the half of that promise
    that lives in the code - the other half is the unit file below.
    """

    def test_only_get_is_implemented(self):
        for verb in ("POST", "PUT", "DELETE", "PATCH", "HEAD"):
            self.assertFalse(
                hasattr(ss.StatusHandler, "do_" + verb),
                "the server implements %s; it is supposed to be readable "
                "only" % verb)

    def test_the_module_never_opens_a_file_for_writing(self):
        import ast
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "trisonica_status_server.py")) as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None)
            if name != "open":
                continue
            modes = [a for a in node.args[1:]]
            for mode in modes:
                value = getattr(mode, "value", getattr(mode, "s", ""))
                self.assertNotIn(
                    "w", str(value),
                    "line %s opens a file for writing" % node.lineno)
                self.assertNotIn(
                    "a", str(value),
                    "line %s opens a file for appending" % node.lineno)


class TestServedPaths(ServerTestCase):

    def test_the_dashboard_renders(self):
        code, headers, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"TriSonica Field Logger", body)

    def test_the_listing_offers_every_file(self):
        code, _headers, body = self.get("/data/")
        self.assertEqual(code, 200)
        for name in self.names:
            self.assertIn(name.encode(), body)

    def test_a_file_downloads_byte_for_byte(self):
        name = self.names[0]
        code, headers, body = self.get("/data/" + name)
        self.assertEqual(code, 200)
        with open(os.path.join(self.dir, name), "rb") as fh:
            self.assertEqual(body, fh.read())
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertIn(name, headers["Content-Disposition"])

    def test_the_whole_archive_is_a_valid_tar_gz(self):
        code, headers, body = self.get("/download-all")
        self.assertEqual(code, 200)
        self.assertIn("gzip", headers["Content-Type"])
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
            self.assertEqual(sorted(tar.getnames()), sorted(self.names))

    def test_the_json_api_carries_the_verdict_not_just_the_readings(self):
        code, headers, body = self.get("/api/status")
        self.assertEqual(code, 200)
        self.assertIn("application/json", headers["Content-Type"])
        payload = json.loads(body.decode("utf-8"))
        self.assertIn("health", payload)
        self.assertIn(payload["health"]["level"], ("ok", "warn", "bad"))
        self.assertIn("data_age_s", payload["health"])

    def test_the_live_endpoint_returns_rows(self):
        code, headers, body = self.get("/api/live")
        self.assertEqual(code, 200)
        self.assertIn("text/plain", headers["Content-Type"])
        self.assertIn(b"2026-08-20T04:29:26", body)

    def test_the_live_page_renders(self):
        code, _headers, body = self.get("/live")
        self.assertEqual(code, 200)
        self.assertIn(b"Live Data", body)

    def test_an_unknown_path_is_a_clean_404(self):
        self.assertEqual(self.get("/wp-admin")[0], 404)


class TestTheJournalIsNotAWriteTarget(ServerTestCase):
    """The journal is on the same SD card as the data, and the dashboard
    reloads itself every minute. Anything that logs once per request lets a
    browser tab - or anything else on the LAN - write to that card all day.
    """

    def _records(self, path):
        captured = []

        class Capture(logging.Handler):
            def emit(self, record):
                captured.append(record)

        handler = Capture()
        ss.log.addHandler(handler)
        self.addCleanup(ss.log.removeHandler, handler)
        previous = ss.log.level
        ss.log.setLevel(logging.DEBUG)
        self.addCleanup(ss.log.setLevel, previous)
        self.get(path)
        return captured

    def test_a_browser_asking_for_a_favicon_gets_an_answer_not_a_404(self):
        code, _headers, body = self.get("/favicon.ico")
        self.assertEqual(code, 204)
        self.assertEqual(body, b"")

    def test_a_client_mistake_does_not_write_a_warning(self):
        levels = [r.levelno for r in self._records("/wp-admin")]
        self.assertTrue(levels, "the request was not logged at all")
        self.assertFalse([lv for lv in levels if lv >= logging.WARNING],
                         "a 404 from a scanner wrote a WARNING to the SD card")

    def test_trying_to_change_something_is_still_loud(self):
        # 501 means someone sent POST/PUT/DELETE at a unit that only reads.
        # That is worth a line even though it costs one.
        captured = []

        class Capture(logging.Handler):
            def emit(self, record):
                captured.append(record)

        handler = Capture()
        ss.log.addHandler(handler)
        self.addCleanup(ss.log.removeHandler, handler)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", "/data/", body="x=1")
            self.assertEqual(conn.getresponse().status, 501)
        finally:
            conn.close()
        self.assertTrue([r for r in captured if r.levelno >= logging.WARNING],
                        "an attempt to modify the unit was logged quietly")


class TestNothingOutsideTheDataDirectory(ServerTestCase):

    def test_traversal_out_of_the_data_directory_is_refused(self):
        for path in ("/data/../../../../etc/passwd",
                     "/data/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                     "/data//etc/passwd",
                     "/data/....//etc/passwd"):
            code, _headers, body = self.get(path)
            self.assertEqual(code, 404, path)
            self.assertNotIn(b"root:", body, path)

    def test_a_non_csv_neighbour_is_not_served(self):
        # Anything else that ends up in the data directory - notes, a key
        # somebody parked there - is not part of the deal.
        with open(os.path.join(self.dir, "private.txt"), "w") as fh:
            fh.write("secret")
        self.assertEqual(self.get("/data/private.txt")[0], 404)

    def test_a_symlink_pointing_out_of_the_directory_is_refused(self):
        target = os.path.join(self.dir, "outside.csv")
        with open(target, "w") as fh:
            fh.write("elsewhere")
        link = os.path.join(self.dir, "escape.csv")
        os.symlink("/etc/passwd", link)
        code, _headers, body = self.get("/data/escape.csv")
        self.assertEqual(code, 404)
        self.assertNotIn(b"root:", body)

    def test_a_null_byte_is_rejected(self):
        self.assertEqual(self.get("/data/x%00.csv")[0], 400)

    def test_a_missing_file_is_a_404_not_a_traceback(self):
        self.assertEqual(self.get("/data/TrisonicaData_1999-01-01_0000Z.csv")[0],
                         404)


class TestFilenamesCannotInjectMarkup(unittest.TestCase):

    def test_the_listing_escapes_what_it_prints(self):
        files = [{"name": '<script>alert(1)</script>.csv',
                  "size_bytes": 10, "mtime_epoch": time.time(),
                  "mtime_utc": "2026-08-20 04:31 UTC"}]
        page = ss.render_file_listing(files)
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)

    def test_the_dashboard_escapes_the_journal_it_quotes(self):
        status = healthy_status()
        status["logger"]["recent_log"] = ["<img src=x onerror=alert(1)>"]
        page = ss.render_dashboard(status)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;img", page)

    def test_the_health_reasons_are_escaped_too(self):
        status = healthy_status(services={"trisonica-logger": "<b>failed</b>"})
        page = ss.render_dashboard(status)
        self.assertNotIn("<b>failed", page)


class TestStatusUnitHardening(unittest.TestCase):
    """The unit file is what makes 'read-only' true even if this Python is
    wrong. It runs as a user with passwordless sudo, on a network nobody
    controls, so the sandbox is not decoration.

    Directives are checked for being EFFECTIVE on systemd 241 (Raspbian
    Buster), not merely present: systemd logs one line about an unknown
    lvalue and carries on, and nothing afterwards says the protection is
    absent. Same trap as TestExportUnitHardening in test_field_logger.py.
    """

    # All post-241 and therefore silently ignored on this unit.
    TOO_NEW = ("ProtectClock", "ProtectHostname", "ProtectKernelLogs",
               "RestrictSUIDSGID", "ProtectProc", "PrivateIPC",
               "ProcSubset")

    def _directives(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "trisonica-status.service")) as fh:
            lines = fh.read().splitlines()
        return [s for s in (line.strip() for line in lines)
                if s and not s.startswith("#") and not s.startswith("[")]

    def _value(self, key):
        for directive in self._directives():
            if directive.startswith(key + "="):
                return directive.split("=", 1)[1].strip()
        return None

    def test_it_cannot_write_to_the_code_it_serves_from(self):
        self.assertEqual(self._value("ProtectSystem"), "strict")
        self.assertEqual(self._value("ProtectHome"), "read-only",
                         "the logger's code and data both live under "
                         "/home/pi; read-only is what keeps an HTTP bug from "
                         "reaching them")

    def test_it_does_not_run_as_root(self):
        self.assertEqual(self._value("User"), "pi")

    def test_privileges_cannot_be_regained(self):
        self.assertEqual(self._value("NoNewPrivileges"), "true")
        self.assertEqual(self._value("CapabilityBoundingSet"), "")

    def test_every_directive_is_understood_by_systemd_241(self):
        for directive in self._directives():
            key = directive.split("=", 1)[0]
            self.assertNotIn(
                key, self.TOO_NEW,
                "%s needs a newer systemd than this unit runs; it would be "
                "ignored and protect nothing" % key)

    def test_the_remote_eye_is_never_allowed_to_stay_shut(self):
        # With the dashboard dead, "the page does not load" and "the station
        # is dead" are indistinguishable from a desk - and the desk is the
        # only place it gets looked at.
        self.assertEqual(self._value("Restart"), "always")
        self.assertEqual(self._value("StartLimitIntervalSec"), "0")

    def test_it_yields_to_the_recording(self):
        nice = self._value("Nice")
        self.assertIsNotNone(nice, "a whole-archive download would compete "
                                   "with the logger for the card")
        self.assertGreater(int(nice), 0)

    def test_it_claims_no_io_priority_the_card_would_ignore(self):
        # mq-deadline, this unit's scheduler, honours no I/O priorities. A
        # directive here would read like protection and do nothing.
        self.assertIsNone(self._value("IOSchedulingClass"))


class TestGatheringFromARealDirectory(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_files_are_listed_with_size_and_time(self):
        path = os.path.join(self.dir, "TrisonicaData_2026-08-20_0000Z.csv")
        with open(path, "w") as fh:
            fh.write(HEADER + "\n")
        files = ss.get_data_files(self.dir)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["size_bytes"], len(HEADER) + 1)
        self.assertIn("mtime_epoch", files[0])

    def test_non_csv_files_are_not_listed(self):
        open(os.path.join(self.dir, "notes.txt"), "w").close()
        self.assertEqual(ss.get_data_files(self.dir), [])

    def test_a_missing_directory_is_empty_rather_than_fatal(self):
        self.assertEqual(ss.get_data_files("/no/such/place"), [])
        self.assertEqual(ss.get_disk_info("/no/such/place"), {})

    def test_the_newest_file_is_by_time_not_by_name(self):
        # After a clock correction the newest rows can land in a file whose
        # name sorts earlier. Freshness is a question about the clock, so it
        # is answered with mtimes.
        old = os.path.join(self.dir, "TrisonicaData_2026-08-20_0600Z.csv")
        new = os.path.join(self.dir, "TrisonicaData_2026-08-20_0100Z.csv")
        for path in (old, new):
            with open(path, "w") as fh:
                fh.write(HEADER + "\n")
        os.utime(old, (1000, 1000))
        info = ss.get_recording_info(ss.get_data_files(self.dir))
        self.assertEqual(info["newest_file"], os.path.basename(new))

    def test_disk_info_reports_days_remaining(self):
        info = ss.get_disk_info(self.dir)
        self.assertGreater(info["total_mb"], 0)
        self.assertIn("estimated_days", info)


class TestTheDeadMansSwitch(unittest.TestCase):
    """Silence has to be the alarm.

    A station that emails its own bad news says nothing when it is the thing
    that failed, and on a roof nobody visits, "no email" then reads as
    "everything is fine". These tests are about the cases where the station
    cannot speak for itself.
    """

    def status(self, level, reasons=()):
        return {"health": {"level": level, "reasons": list(reasons),
                           "data_age_s": 3.0},
                "system": {"hostname": "raspberrypi", "uptime": "up 7 days",
                           "timestamp_utc": "2026-08-20 05:00:00 UTC"},
                "recording": {"newest_file": "TrisonicaData.csv"},
                "disk": {"free_mb": 3600.0, "estimated_days": 29.5}}

    def test_a_healthy_station_pings_and_stays_quiet(self):
        alarm, _summary, count = al.decide(self.status("ok"), 0)
        self.assertFalse(alarm)
        self.assertEqual(count, 0)

    def test_recording_stopped_raises_the_alarm_immediately(self):
        # No waiting, no consecutive-check rule: this is the one that costs
        # data every minute it goes unnoticed.
        alarm, summary, _count = al.decide(
            self.status("bad", ["Nothing recorded for 2 h 0 min"]), 0)
        self.assertTrue(alarm)
        self.assertIn("NOT BEING RECORDED", summary)
        self.assertIn("Nothing recorded for 2 h", summary)

    def test_a_passing_warning_does_not_wake_anyone(self):
        alarm, _summary, count = al.decide(self.status("warn", ["x"]), 0)
        self.assertFalse(alarm)
        self.assertEqual(count, 1)

    def test_a_warning_that_persists_escalates(self):
        # A card with days left needs a visit booked; it must not be possible
        # for that to sit yellow on a page nobody opens until it goes red.
        count = 0
        alarms = []
        for _ in range(al.ESCALATE_AFTER):
            alarm, _summary, count = al.decide(
                self.status("warn", ["The card is nearly full"]), count)
            alarms.append(alarm)
        self.assertEqual(alarms[:-1], [False] * (al.ESCALATE_AFTER - 1))
        self.assertTrue(alarms[-1], "a warning never escalated")

    def test_recovery_clears_the_run(self):
        _alarm, _summary, count = al.decide(self.status("warn"), 3)
        self.assertEqual(count, 4)
        _alarm, _summary, count = al.decide(self.status("ok"), count)
        self.assertEqual(count, 0, "a recovered station stayed armed")

    def test_an_unreachable_dashboard_is_not_instantly_an_alarm(self):
        # The status service restarts itself within 30 s. One missed poll
        # during that window is not worth an email.
        alarm, summary, count = al.decide(None, 0)
        self.assertFalse(alarm)
        self.assertEqual(count, 1)
        self.assertIn("not answering", summary)

    def test_an_unreachable_dashboard_that_stays_down_does_alarm(self):
        alarm, _summary, _count = al.decide(None, al.ESCALATE_AFTER - 1)
        self.assertTrue(alarm)

    def test_a_level_this_version_does_not_know_is_treated_as_a_warning(self):
        alarm, _summary, count = al.decide(self.status("catastrophe"), 0)
        self.assertFalse(alarm)
        self.assertEqual(count, 1)

    def test_the_alarm_ping_goes_to_the_fail_endpoint(self):
        sent = {}

        class FakeResponse(object):
            def read(self):
                return b"OK"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            sent["url"] = request.get_full_url()
            sent["body"] = request.data
            return FakeResponse()

        real = al.urllib.request.urlopen
        al.urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, al.urllib.request, "urlopen", real)

        self.assertTrue(al.ping("https://hc-ping.com/uuid", "down", True))
        self.assertTrue(sent["url"].endswith("/fail"),
                        "an alarm was sent as a normal heartbeat: %s"
                        % sent["url"])
        self.assertIn(b"down", sent["body"])

        al.ping("https://hc-ping.com/uuid", "fine", False)
        self.assertFalse(sent["url"].endswith("/fail"))

    def test_a_monitor_that_cannot_be_reached_is_not_fatal(self):
        def fake_urlopen(request, timeout=None):
            raise OSError("network is unreachable")

        real = al.urllib.request.urlopen
        al.urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, al.urllib.request, "urlopen", real)
        # The monitor notices the missing ping by itself; crashing here would
        # only fill the journal.
        self.assertFalse(al.ping("https://hc-ping.com/uuid", "x", False))

    def test_the_body_carries_what_the_email_needs(self):
        body = al.build_body(self.status("bad", ["stopped"]), "SUMMARY")
        for expected in ("SUMMARY", "raspberrypi", "last row", "card"):
            self.assertIn(expected, body)

    def test_a_body_survives_having_no_status_at_all(self):
        self.assertIn("SUMMARY", al.build_body(None, "SUMMARY"))


class TestAlertConfiguration(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def _config(self, text):
        path = os.path.join(self.dir, "alert.conf")
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_reads_the_ping_url(self):
        path = self._config("# monitor\nPING_URL=https://hc-ping.com/abc\n")
        self.assertEqual(al.read_config(path)["PING_URL"],
                         "https://hc-ping.com/abc")

    def test_comments_and_junk_are_ignored(self):
        path = self._config("\n# note\nnonsense\nPING_URL = https://x/y \n")
        self.assertEqual(al.read_config(path), {"PING_URL": "https://x/y"})

    def test_a_missing_config_is_empty_not_an_exception(self):
        self.assertEqual(al.read_config("/no/such/file"), {})

    def test_an_unconfigured_install_does_nothing_at_all(self):
        # The unit ships before the monitor account exists. It must not fail,
        # and must not ping anything it invented.
        def fake_urlopen(*a, **k):
            raise AssertionError("pinged with no PING_URL configured")

        real = al.urllib.request.urlopen
        al.urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, al.urllib.request, "urlopen", real)
        real_argv = sys.argv
        sys.argv = ["trisonica_alert.py", "--config", "/no/such/file"]
        self.addCleanup(setattr, sys, "argv", real_argv)
        self.assertEqual(al.main(), 0)

    def test_the_counter_survives_between_runs(self):
        al.write_counter(self.dir, 4)
        self.assertEqual(al.read_counter(self.dir), 4)

    def test_a_missing_counter_starts_at_zero(self):
        self.assertEqual(al.read_counter("/no/such/dir"), 0)

    def test_an_unwritable_state_dir_does_not_stop_the_check(self):
        # Losing the count costs a delayed escalation, not the alarm itself.
        al.write_counter("/proc/nowhere", 3)


class TestAlertUnitHardening(unittest.TestCase):
    """Same rules as the dashboard's unit: effective on systemd 241, and no
    write access to anything but its own state directory.
    """

    def _directives(self, filename):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, filename)) as fh:
            lines = fh.read().splitlines()
        return [s for s in (line.strip() for line in lines)
                if s and not s.startswith("#") and not s.startswith("[")]

    def _value(self, filename, key):
        for directive in self._directives(filename):
            if directive.startswith(key + "="):
                return directive.split("=", 1)[1].strip()
        return None

    def test_the_checker_cannot_write_to_the_data_or_the_code(self):
        self.assertEqual(
            self._value("trisonica-alert.service", "ProtectSystem"), "strict")
        self.assertEqual(
            self._value("trisonica-alert.service", "ProtectHome"), "read-only")

    def test_it_gets_exactly_one_writable_path(self):
        self.assertEqual(
            self._value("trisonica-alert.service", "StateDirectory"),
            "trisonica-alert")

    def test_every_directive_is_understood_by_systemd_241(self):
        for directive in self._directives("trisonica-alert.service"):
            key = directive.split("=", 1)[0]
            self.assertNotIn(key, TestStatusUnitHardening.TOO_NEW,
                             "%s needs a newer systemd than this unit runs"
                             % key)

    def test_a_wedged_check_cannot_outlive_its_own_interval(self):
        timeout = self._value("trisonica-alert.service", "TimeoutStartSec")
        self.assertIsNotNone(timeout)
        self.assertLessEqual(int(timeout), 300)

    def test_the_timer_does_not_replay_history_after_a_power_cut(self):
        # Persistent=true would fire a catch-up run for every interval missed
        # while the unit was off, reporting a week nobody can act on.
        self.assertIsNone(self._value("trisonica-alert.timer", "Persistent"))
        self.assertIsNotNone(
            self._value("trisonica-alert.timer", "OnUnitActiveSec"))

    def test_the_timer_does_not_fire_before_the_station_is_up(self):
        # Power-cut is the normal way this unit starts; a check at t=0 would
        # report a healthy station as broken every single time.
        boot = self._value("trisonica-alert.timer", "OnBootSec")
        self.assertIsNotNone(boot)
        self.assertNotIn(boot, ("0", "0s", "0min"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

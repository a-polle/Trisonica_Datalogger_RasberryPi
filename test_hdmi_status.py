#!/usr/bin/env python3
"""Focused tests for the low-overhead HDMI status screen."""

import os
import tempfile
import unittest

import trisonica_hdmi_status as hs


def healthy_status():
    return {
        "health": {"level": "ok", "reasons": [], "data_age_s": 2.4},
        "reading": {"values": {
            "S": {"value": 1.25}, "D": {"value": 270},
            "T": {"value": 21.4}, "H": {"value": 51},
            "P": {"value": 1008},
        }},
        "logger": {"sample_rate_hz": 10.0, "bad_pct": 0.0},
        "disk": {"free_mb": 5100, "estimated_days": 42},
        "system": {"timestamp_utc": "2026-09-08 03:00:00 UTC"},
    }


class TestStatusUrl(unittest.TestCase):
    def test_prefix_follows_the_dashboard_configuration(self):
        handle, path = tempfile.mkstemp()
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with os.fdopen(handle, "w") as fh:
            fh.write("PUBLIC_PREFIX=private-path\n")
        self.assertEqual(
            hs.status_url(path),
            "http://127.0.0.1:8080/private-path/api/status")

    def test_no_configuration_uses_the_local_plain_endpoint(self):
        self.assertEqual(
            hs.status_url("/no/such/file"),
            "http://127.0.0.1:8080/api/status")

    def test_an_invalid_prefix_is_refused(self):
        handle, path = tempfile.mkstemp()
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with os.fdopen(handle, "w") as fh:
            fh.write("PUBLIC_PREFIX=two/segments\n")
        with self.assertRaises(ValueError):
            hs.status_url(path)


class TestScreen(unittest.TestCase):
    def test_healthy_screen_contains_only_the_useful_readings(self):
        page = hs.render(healthy_status())
        self.assertIn("RECORDING NORMALLY", page)
        self.assertIn("1.25 m/s", page)
        self.assertIn("21.4 C", page)
        self.assertIn("10.00 Hz", page)
        self.assertIn("about 42 days", page)
        self.assertNotIn("api/status", page)
        self.assertNotIn("raspberrypi", page)

    def test_fault_reason_is_visible(self):
        status = healthy_status()
        status["health"] = {
            "level": "bad", "data_age_s": 900,
            "reasons": ["Nothing has been recorded for 15 minutes"],
        }
        page = hs.render(status, width=50)
        self.assertIn("NOT RECORDING", page)
        self.assertIn("Nothing has been recorded", page)

    def test_unreachable_status_is_plain_and_nontechnical(self):
        page = hs.render(None)
        self.assertIn("STATUS TEMPORARILY UNAVAILABLE", page)
        self.assertIn("Retrying automatically", page)
        self.assertNotIn("exception", page.lower())


if __name__ == "__main__":
    unittest.main()

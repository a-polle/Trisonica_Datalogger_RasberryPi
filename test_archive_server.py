#!/usr/bin/env python3
"""Focused tests for the collector-side read-only archive."""

import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

import trisonica_archive_server as archive


class TestConfiguration(unittest.TestCase):
    def test_prefix_is_required_and_normalized(self):
        with self.assertRaises(ValueError):
            archive.normalize_prefix("")
        self.assertEqual(archive.normalize_prefix("/private-token/"),
                         "/private-token")

    def test_unsafe_prefixes_are_rejected(self):
        for value in ("..", "a/b", "white space", "x?y", "x#y"):
            with self.assertRaises(ValueError, msg=value):
                archive.normalize_prefix(value)

    def test_station_link_must_be_plain_https(self):
        self.assertEqual(
            archive.normalize_external_url("https://station.example/x"),
            "https://station.example/x/")
        for value in ("http://station.example/x", "javascript:alert(1)",
                      "https://user:pass@station.example/x",
                      "https://station.example/x?q=1"):
            with self.assertRaises(ValueError, msg=value):
                archive.normalize_external_url(value)


class TestArchiveData(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)

    def write(self, name, content=b"time,S\nnow,1\n"):
        path = os.path.join(self.root, name)
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def test_only_regular_csv_files_are_listed(self):
        name = "TrisonicaData_2026-09-08_000000Z.csv"
        self.write(name)
        self.write("private.txt", b"secret")
        self.write("unrelated.csv", b"not station data")
        os.symlink("/etc/passwd", os.path.join(
            self.root, "TrisonicaData_escape.csv"))
        self.assertEqual([item["name"] for item in archive.get_files(self.root)],
                         [name])

    def test_summary_uses_only_safe_backup_fields(self):
        self.write("TrisonicaData_2026-09-08_000000Z.csv", b"1234")
        files = archive.get_files(self.root)
        payload = {
            "last_success_epoch": 1000,
            "remote": "pi@private-address",
            "error_tail": ["secret detail"],
        }
        summary = archive.archive_summary(files, payload, now=1100)
        self.assertEqual(summary["files"], 1)
        self.assertEqual(summary["bytes"], 4)
        rendered = archive.render_home("/token", files, payload, now=1100)
        self.assertNotIn("private-address", rendered)
        self.assertNotIn("secret detail", rendered)

    def test_missing_status_reads_naturally(self):
        rendered = archive.render_home("/token", [], {})
        self.assertIn("Unknown", rendered)
        self.assertNotIn("unknown ago", rendered)


class ServerTestCase(unittest.TestCase):
    PREFIX = "/private-token"

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.data = os.path.join(self.root, "data")
        os.mkdir(self.data)
        self.name = "TrisonicaData_2026-09-08_000000Z.csv"
        self.content = b"time,S\n2026-09-08T00:00:00Z,1.2\n"
        with open(os.path.join(self.data, self.name), "wb") as fh:
            fh.write(self.content)
        with open(os.path.join(self.data, "private.txt"), "w") as fh:
            fh.write("not public")
        self.status = os.path.join(self.root, "backup-status.json")
        with open(self.status, "w") as fh:
            json.dump({"last_success_epoch": time.time(),
                       "remote": "pi@private-address"}, fh)

        self.server = archive.ArchiveServer(
            ("127.0.0.1", 0), self.data, self.status, self.PREFIX,
            "https://station.example/live/")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_private_root_shows_current_archive(self):
        code, headers, body = self.request(self.PREFIX + "/")
        self.assertEqual(code, 200)
        self.assertIn(b"Backup current", body)
        self.assertIn(b"1", body)
        self.assertIn(b"Live station", body)
        self.assertNotIn(b"private-address", body)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])

    def test_bare_host_and_guessed_paths_reveal_nothing(self):
        for path in ("/", "/data/", "/wp-admin", "/private-token-wrong/"):
            code, headers, body = self.request(path)
            self.assertEqual(code, 404, path)
            self.assertNotIn(self.PREFIX.encode(), body)
            self.assertNotIn("Python", headers.get("Server", ""))

    def test_listing_links_to_archived_file(self):
        code, _headers, body = self.request(self.PREFIX + "/data/")
        self.assertEqual(code, 200)
        self.assertIn(self.name.encode(), body)
        self.assertNotIn(b"private.txt", body)

    def test_csv_download_is_byte_exact(self):
        code, headers, body = self.request(
            self.PREFIX + "/data/" + self.name)
        self.assertEqual(code, 200)
        self.assertEqual(body, self.content)
        self.assertEqual(int(headers["Content-Length"]), len(self.content))
        self.assertIn(self.name, headers["Content-Disposition"])

    def test_non_csv_and_traversal_are_refused(self):
        for path in ("private.txt", "unrelated.csv", "../../etc/passwd",
                     "%2e%2e%2fescape.csv",
                     "TrisonicaData_1999-01-01_000000Z.csv"):
            code, _headers, body = self.request(
                self.PREFIX + "/data/" + path)
            self.assertEqual(code, 404, path)
            self.assertNotIn(b"root:", body)

    def test_symlink_escape_is_refused(self):
        name = "TrisonicaData_escape.csv"
        os.symlink("/etc/passwd", os.path.join(self.data, name))
        code, _headers, body = self.request(
            self.PREFIX + "/data/" + name)
        self.assertEqual(code, 404)
        self.assertNotIn(b"root:", body)

    def test_modifying_methods_are_refused(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(self.request(self.PREFIX + "/data/", method)[0],
                             405, method)


class TestServiceSandbox(unittest.TestCase):
    def setUp(self):
        path = os.path.join(os.path.dirname(__file__),
                            "trisonica-archive.service")
        with open(path) as fh:
            self.unit = fh.read()

    def test_service_uses_a_dynamic_identity_and_read_only_archive_bind(self):
        self.assertIn("DynamicUser=yes", self.unit)
        self.assertIn("ProtectHome=yes", self.unit)
        self.assertIn("BindReadOnlyPaths=", self.unit)
        self.assertNotIn("ReadWritePaths=", self.unit)

    def test_service_has_no_privileges_or_writable_system(self):
        for directive in ("NoNewPrivileges=yes", "ProtectSystem=strict",
                          "CapabilityBoundingSet=", "MemoryDenyWriteExecute=yes"):
            self.assertIn(directive, self.unit)


if __name__ == "__main__":
    unittest.main(verbosity=2)

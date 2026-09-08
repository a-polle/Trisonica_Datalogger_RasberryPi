#!/usr/bin/env python3
"""Small read-only web server for the off-site TriSonica CSV archive.

This runs on the collector, not on the field Raspberry Pi.  It deliberately
serves only CSV files from one directory and a small summary derived from the
collector's backup-status.json.  There are no write methods, commands, APIs,
or paths into the rest of the host.
"""

import argparse
import datetime
import html
import http.server
import json
import logging
import os
import re
import signal
import socketserver
import stat
import sys
import threading
import time
from urllib.parse import quote as _url_quote
from urllib.parse import unquote as _url_unquote
from urllib.parse import urlsplit


DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8081
DEFAULT_DATA_DIR = "/srv/trisonica-archive/data"
DEFAULT_STATUS_FILE = "/srv/trisonica-archive/backup-status.json"
DEFAULT_CONFIG = "/etc/trisonica-archive.conf"
REQUEST_TIMEOUT_S = 30
DOWNLOAD_TIMEOUT_S = 600
MAX_CONCURRENT_REQUESTS = 24
CSV_NAME_RE = re.compile(r"^TrisonicaData_[A-Za-z0-9_.-]+\.csv$")

log = logging.getLogger("trisonica-archive")


def read_config(path):
    values = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except (IOError, OSError):
        return {}
    return values


def normalize_prefix(raw):
    token = (raw or "").strip().strip("/")
    if not token:
        raise ValueError("PUBLIC_PREFIX is required")
    if not re.match(r"^[A-Za-z0-9._~-]+$", token) or set(token) == {"."}:
        raise ValueError("PUBLIC_PREFIX must be one safe path segment")
    return "/" + token


def normalize_external_url(raw):
    value = (raw or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise ValueError("STATION_URL must be a plain HTTPS URL")
    return value.rstrip("/") + "/"


def fmt_size(value):
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (value, unit)
        value /= 1024.0


def fmt_utc(epoch):
    if not epoch:
        return "Never"
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fmt_age(seconds):
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds < 120:
        return "%d seconds" % seconds
    if seconds < 7200:
        return "%d minutes" % round(seconds / 60.0)
    if seconds < 172800:
        return "%.1f hours" % (seconds / 3600.0)
    return "%.1f days" % (seconds / 86400.0)


def get_files(data_dir):
    files = []
    try:
        entries = os.scandir(data_dir)
    except (IOError, OSError):
        return files
    with entries:
        for entry in entries:
            if not CSV_NAME_RE.match(entry.name) or entry.is_symlink():
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except (IOError, OSError):
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            files.append({
                "name": entry.name,
                "size_bytes": info.st_size,
                "mtime": info.st_mtime,
            })
    return sorted(files, key=lambda item: (item["mtime"], item["name"]))


def read_backup_status(path):
    try:
        with open(path) as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except (IOError, OSError, ValueError):
        return {}


def page(title, body):
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>{title}</title><style>
:root{{--bg:#f4f6f8;--card:#fff;--text:#17212b;--muted:#607080;
--line:#dce2e7;--blue:#1769aa;--green:#238636;--amber:#9a6700}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);
font:16px/1.45 system-ui,-apple-system,sans-serif}}main{{max-width:850px;margin:auto;
padding:24px 16px 48px}}h1{{font-size:1.7rem;margin:0 0 4px}}h2{{font-size:1.1rem;
margin:0 0 12px}}.muted{{color:var(--muted)}}.card{{background:var(--card);
border:1px solid var(--line);border-radius:12px;padding:18px;margin-top:16px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px}}
.value{{font-size:1.35rem;font-weight:650}}.label{{color:var(--muted);font-size:.85rem}}
.badge{{display:inline-block;padding:4px 9px;border-radius:999px;background:#dafbe1;
color:var(--green);font-weight:650;font-size:.85rem}}.badge.old{{background:#fff1c2;
color:var(--amber)}}.btn{{display:inline-block;background:var(--blue);color:#fff;
text-decoration:none;padding:9px 13px;border-radius:8px;margin:6px 6px 0 0}}
.nav a{{color:var(--blue);text-decoration:none}}table{{width:100%;border-collapse:collapse}}
th,td{{padding:10px 6px;border-bottom:1px solid var(--line);text-align:left}}
th{{font-size:.8rem;color:var(--muted)}}td.r,th.r{{text-align:right;white-space:nowrap}}
td a{{color:var(--blue);overflow-wrap:anywhere}}.foot{{color:var(--muted);
font-size:.82rem;margin-top:18px}}@media(max-width:560px){{th:last-child,td:last-child{{display:none}}}}
</style></head><body><main>{body}</main></body></html>""".format(
        title=html.escape(title), body=body)


def link(prefix, path):
    if path == "/":
        return prefix + "/"
    return prefix + path


def archive_summary(files, status_payload, now=None):
    last_success = status_payload.get("last_success_epoch")
    if not isinstance(last_success, (int, float)):
        last_success = None
    now = time.time() if now is None else now
    age = max(0.0, now - last_success) if last_success else None
    return {
        "files": len(files),
        "bytes": sum(item["size_bytes"] for item in files),
        "last_success": last_success,
        "age_s": age,
        "current": age is not None and age <= 2 * 3600,
        "newest_file": files[-1]["name"] if files else None,
    }


def render_home(prefix, files, status_payload, station_url="", now=None):
    info = archive_summary(files, status_payload, now=now)
    badge = "Backup current" if info["current"] else "Check backup age"
    badge_class = "badge" if info["current"] else "badge old"
    age_text = (fmt_age(info["age_s"]) + " ago"
                if info["age_s"] is not None else "Unknown")
    parts = [
        '<h1>TriSonica Server Archive</h1>',
        '<div class="card"><span class="%s">%s</span>' %
        (badge_class, badge),
        '<div class="grid">',
        '<div><div class="value">%d</div><div class="label">CSV files</div></div>' %
        info["files"],
        '<div><div class="value">%s</div><div class="label">archive size</div></div>' %
        fmt_size(info["bytes"]),
        '<div><div class="value">%s</div><div class="label">last successful backup</div></div>' %
        html.escape(age_text),
        '</div><p class="muted">Last successful backup: %s</p>' %
        html.escape(fmt_utc(info["last_success"])),
        '<p><a class="btn" target="_blank" rel="noreferrer" href="%s">Data files</a>' %
        link(prefix, "/data/"),
    ]
    if station_url:
        parts.append(' <a class="btn" target="_blank" rel="noreferrer" href="%s">Live station</a>' %
                     html.escape(station_url, quote=True))
    parts.append('</p></div>')
    return page("TriSonica Server Archive", "\n".join(parts))


def render_listing(prefix, files, station_url=""):
    total = sum(item["size_bytes"] for item in files)
    parts = [
        '<div class="nav"><a href="%s">&larr; Archive status</a></div>' %
        link(prefix, "/"),
        '<div class="card"><h1>Server data files</h1>',
        '<p class="muted">%d files &middot; %s</p>' %
        (len(files), fmt_size(total)),
    ]
    if not files:
        parts.append("<p>No files have been backed up yet.</p>")
    else:
        parts.append('<table><thead><tr><th>File</th><th class="r">Size</th>'
                     '<th>Modified</th></tr></thead><tbody>')
        for item in reversed(files):
            name = html.escape(item["name"])
            href = link(prefix, "/data/" + _url_quote(item["name"]))
            parts.append('<tr><td><a href="%s">%s</a></td>'
                         '<td class="r">%s</td><td>%s</td></tr>' %
                         (href, name, fmt_size(item["size_bytes"]),
                          html.escape(fmt_utc(item["mtime"]))))
        parts.append("</tbody></table>")
    if station_url:
        parts.append('<p><a class="btn" target="_blank" rel="noreferrer" href="%s">Live station</a></p>' %
                     html.escape(station_url, quote=True))
    parts.append("</div>")
    return page("TriSonica Server Data", "\n".join(parts))


class ArchiveHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "TriSonicaArchive"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Request lines contain the private prefix. Keep it out of journals.
        return

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "base-uri 'none'; frame-ancestors 'none'; "
                         "form-action 'none'")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _path(self):
        path = urlsplit(self.path).path
        prefix = self.server.public_prefix
        if path == prefix or path == prefix + "/":
            return "/"
        if path.startswith(prefix + "/"):
            return path[len(prefix):]
        return None

    def _html(self, body, code=200):
        encoded = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        path = self._path()
        if path is None:
            self.send_error(404)
            return
        if path == "/":
            files = get_files(self.server.data_dir)
            status_payload = read_backup_status(self.server.status_file)
            self._html(render_home(self.server.public_prefix, files,
                                   status_payload, self.server.station_url))
        elif path in ("/data", "/data/"):
            self._html(render_listing(self.server.public_prefix,
                                      get_files(self.server.data_dir),
                                      self.server.station_url))
        elif path.startswith("/data/"):
            self._serve_file(path[len("/data/"):])
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def _serve_file(self, raw_name):
        try:
            filename = _url_unquote(raw_name)
        except Exception:
            self.send_error(400)
            return
        if (not filename or "\x00" in filename or "/" in filename or
                "\\" in filename or not CSV_NAME_RE.match(filename)):
            self.send_error(404)
            return
        path = os.path.join(self.server.data_dir, filename)
        root = os.path.realpath(self.server.data_dir)
        real = os.path.realpath(path)
        if (not real.startswith(root + os.sep) or os.path.islink(path) or
                not os.path.isfile(path)):
            self.send_error(404)
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            self.send_error(500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % filename)
        self.end_headers()
        try:
            self.connection.settimeout(DOWNLOAD_TIMEOUT_S)
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (IOError, OSError):
            pass

    def _reject_write(self):
        log.warning("rejected modifying HTTP request from %s", self.client_address[0])
        self.send_error(405, "Read-only archive")

    do_POST = _reject_write
    do_PUT = _reject_write
    do_PATCH = _reject_write
    do_DELETE = _reject_write


class ArchiveServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, data_dir, status_file, public_prefix,
                 station_url=""):
        self.data_dir = data_dir
        self.status_file = status_file
        self.public_prefix = public_prefix
        self.station_url = station_url
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        super().__init__(address, ArchiveHandler)

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(REQUEST_TIMEOUT_S)
        return sock, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def setup_logging(verbose):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)


def main():
    parser = argparse.ArgumentParser(description="Serve the TriSonica archive read-only")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--status-file", default=DEFAULT_STATUS_FILE)
    parser.add_argument("--bind", default=DEFAULT_BIND)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--public-prefix")
    parser.add_argument("--station-url")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)

    config = read_config(args.config)
    try:
        prefix = normalize_prefix(args.public_prefix or
                                  config.get("PUBLIC_PREFIX", ""))
        station_url = normalize_external_url(
            args.station_url if args.station_url is not None else
            config.get("STATION_URL", ""))
    except ValueError as exc:
        log.error("%s", exc)
        return 1

    if not os.path.isdir(args.data_dir):
        log.error("data directory does not exist: %s", args.data_dir)
        return 1

    try:
        server = ArchiveServer((args.bind, args.port), args.data_dir,
                               args.status_file, prefix, station_url)
    except OSError as exc:
        log.error("cannot start: %s", exc)
        return 1

    log.info("archive server listening on %s:%d", args.bind, args.port)
    log.info("serving %s below a private path", args.data_dir)

    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

#!/usr/bin/env python3
"""
agentchat-webui — tiny static + proxy server for the agentchat web UI.

- Serves web/index.html at /
- Proxies /v1/* to http://127.0.0.1:7878 (the agentchat API)
- Stdlib only.

Run:
  python3 server.py --port 7879 --api http://127.0.0.1:7878
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
import sys
import urllib.error
import urllib.request
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
INDEX_PATH = WEB_DIR / "index.html"


class WebUIHandler(http.server.BaseHTTPRequestHandler):
    server_version = "agentchat-webui/1.3.0"
    api_base: str = "http://127.0.0.1:7878"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}\n")

    # --- helpers ---
    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self._json(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _proxy(self, method: str, path: str) -> None:
        # Forward to api_base + path (with query string)
        target = self.api_base.rstrip("/") + path
        # pass through relevant headers
        fwd_headers = {}
        for h in ("Authorization", "Content-Type", "Accept"):
            v = self.headers.get(h)
            if v:
                fwd_headers[h] = v
        # read body if any
        body_bytes = None
        if method in ("POST", "PUT", "PATCH") and self.headers.get("Content-Length"):
            length = int(self.headers["Content-Length"])
            if length > 0:
                body_bytes = self.rfile.read(length)
        req = urllib.request.Request(target, data=body_bytes, method=method, headers=fwd_headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = resp.read()
                self.send_response(resp.status)
                # forward content-type if present
                ct = resp.headers.get("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as e:
            try:
                payload = e.read()
            except Exception:
                payload = json.dumps({"error": f"upstream {e.code}"}).encode()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except urllib.error.URLError as e:
            self._json(502, {"error": f"upstream unreachable: {e.reason}"})

    # --- routing ---
    def do_GET(self):
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        path = parsed.path or "/"

        if path == "/" or path == "/index.html":
            return self._send_file(INDEX_PATH, "text/html; charset=utf-8")

        if path.startswith("/v1/"):
            return self._proxy("GET", parsed.path + ("?" + parsed.query if parsed.query else ""))

        if path == "/health":
            return self._json(200, {"ok": True, "service": "agentchat-webui"})

        # --- PWA assets (v1.3) ---
        if path == "/manifest.webmanifest":
            return self._send_file(WEB_DIR / "manifest.webmanifest",
                                   "application/manifest+json; charset=utf-8")
        if path == "/sw.js":
            return self._send_file(WEB_DIR / "sw.js",
                                   "application/javascript; charset=utf-8")
        if path in ("/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"):
            return self._send_file(WEB_DIR / path.lstrip("/"), "image/png")

        # tiny favicon (no-op)
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        if parsed.path.startswith("/v1/"):
            return self._proxy("POST", parsed.path + ("?" + parsed.query if parsed.query else ""))
        self._json(404, {"error": "not found"})

    def do_PUT(self):
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        if parsed.path.startswith("/v1/"):
            return self._proxy("PUT", parsed.path + ("?" + parsed.query if parsed.query else ""))
        self._json(404, {"error": "not found"})

    def do_PATCH(self):
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        if parsed.path.startswith("/v1/"):
            return self._proxy("PATCH", parsed.path + ("?" + parsed.query if parsed.query else ""))
        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        if parsed.path.startswith("/v1/"):
            return self._proxy("DELETE", parsed.path + ("?" + parsed.query if parsed.query else ""))
        self._json(404, {"error": "not found"})

    def do_OPTIONS(self):
        # No CORS needed (same origin), but answer preflight cleanly
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=7879)
    p.add_argument("--api", default="http://127.0.0.1:7878",
                   help="agentchat API base URL")
    args = p.parse_args()

    WebUIHandler.api_base = args.api

    if not INDEX_PATH.exists():
        print(f"FATAL: {INDEX_PATH} not found", file=sys.stderr)
        return 2

    server = ThreadingHTTPServer((args.host, args.port), WebUIHandler)
    print(f"agentchat-webui listening on http://{args.host}:{args.port}  (proxying {args.api})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

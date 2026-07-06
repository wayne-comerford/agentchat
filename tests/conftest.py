"""Shared pytest fixtures for the agentchat test suite.

The tests spin up a real `agentchat serve` process against a throwaway
`AGENTCHAT_HOME` on an ephemeral port and drive it over HTTP, exactly the
way a real peer would. This exercises the full stack — routing, auth,
SQLite, SSE — instead of poking at internal functions.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    """Tiny HTTP client for the agentchat API (stdlib only)."""

    def __init__(self, base_url: str, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        token: str | None = ...,  # type: ignore[assignment]
        raw: bool = False,
    ):
        url = self.base_url + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        tok = self.token if token is ... else token
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            payload = e.read()
            status = e.code
        if raw:
            return status, payload
        parsed = json.loads(payload) if payload else None
        return status, parsed

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body=body, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    def upload(self, path, filename: str, mime: str, content: bytes, token=None):
        """multipart/form-data POST with a single file part."""
        boundary = "----pytest-upload-boundary-XYZ123"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
        url = self.base_url + path
        tok = self.token if token is None else token
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            payload = e.read()
            status = e.code
        return status, (json.loads(payload) if payload else None)


@pytest.fixture
def server(tmp_path):
    """Launch a real agentchat server for the test."""
    home = tmp_path / "agentchat-home"
    port = _free_port()
    env = dict(os.environ)
    env["AGENTCHAT_HOME"] = str(home)
    env["LOGIN_RATE_LIMIT"] = "0"  # disable the auth rate limiter for tests
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentchat", "serve", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"

    # Wait for readiness.
    deadline = time.time() + 15
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode() if proc.stdout else ""
            raise RuntimeError(f"server exited early:\n{out}")
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=1) as r:
                if r.status == 200:
                    ready = True
                    break
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.2)
    if not ready:
        proc.terminate()
        out = proc.stdout.read().decode() if proc.stdout else ""
        raise RuntimeError(f"server did not become ready:\n{out}")

    yield {"base_url": base_url, "home": home, "proc": proc}

    proc.terminate()
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)


@pytest.fixture
def api(server):
    """An unauthenticated Client pointed at the session server."""
    return Client(server["base_url"])


_counter = {"n": 0}


@pytest.fixture
def register(server):
    """Factory that registers a fresh user+workspace and returns an
    authenticated Client plus the raw registration payload.

    Usernames/workspaces are unique per call so tests don't collide in the
    shared (session-scoped) database.
    """
    created: list[Client] = []

    def _make(prefix: str = "u"):
        _counter["n"] += 1
        uid = f"{prefix}_{os.getpid()}_{_counter['n']}"
        body = {
            "username": uid,
            "password": "test-password-123",
            "workspace_name": f"ws-{uid}",
        }
        c = Client(server["base_url"])
        status, payload = c.post("/v1/auth/register", body=body)
        assert status == 201, f"register failed: {status} {payload}"
        c.token = payload["token"]
        c.username = uid  # type: ignore[attr-defined]
        c.password = body["password"]  # type: ignore[attr-defined]
        created.append(c)
        return c, payload

    return _make

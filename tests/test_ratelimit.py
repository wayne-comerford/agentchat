"""The auth rate limiter is env-configurable (LOGIN_RATE_LIMIT) and enforced.

This spins up its own server with a low limit, separate from the shared
session server (which disables the limiter so the rest of the suite runs).
"""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import Client

REPO_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def limited_server(tmp_path):
    port = _free_port()
    env = dict(os.environ)
    env["AGENTCHAT_HOME"] = str(tmp_path)
    env["LOGIN_RATE_LIMIT"] = "3"
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentchat", "serve", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=1) as r:
                if r.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    yield base_url
    proc.terminate()
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)


def test_rate_limit_enforced(limited_server):
    c = Client(limited_server)
    statuses = []
    for i in range(5):
        status, _ = c.post(
            "/v1/auth/login",
            body={"username": f"nobody{i}", "password": "whatever-123"},
        )
        statuses.append(status)
    # First 3 attempts go through (401 invalid creds); the 4th+ are limited (429).
    assert statuses[:3] == [401, 401, 401]
    assert 429 in statuses[3:]

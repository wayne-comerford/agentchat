"""Webhook tests: subscribe, signature, retry/backoff, dedupe, unsubscribe.

Mirrors the conftest fixture pattern in tests/conftest.py — spin up a real
`agentchat serve` and drive it over HTTP, plus a thread-pool of httptest
sinks to capture deliveries.

stdlib only.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import socketserver
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

import pytest


class _Sink:
    """Threaded HTTP sink that captures POST bodies + headers."""

    def __init__(self, status: int = 200, fail_until: int = 0):
        self.captured: list[dict[str, Any]] = []
        self.status = status
        self.fail_until = fail_until
        self._hit_count = 0
        self._lock = threading.Lock()

    def make_handler(self):
        sink = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                with sink._lock:
                    sink._hit_count += 1
                    hit_no = sink._hit_count
                n = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(n)
                with sink._lock:
                    sink.captured.append({
                        "path": self.path,
                        "headers": {k: v for k, v in self.headers.items()},
                        "body": body,
                    })
                if hit_no <= sink.fail_until:
                    self.send_response(500)
                else:
                    self.send_response(sink.status)
                self.end_headers()
                self.wfile.write(b"OK")

        return H

    @property
    def hits(self) -> int:
        with self._lock:
            return self._hit_count


class _SinkServer:
    """Wrapper around a threaded HTTP server with a sink."""

    def __init__(self, port: int, status: int = 200, fail_until: int = 0):
        self.sink = _Sink(status=status, fail_until=fail_until)
        self.port = port
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        try:
            self.server = socketserver.ThreadingTCPServer(
                ("127.0.0.1", port), self.sink.make_handler()
            )
        except OSError as e:
            raise RuntimeError(f"SinkServer bind failed on port {port}: {e}") from e
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/sink"


def _post(api: str, path: str, body: dict, *, auth_token: str | None = None) -> dict:
    headers = {"content-type": "application/json"}
    if auth_token:
        headers["authorization"] = f"Bearer {auth_token}"
    req = urllib.request.Request(
        api + path,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode(errors="replace")}


def _get(api: str, path: str, *, auth_token: str | None = None) -> dict:
    headers = {}
    if auth_token:
        headers["authorization"] = f"Bearer {auth_token}"
    req = urllib.request.Request(api + path, headers=headers, method="GET")
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode(errors="replace")}


def _delete(api: str, path: str, *, auth_token: str | None = None) -> dict:
    headers = {}
    if auth_token:
        headers["authorization"] = f"Bearer {auth_token}"
    req = urllib.request.Request(api + path, headers=headers, method="DELETE")
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode(errors="replace")}


@pytest.fixture
def sink_factory():
    """Factory for ephemeral sink servers on free ports."""
    created: list[_SinkServer] = []
    # Use a real random base port to dodge TIME_WAIT collisions within one run.
    import socket as _s
    with _s.socket() as _tmp:
        _tmp.bind(("127.0.0.1", 0))
        port_cursor = [_tmp.getsockname()[1]]

    def _make(status: int = 200, fail_until: int = 0) -> _SinkServer:
        port_cursor[0] += 1
        srv = _SinkServer(port_cursor[0], status=status, fail_until=fail_until)
        created.append(srv)
        return srv

    yield _make

    for s in created:
        s.stop()


def _register_login(api: str) -> dict:
    """Register + log in a fresh user. Returns token."""
    uname = f"wh_test_{uuid.uuid4().hex[:8]}"
    pwd = "TestPassword-2026"
    ws = f"wh-{uuid.uuid4().hex[:6]}"
    _post(api, "/v1/auth/register",
          {"username": uname, "password": pwd, "workspace_name": ws})
    login = _post(api, "/v1/auth/login",
                  {"username": uname, "password": pwd, "workspace": ws})
    token = login.get("token")
    assert token, f"login failed: {login}"
    return {"token": token, "agent_name": uname, "username": uname}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_subscribe_returns_secret_once(server, sink_factory):
    api = server["base_url"]
    res = _register_login(api)
    sink = sink_factory()
    sub = _post(api, "/v1/webhooks/subscribe",
                {"topic": "message.posted", "target_url": sink.url},
                auth_token=res["token"])
    assert "_error" not in sub, f"subscribe errored: {sub}"
    assert "secret" in sub and len(sub["secret"]) >= 32
    assert sub["topic"] == "message.posted"
    assert sub["target_url"] == sink.url


def test_subscribe_rejects_unknown_topic(server, sink_factory):
    api = server["base_url"]
    res = _register_login(api)
    sink = sink_factory()
    sub = _post(api, "/v1/webhooks/subscribe",
                {"topic": "nope.unknown", "target_url": sink.url},
                auth_token=res["token"])
    assert sub.get("_error") == 400


def test_subscribe_rejects_bad_url(server, sink_factory):
    api = server["base_url"]
    res = _register_login(api)
    sub = _post(api, "/v1/webhooks/subscribe",
                {"topic": "message.posted", "target_url": "not-a-url"},
                auth_token=res["token"])
    assert sub.get("_error") == 400


def test_subscribe_duplicate_rejected(server, sink_factory):
    api = server["base_url"]
    res = _register_login(api)
    sink = sink_factory()
    print(f"\n>>> test port: {sink.port}, target_url: {sink.url}")
    first = _post(api, "/v1/webhooks/subscribe",
                  {"topic": "message.posted", "target_url": sink.url},
                  auth_token=res["token"])
    print(f">>> first sub: {first}")
    assert "_error" not in first, f"first sub errored: {first}"
    again = _post(api, "/v1/webhooks/subscribe",
                  {"topic": "message.posted", "target_url": sink.url},
                  auth_token=res["token"])
    print(f">>> again sub: {again}")
    assert again.get("_error") == 409, f"second sub did not 409: {again}"


def test_list_subscriptions_masks_secret(server, sink_factory):
    api = server["base_url"]
    res = _register_login(api)
    sink = sink_factory()
    sub = _post(api, "/v1/webhooks/subscribe",
                {"topic": "message.posted", "target_url": sink.url},
                auth_token=res["token"])
    assert "_error" not in sub, f"subscribe errored: {sub}"
    secret = sub["secret"]
    listed = _get(api, "/v1/webhooks/subscriptions", auth_token=res["token"])
    assert "subscriptions" in listed
    # The newly created one — match by id
    s = next((x for x in listed["subscriptions"] if x["id"] == sub["id"]), None)
    assert s is not None, f"new sub not in list: {listed}"
    assert s["secret_preview"].startswith("***")
    assert "secret" not in s
    # Plaintext never appears anywhere
    flat = json.dumps(listed)
    assert secret not in flat


def test_unsubscribe_removes_subscription(server, sink_factory):
    api = server["base_url"]
    res = _register_login(api)
    sink = sink_factory()
    sub = _post(api, "/v1/webhooks/subscribe",
                {"topic": "message.posted", "target_url": sink.url},
                auth_token=res["token"])
    assert "_error" not in sub, f"subscribe errored: {sub}"
    sub_id = sub["id"]
    deleted = _delete(api, f"/v1/webhooks/subscriptions/{sub_id}", auth_token=res["token"])
    assert deleted.get("deleted") == sub_id
    again = _delete(api, f"/v1/webhooks/subscriptions/{sub_id}", auth_token=res["token"])
    assert again.get("_error") == 404


def test_message_post_triggers_webhook_with_valid_signature(server, sink_factory):
    api = server["base_url"]
    res = _register_login(api)
    sink = sink_factory()
    sub = _post(api, "/v1/webhooks/subscribe",
                {"topic": "message.posted", "target_url": sink.url},
                auth_token=res["token"])
    assert "_error" not in sub, f"subscribe errored: {sub}"
    secret = sub["secret"]

    thread_id = f"wh-test-{uuid.uuid4().hex[:8]}"
    _post(api, "/v1/threads",
          {"id": thread_id, "name": "wh-test", "members": [res["agent_name"]]},
          auth_token=res["token"])
    msg = _post(api, f"/v1/threads/{thread_id}/messages", {"body": "hello"},
                auth_token=res["token"])
    assert "message" in msg

    deadline = time.time() + 8
    while time.time() < deadline and sink.sink.hits == 0:
        time.sleep(0.2)
    assert sink.sink.hits >= 1

    captured = sink.sink.captured[-1]
    body = captured["body"]
    hdr = {k.lower(): v for k, v in captured["headers"].items()}
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert hdr.get("x-agentchat-signature") == expected
    assert hdr.get("x-agentchat-topic") == "message.posted"
    assert hdr.get("x-agentchat-event-id") is not None


def test_failing_target_is_retried_with_backoff(server, sink_factory):
    api = server["base_url"]
    res = _register_login(api)
    sink = sink_factory(status=200, fail_until=2)  # first 2 hits = 500, then 200
    sub = _post(api, "/v1/webhooks/subscribe",
                {"topic": "message.posted", "target_url": sink.url},
                auth_token=res["token"])
    assert "_error" not in sub, f"subscribe errored: {sub}"
    thread_id = f"wh-retry-{uuid.uuid4().hex[:8]}"
    _post(api, "/v1/threads",
          {"id": thread_id, "name": "wh-retry", "members": [res["agent_name"]]},
          auth_token=res["token"])
    msg = _post(api, f"/v1/threads/{thread_id}/messages", {"body": "retry me"},
                auth_token=res["token"])
    assert "message" in msg

    # Backoff: 1s, 5s, 30s. First 2 fail → 3rd should succeed at t≈6s.
    deadline = time.time() + 12
    while time.time() < deadline and sink.sink.hits < 3:
        time.sleep(0.3)
    assert sink.sink.hits >= 3, f"expected >=3 hits, got {sink.sink.hits}"


def test_thread_created_event_fires(server, sink_factory):
    api = server["base_url"]
    res = _register_login(api)
    sink = sink_factory()
    sub = _post(api, "/v1/webhooks/subscribe",
                {"topic": "thread.created", "target_url": sink.url},
                auth_token=res["token"])
    assert "_error" not in sub, f"subscribe errored: {sub}"
    thread_id = f"wh-thcreated-{uuid.uuid4().hex[:8]}"
    _post(api, "/v1/threads",
          {"id": thread_id, "name": "TC", "members": [res["agent_name"]]},
          auth_token=res["token"])
    deadline = time.time() + 8
    while time.time() < deadline and sink.sink.hits == 0:
        time.sleep(0.2)
    assert sink.sink.hits >= 1


def test_deliveries_endpoint_returns_records(server, sink_factory):
    api = server["base_url"]
    res = _register_login(api)
    sink = sink_factory()
    sub = _post(api, "/v1/webhooks/subscribe",
                {"topic": "message.posted", "target_url": sink.url},
                auth_token=res["token"])
    assert "_error" not in sub, f"subscribe errored: {sub}"
    thread_id = f"wh-listing-{uuid.uuid4().hex[:8]}"
    _post(api, "/v1/threads",
          {"id": thread_id, "name": "WL", "members": [res["agent_name"]]},
          auth_token=res["token"])
    msg = _post(api, f"/v1/threads/{thread_id}/messages", {"body": "hi"},
                auth_token=res["token"])
    assert "message" in msg
    deadline = time.time() + 8
    while time.time() < deadline and sink.sink.hits == 0:
        time.sleep(0.2)
    listed = _get(api, f"/v1/webhooks/deliveries?topic=message.posted",
                  auth_token=res["token"])
    assert "deliveries" in listed
    # Our delivery should be present and delivered
    mine = [d for d in listed["deliveries"] if d["target_url"] == sink.url]
    assert mine, f"no delivery for our sink in {listed}"
    assert mine[0]["delivered_at"] is not None
    assert mine[0]["failed_at"] is None


def test_secret_never_leaked_via_listings(server, sink_factory):
    api = server["base_url"]
    res = _register_login(api)
    sink = sink_factory()
    sub = _post(api, "/v1/webhooks/subscribe",
                {"topic": "message.posted", "target_url": sink.url},
                auth_token=res["token"])
    assert "_error" not in sub, f"subscribe errored: {sub}"
    secret = sub["secret"]
    thread_id = f"wh-sec-{uuid.uuid4().hex[:8]}"
    _post(api, "/v1/threads",
          {"id": thread_id, "name": "WS", "members": [res["agent_name"]]},
          auth_token=res["token"])
    _post(api, f"/v1/threads/{thread_id}/messages", {"body": "leak?"},
          auth_token=res["token"])
    deadline = time.time() + 6
    while time.time() < deadline and sink.sink.hits == 0:
        time.sleep(0.2)
    # Listing endpoints must never expose the plaintext secret
    subs = _get(api, "/v1/webhooks/subscriptions", auth_token=res["token"])
    assert secret not in json.dumps(subs)
    dels = _get(api, "/v1/webhooks/deliveries?topic=message.posted",
                auth_token=res["token"])
    assert secret not in json.dumps(dels)

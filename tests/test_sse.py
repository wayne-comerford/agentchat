"""Server-Sent Events stream for live thread updates."""
from __future__ import annotations

import http.client
import socket
import threading
import time
import urllib.parse


def _host_port(base_url):
    p = urllib.parse.urlparse(base_url)
    return p.hostname, p.port


def test_sse_requires_membership(register, server):
    owner, _ = register("sseowner")
    intruder, _ = register("sseintruder")
    tid = f"sse-{owner.username}"
    owner.post("/v1/threads", body={"id": tid, "members": [owner.username]})

    host, port = _host_port(server["base_url"])
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", f"/v1/threads/{tid}/events", headers={"Authorization": f"Bearer {intruder.token}"})
    resp = conn.getresponse()
    assert resp.status == 403
    conn.close()


def test_sse_delivers_new_message(register, server):
    owner, _ = register("sselive")
    tid = f"sselive-{owner.username}"
    owner.post("/v1/threads", body={"id": tid, "members": [owner.username]})

    host, port = _host_port(server["base_url"])
    conn = http.client.HTTPConnection(host, port, timeout=8)
    conn.request("GET", f"/v1/threads/{tid}/events?since=0", headers={"Authorization": f"Bearer {owner.token}"})
    resp = conn.getresponse()
    assert resp.status == 200
    assert "text/event-stream" in resp.getheader("Content-Type", "")

    unique = f"live-payload-{owner.username}"

    # Post a message shortly after the stream is established.
    def _post():
        time.sleep(0.5)
        owner.post(f"/v1/threads/{tid}/messages", body={"body": unique})

    threading.Thread(target=_post, daemon=True).start()

    # Read the stream until the payload shows up or we time out.
    found = False
    deadline = time.time() + 7
    buf = b""
    while time.time() < deadline:
        try:
            chunk = resp.read(256)
        except (socket.timeout, TimeoutError):
            break
        if not chunk:
            break
        buf += chunk
        if unique.encode() in buf and b"event: message" in buf:
            found = True
            break
    conn.close()
    assert found, f"did not receive live SSE message; buffer was: {buf!r}"

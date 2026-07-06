"""Structured audit log tests (v1.1.2).

The audit_log table is the canonical event log — registration, login,
file uploads, webhook subscribes all funnel through `audit_log()`.
`GET /v1/audit` exposes a filtered view (actor, action, target_type,
target_id, since/until, limit).

Old `/v1/audit` (admin threads view) is now `/v1/threads/all`.
"""
from __future__ import annotations

import time
import urllib.request as ur

import pytest


def _now_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def test_register_records_audit(server, register):
    client, _ = register("aud")
    status, body = client.get("/v1/audit?action=register")
    assert status == 200
    actions = [e["action"] for e in body["entries"]]
    assert "register" in actions
    # Find our specific actor
    ours = [e for e in body["entries"]
            if e["actor"] == client.username and e["action"] == "register"]
    assert ours, f"no register entry for {client.username}"
    e = ours[0]
    assert e["target_type"] == "user"
    assert e["target_id"] == client.username
    assert e["metadata"]["workspace"]


def test_login_records_audit(server, register):
    client, _ = register("login")
    # Re-login through password
    import urllib.request as ur2, urllib.error, json
    url = server["base_url"] + "/v1/auth/login"
    body = json.dumps({"username": client.username, "password": client.password}).encode()
    req = ur2.Request(url, data=body, method="POST",
                      headers={"Content-Type": "application/json"})
    ur2.urlopen(req, timeout=10).read()
    status, payload = client.get("/v1/audit?action=login&actor=" + client.username)
    assert status == 200
    actors_actions = [(e["actor"], e["action"]) for e in payload["entries"]]
    assert (client.username, "login") in actors_actions


def test_file_upload_audit(server, register):
    client, _ = register("flaud")
    client.upload("/v1/files", "aud.txt", "text/plain", b"audited")
    status, body = client.get("/v1/audit?action=file_upload")
    assert status == 200
    entries = [e for e in body["entries"] if e["actor"] == client.username]
    assert entries
    e = entries[0]
    assert e["action"] == "file_upload"
    assert e["target_type"] == "file"
    assert e["metadata"]["mime_type"] == "text/plain"
    assert e["metadata"]["deduped"] is False


def test_webhook_subscribe_audit(server, register):
    client, _ = register("whaud")
    # Note: API uses dotted topic names ("message.posted", "thread.created")
    status, payload = client.post(
        "/v1/webhooks/subscribe",
        body={"topic": "message.posted", "target_url": "https://example.com/wh"},
    )
    assert status == 201
    sub_id = payload["id"]
    status, body = client.get(f"/v1/audit?action=webhook_subscribe&target_id={sub_id}")
    assert status == 200
    matches = [e for e in body["entries"]
               if e["actor"] == client.username and e["target_id"] == str(sub_id)]
    assert matches
    assert matches[0]["metadata"]["topic"] == "message.posted"


def test_filter_by_actor(server, register):
    a, _ = register("fa")
    b, _ = register("fb")
    a.upload("/v1/files", "a.txt", "text/plain", b"a")
    b.upload("/v1/files", "b.txt", "text/plain", b"b")
    status, body = a.get(f"/v1/audit?actor={b.username}&action=file_upload")
    assert status == 200
    assert all(e["actor"] == b.username for e in body["entries"])
    status, body = a.get(f"/v1/audit?actor={a.username}&action=file_upload")
    assert all(e["actor"] == a.username for e in body["entries"])


def test_filter_by_since(server, register):
    client, _ = register("since")
    client.upload("/v1/files", "before.txt", "text/plain", b"x")
    future = "2099-01-01T00:00:00+00:00"
    status, body = client.get(f"/v1/audit?since={future}")
    assert status == 200
    assert body["count"] == 0
    status, body = client.get(f"/v1/audit?since=2000-01-01T00:00:00+00:00")
    assert body["count"] > 0


def test_unauth_audit_rejected(server):
    import urllib.request as ur2, urllib.error
    url = server["base_url"] + "/v1/audit"
    with pytest.raises(urllib.error.HTTPError) as exc:
        ur2.urlopen(url, timeout=5)
    assert exc.value.code == 401


def test_old_admin_endpoint_still_works(server, register):
    # /v1/audit_threads = backwards-compatible alias for /v1/threads/all
    client, _ = register("old")
    client.upload("/v1/files", "z.txt", "text/plain", b"z")
    status, body = client.get("/v1/audit_threads")
    assert status == 200
    assert "threads" in body


def test_audit_helper_direct(tmp_path):
    # In-process unit test — confirms write path without HTTP overhead.
    import agentchat, os
    os.environ["AGENTCHAT_HOME"] = str(tmp_path / "audit-home")
    agentchat.db_init()
    rid = agentchat.audit_log(action="search", actor="hb",
                              target_type="search", metadata={"q": "abc"})
    assert isinstance(rid, int) and rid > 0
    entries = agentchat.audit_list(actor="hb", action="search", limit=1)
    assert any(e["id"] == rid for e in entries)


def test_audit_unknown_action_still_logged(tmp_path):
    # Forward-compat: a future action slips through, the read path is consistent.
    import agentchat, os
    os.environ["AGENTCHAT_HOME"] = str(tmp_path / "audit-home2")
    agentchat.db_init()
    rid = agentchat.audit_log(action="future_action_xyz", actor="hb")
    assert rid is not None
    entries = agentchat.audit_list(action="future_action_xyz")
    assert any(e["id"] == rid for e in entries)

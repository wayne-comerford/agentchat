"""File upload / dedupe / download / delete tests (v1.1.x).

Each test registers a fresh user, uploads bytes, and asserts on storage
behaviour. Dedupe is per-content (sha256) — the second upload of the same
bytes must return the original id with `deduped=true`.
"""
from __future__ import annotations

import urllib.error
import urllib.request as ur

import pytest


def test_upload_returns_id(server, register):
    client, _ = register("up")
    status, body = client.upload(
        "/v1/files",
        filename="hello.txt",
        mime="text/plain",
        content=b"hello world",
    )
    assert status == 201, body
    assert body["size_bytes"] == 11
    assert body["filename"] == "hello.txt"
    assert body["deduped"] is False
    assert body["ref_count"] == 1
    assert len(body["sha256"]) == 64


def test_get_meta_returns_record(server, register):
    client, _ = register("meta")
    _, up = client.upload("/v1/files", "x.txt", "text/plain", b"abc")
    fid = up["id"]
    status, meta = client.get(f"/v1/files/{fid}")
    assert status == 200
    assert meta["id"] == fid
    assert meta["size_bytes"] == 3
    assert meta["mime_type"] == "text/plain"


def test_download_returns_same_bytes(server, register):
    client, _ = register("dl")
    payload = b"\x00\x01\x02 hello \x03"
    _, up = client.upload("/v1/files", "binary.bin", "application/octet-stream", payload)
    fid = up["id"]
    # Use raw request to download
    import urllib.request as ur
    url = server["base_url"] + f"/v1/files/{fid}/download"
    req = ur.Request(url, headers={"Authorization": f"Bearer {client.token}"})
    with ur.urlopen(req, timeout=5) as r:
        assert r.status == 200
        assert r.read() == payload
        assert r.headers["Content-Type"] == "application/octet-stream"


def test_dedupe_same_content_returns_existing_id(server, register):
    client, _ = register("dedupe")
    payload = b"identical bytes here"
    _, up1 = client.upload("/v1/files", "first.txt", "text/plain", payload)
    _, up2 = client.upload("/v1/files", "second.txt", "text/plain", payload)
    assert up1["id"] == up2["id"]
    assert up2["deduped"] is True
    assert up2["ref_count"] == 2


def test_dedupe_different_content_gets_new_id(server, register):
    client, _ = register("diff")
    _, a = client.upload("/v1/files", "a.txt", "text/plain", b"aaaa")
    _, b = client.upload("/v1/files", "b.txt", "text/plain", b"bbbb")
    assert a["id"] != b["id"]
    assert a["ref_count"] == 1 and b["ref_count"] == 1


def test_delete_only_owner_can(server, register):
    owner, _ = register("owner")
    other, _ = register("otherz")
    # owner uploads
    _, up = owner.upload("/v1/files", "priv.txt", "text/plain", b"secret")
    fid = up["id"]
    # other can't delete
    status, body = other.delete(f"/v1/files/{fid}")
    assert status == 403


def test_delete_refcount_decrement_then_wipe(server, register):
    client, _ = register("ref")
    payload = b"will-be-deleted"
    _, up1 = client.upload("/v1/files", "f.txt", "text/plain", payload)
    _, up2 = client.upload("/v1/files", "g.txt", "text/plain", payload)
    fid = up1["id"]
    assert up2["ref_count"] == 2
    # First delete: decrements to 1
    status, body = client.delete(f"/v1/files/{fid}")
    assert status == 200 and body["result"] == "decremented"
    # Second delete: actually removes the row
    status, body = client.delete(f"/v1/files/{fid}")
    assert status == 200 and body["result"] == "deleted"
    # Now 404
    status, _ = client.get(f"/v1/files/{fid}")
    assert status == 404


def test_blocked_mime(server, register):
    client, _ = register("mime")
    # Default allowlist does NOT include "application/x-msdownload"
    status, body = client.upload(
        "/v1/files",
        "evil.exe",
        "application/x-msdownload",
        b"\x4d\x5a\x90\x00",
    )
    assert status == 400
    assert "mime" in body["error"].lower()


def test_allowed_mime_glob_image(server, register):
    client, _ = register("img")
    status, body = client.upload(
        "/v1/files", "pic.png", "image/png", b"\x89PNG\r\n\x1a\n fake"
    )
    assert status == 201, body


def test_unauth_upload_rejected(server):
    import urllib.request as ur
    import urllib.error
    url = server["base_url"] + "/v1/files"
    boundary = "----unauth-test"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="x.txt"\r\n'
        "Content-Type: text/plain\r\n\r\nbody\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    req = ur.Request(url, data=body, method="POST",
                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        ur.urlopen(req, timeout=5)
    assert exc.value.code == 401


def test_size_cap_helper():
    # Direct in-process check — the HTTP path is exercised by the session
    # server which uses the default 25 MiB cap (oversized uploads in a
    # real HTTP test would slow the suite down without extra coverage).
    import agentchat
    cap = agentchat._max_upload_bytes()
    assert cap >= 1024
    # An oversized payload gets an error
    bigger = b"x" * (cap + 1)
    res = agentchat.file_upload(
        owner_user_id=0,
        filename="big.bin",
        mime_type="application/octet-stream",
        content=bigger,
    )
    # Either rejected (size) or rejected (mime if defaults were tightened); we
    # only assert it's not a valid upload
    assert "error" in res or res.get("deduped") is False and res["size_bytes"] <= cap

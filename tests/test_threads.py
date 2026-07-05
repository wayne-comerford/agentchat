"""Threads: create, list, membership gating, posting, ack, forgot-password reset end-to-end."""
from __future__ import annotations


def _new_thread(client, members, tid=None, name="Test thread"):
    tid = tid or f"t-{client.username}"
    status, body = client.post(
        "/v1/threads", body={"id": tid, "name": name, "members": members}
    )
    return status, body, tid


def test_create_thread_and_list(register):
    client, _ = register()
    status, body, tid = _new_thread(client, [client.username])
    assert status == 201
    assert body["thread"]["id"] == tid
    assert client.username in body["thread"]["members"]

    status, listing = client.get("/v1/threads")
    assert status == 200
    assert any(t["id"] == tid for t in listing["threads"])


def test_create_thread_rejects_unknown_member(register):
    client, _ = register()
    status, body, _ = _new_thread(client, ["no-such-agent-xyz"])
    assert status == 400
    assert "unknown agent" in body["error"]


def test_create_thread_rejects_bad_id(register):
    client, _ = register()
    status, body = client.post(
        "/v1/threads", body={"id": "bad id with spaces", "members": [client.username]}
    )
    assert status == 400


def test_post_and_read_message(register):
    client, _ = register()
    _, _, tid = _new_thread(client, [client.username])
    status, body = client.post(
        f"/v1/threads/{tid}/messages", body={"body": "hello thread"}
    )
    assert status == 201
    msg_id = body["message"]["msg_id"]
    assert msg_id.startswith("t_")

    status, msgs = client.get(f"/v1/threads/{tid}/messages?limit=5")
    assert status == 200
    assert msgs["latest"] is True
    bodies = [m["body"] for m in msgs["messages"]]
    assert "hello thread" in bodies


def test_post_requires_body(register):
    client, _ = register()
    _, _, tid = _new_thread(client, [client.username])
    status, _ = client.post(f"/v1/threads/{tid}/messages", body={"body": "   "})
    assert status == 400


def test_non_member_cannot_read_or_post(register):
    owner, _ = register("owner")
    intruder, _ = register("intruder")
    _, _, tid = _new_thread(owner, [owner.username], tid=f"private-{owner.username}")

    # Intruder is in a different workspace and not a member.
    status, _ = intruder.get(f"/v1/threads/{tid}/messages")
    assert status == 403
    status, _ = intruder.post(f"/v1/threads/{tid}/messages", body={"body": "sneak"})
    assert status == 403
    status, _ = intruder.get(f"/v1/threads/{tid}")
    assert status == 404  # membership-gated: looks like it doesn't exist


def test_add_member_then_access(register):
    owner, _ = register("owner")
    guest, _ = register("guest")
    _, _, tid = _new_thread(owner, [owner.username], tid=f"shared-{owner.username}")

    status, _ = owner.post(f"/v1/threads/{tid}/members", body={"members": [guest.username]})
    assert status == 200

    # Now the guest can read.
    status, body = guest.get(f"/v1/threads/{tid}")
    assert status == 200
    assert guest.username in body["thread"]["members"]


def test_ack_marks_read(register):
    a, _ = register("sender")
    b, _ = register("reader")
    _, _, tid = _new_thread(a, [a.username, b.username], tid=f"ack-{a.username}")
    _, posted = a.post(f"/v1/threads/{tid}/messages", body={"body": "read me"})
    msg_id = posted["message"]["msg_id"]

    # Reader has one unread.
    _, before = b.get(f"/v1/threads/{tid}")
    assert before["thread"]["unread"] == 1

    status, _ = b.post(f"/v1/messages/{msg_id}/ack")
    assert status == 200

    _, after = b.get(f"/v1/threads/{tid}")
    assert after["thread"]["unread"] == 0


def test_forgot_reset_end_to_end(register, server):
    """Full reset flow: request token via /forgot (read from server.log,
    the documented single-tenant delivery channel), consume via /reset."""
    from conftest import Client

    client, _ = register("resetme")
    status, _ = client.post("/v1/auth/forgot", body={"username": client.username})
    assert status == 200

    # The reset token is logged under the whitelisted PASSWORD_RESET_TOKEN prefix.
    log_path = server["home"] / "server.log"
    token = None
    for line in log_path.read_text().splitlines():
        if "PASSWORD_RESET_TOKEN" in line and f"username={client.username} " in line + " ":
            token = line.split("token=", 1)[1].strip()
    assert token, "reset token not found in server.log"

    new_password = "a-fresh-password-99"
    status, _ = client.post(
        "/v1/auth/reset", body={"token": token, "new_password": new_password}
    )
    assert status == 200

    # Old password no longer works; new one does.
    fresh = Client(server["base_url"])
    status, _ = fresh.post(
        "/v1/auth/login", body={"username": client.username, "password": client.password}
    )
    assert status == 401
    status, body = fresh.post(
        "/v1/auth/login", body={"username": client.username, "password": new_password}
    )
    assert status == 200

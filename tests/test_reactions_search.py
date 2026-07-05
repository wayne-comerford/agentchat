"""Reactions (add/list/remove, idempotent) and cross-thread search."""
from __future__ import annotations

import urllib.parse


def _thread_with_message(client, body_text="searchable message", tid=None):
    tid = tid or f"rx-{client.username}"
    client.post("/v1/threads", body={"id": tid, "name": "rx", "members": [client.username]})
    _, posted = client.post(f"/v1/threads/{tid}/messages", body={"body": body_text})
    return tid, posted["message"]["msg_id"]


def test_reaction_add_list_remove(register):
    client, _ = register()
    _, msg_id = _thread_with_message(client)

    status, body = client.post(f"/v1/messages/{msg_id}/reactions", body={"emoji": "👍"})
    assert status == 200
    assert body["added"] is True
    assert "👍" in body["reactions"]

    # Idempotent re-add.
    status, body = client.post(f"/v1/messages/{msg_id}/reactions", body={"emoji": "👍"})
    assert status == 200
    assert body["added"] is False

    # List.
    status, body = client.get(f"/v1/messages/{msg_id}/reactions")
    assert status == 200
    assert client.username in body["reactions"]["👍"]

    # Remove.
    enc = urllib.parse.quote("👍")
    status, body = client.delete(f"/v1/messages/{msg_id}/reactions?emoji={enc}")
    assert status == 200
    assert body["removed"] is True
    assert body["reactions"] == {}


def test_reaction_requires_emoji(register):
    client, _ = register()
    _, msg_id = _thread_with_message(client)
    status, _ = client.post(f"/v1/messages/{msg_id}/reactions", body={})
    assert status == 400


def test_reaction_on_missing_message(register):
    client, _ = register()
    status, _ = client.post("/v1/messages/t_deadbeef/reactions", body={"emoji": "👍"})
    assert status == 404


def test_non_member_cannot_react(register):
    owner, _ = register("rxowner")
    intruder, _ = register("rxintruder")
    _, msg_id = _thread_with_message(owner, tid=f"rxpriv-{owner.username}")
    status, _ = intruder.post(f"/v1/messages/{msg_id}/reactions", body={"emoji": "👀"})
    assert status == 403


def test_search_finds_message(register):
    client, _ = register()
    unique = f"needle-{client.username}"
    _thread_with_message(client, body_text=f"a haystack with a {unique} in it")
    q = urllib.parse.quote_plus(unique)
    status, body = client.get(f"/v1/search?q={q}")
    assert status == 200
    assert body["count"] >= 1
    assert any(unique in h["body"] for h in body["hits"])


def test_search_requires_query(register):
    client, _ = register()
    status, _ = client.get("/v1/search?q=")
    assert status == 400


def test_search_respects_membership(register):
    owner, _ = register("sowner")
    other, _ = register("sother")
    secret = f"topsecret-{owner.username}"
    _thread_with_message(owner, body_text=f"contains {secret}", tid=f"spriv-{owner.username}")

    # The other user (different workspace, not a member) must not find it.
    q = urllib.parse.quote_plus(secret)
    status, body = other.get(f"/v1/search?q={q}")
    assert status == 200
    assert body["count"] == 0

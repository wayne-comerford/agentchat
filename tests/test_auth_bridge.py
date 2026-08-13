"""
Tests for v1.2.0.dev4 additions:

- Bridge auth routes (login / logout / whoami / identities)
- POST /v1/ui/post signs with the session-identity keypair
- Mention dispatch helpers (extract_mentions, channel_of, build_reply_text)
- DedupeStore idempotency
"""
import json
import os
import pytest

from agentchat.nostr.keys import NostrKeys
from agentchat.web import mention_dispatcher as md
from agentchat.web import nostr_bridge as nb


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def tmp_keys(tmp_path, monkeypatch):
    """Generate three keypairs and write nsec.json + registry.json."""
    home = tmp_path / ".hermes"
    nostr = home / "nostr"
    nostr.mkdir(parents=True)
    monkeypatch.setenv("AGENTCHAT_NOSTR_DIR", str(nostr))

    pairs = {}
    reg = {}
    for name in ("hermes", "chappy", "wayne-observer"):
        kp = NostrKeys.generate()
        (nostr / f"{name}.nsec.json").write_text(json.dumps({
            "private_key_hex": kp.private_key_hex,
            "nsec": kp.nsec,
        }))
        os.chmod(nostr / f"{name}.nsec.json", 0o600)
        pairs[name] = kp
        reg[name] = {
            "public_key_hex": kp.public_key_hex,
            "npub": kp.npub,
        }
    (nostr / "registry.json").write_text(json.dumps(reg))
    return pairs, reg


@pytest.fixture
def bridge_state(tmp_keys):
    pairs, reg = tmp_keys
    state = nb.BridgeState(nb.DEFAULT_CONFIG)
    state.registry = reg
    return state, pairs


# --------------------------------------------------------------------------- #
# Auth endpoints
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_login_sets_cookie_and_returns_npub(aiohttp_client, tmp_keys):
    pairs, reg = tmp_keys
    app = nb.make_app(nb.DEFAULT_CONFIG)
    app["state"].registry = reg
    client = await aiohttp_client(app)
    resp = await client.post("/v1/auth/login", json={"name": "hermes"})
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["name"] == "hermes"
    assert data["npub"] == pairs["hermes"].npub


@pytest.mark.asyncio
async def test_login_unknown_name_returns_404(aiohttp_client, tmp_keys):
    _, reg = tmp_keys
    app = nb.make_app(nb.DEFAULT_CONFIG)
    app["state"].registry = reg
    client = await aiohttp_client(app)
    resp = await client.post("/v1/auth/login", json={"name": "ghost"})
    assert resp.status == 404


@pytest.mark.asyncio
async def test_login_requires_name(aiohttp_client, tmp_keys):
    _, reg = tmp_keys
    app = nb.make_app(nb.DEFAULT_CONFIG)
    app["state"].registry = reg
    client = await aiohttp_client(app)
    resp = await client.post("/v1/auth/login", json={})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_whoami_anonymous(aiohttp_client, tmp_keys):
    _, reg = tmp_keys
    app = nb.make_app(nb.DEFAULT_CONFIG)
    app["state"].registry = reg
    client = await aiohttp_client(app)
    resp = await client.get("/v1/auth/whoami")
    data = await resp.json()
    assert data["logged_in"] is False


@pytest.mark.asyncio
async def test_whoami_authenticated(aiohttp_client, tmp_keys):
    pairs, reg = tmp_keys
    app = nb.make_app(nb.DEFAULT_CONFIG)
    app["state"].registry = reg
    client = await aiohttp_client(app)
    await client.post("/v1/auth/login", json={"name": "chappy"})
    resp = await client.get("/v1/auth/whoami")
    data = await resp.json()
    assert data["logged_in"] is True
    assert data["name"] == "chappy"
    assert data["npub"] == pairs["chappy"].npub


@pytest.mark.asyncio
async def test_logout_clears_session(aiohttp_client, tmp_keys):
    _, reg = tmp_keys
    app = nb.make_app(nb.DEFAULT_CONFIG)
    app["state"].registry = reg
    client = await aiohttp_client(app)
    await client.post("/v1/auth/login", json={"name": "chappy"})
    await client.post("/v1/auth/logout")
    resp = await client.get("/v1/auth/whoami")
    data = await resp.json()
    assert data["logged_in"] is False


@pytest.mark.asyncio
async def test_identities_lists_local_agents(aiohttp_client, tmp_keys):
    _, reg = tmp_keys
    app = nb.make_app(nb.DEFAULT_CONFIG)
    app["state"].registry = reg
    client = await aiohttp_client(app)
    resp = await client.get("/v1/auth/identities")
    data = await resp.json()
    names = {d["name"] for d in data}
    assert names == {"hermes", "chappy", "wayne-observer"}


# --------------------------------------------------------------------------- #
# POST signs as the session identity
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_post_signs_as_session_identity(aiohttp_client, tmp_keys, monkeypatch):
    """Posting while logged in as 'chappy' must publish under chappy's key."""
    pairs, reg = tmp_keys

    captured = {}

    class FakePool:
        def __init__(self, relays, keys):
            captured["signed_with"] = keys.public_key_hex
            captured["relays"] = relays

        def stop_listen(self): pass
        def publish_channel_message(self, channel_id, content, mentions=None):
            captured["channel"] = channel_id
            captured["content"] = content
            captured["mentions"] = mentions or []
            return "fake_event_id"

    monkeypatch.setattr(nb, "RelayPool", FakePool)

    app = nb.make_app(nb.DEFAULT_CONFIG)
    app["state"].registry = reg
    client = await aiohttp_client(app)
    await client.post("/v1/auth/login", json={"name": "chappy"})

    resp = await client.post("/v1/ui/post", json={
        "channel": "general",
        "content": "hello from chappy",
        "mentions": [],
    })
    assert resp.status == 200
    data = await resp.json()
    assert captured["signed_with"] == pairs["chappy"].public_key_hex
    assert captured["channel"] == "general"
    assert captured["content"] == "hello from chappy"
    assert data["signed_by"] == pairs["chappy"].npub


@pytest.mark.asyncio
async def test_post_falls_back_to_default_when_no_session(aiohttp_client, tmp_keys, monkeypatch):
    """No cookie => use the bridge-default identity (hermes)."""
    pairs, reg = tmp_keys

    captured = {}

    class FakePool:
        def __init__(self, relays, keys):
            captured["signed_with"] = keys.public_key_hex

        def stop_listen(self): pass
        def publish_channel_message(self, channel_id, content, mentions=None):
            return "fake_event_id"

    monkeypatch.setattr(nb, "RelayPool", FakePool)

    app = nb.make_app(nb.DEFAULT_CONFIG)
    app["state"].registry = reg
    client = await aiohttp_client(app)

    resp = await client.post("/v1/ui/post", json={"channel": "general", "content": "hi"})
    assert resp.status == 200
    assert captured["signed_with"] == pairs["hermes"].public_key_hex
    data = await resp.json()
    assert data["signed_by"] == pairs["hermes"].npub


@pytest.mark.asyncio
async def test_post_with_invalid_session_falls_back_to_default(aiohttp_client, tmp_keys, monkeypatch):
    """Cookie set to nonexistent name => falls back to default."""
    pairs, reg = tmp_keys

    captured = {}

    class FakePool:
        def __init__(self, relays, keys):
            captured["signed_with"] = keys.public_key_hex

        def stop_listen(self): pass
        def publish_channel_message(self, channel_id, content, mentions=None):
            return "fake_event_id"

    monkeypatch.setattr(nb, "RelayPool", FakePool)

    app = nb.make_app(nb.DEFAULT_CONFIG)
    app["state"].registry = reg
    client = await aiohttp_client(app)
    # Manually set a bogus cookie
    client.session.cookie_jar.update_cookies({"agentchat_session": "ghost"})
    resp = await client.post("/v1/ui/post", json={"channel": "general", "content": "hi"})
    assert resp.status == 200
    assert captured["signed_with"] == pairs["hermes"].public_key_hex


@pytest.mark.asyncio
async def test_post_extracts_mentions_in_payload(aiohttp_client, tmp_keys, monkeypatch):
    pairs, reg = tmp_keys
    captured = {}

    class FakePool:
        def __init__(self, relays, keys): pass
        def stop_listen(self): pass
        def publish_channel_message(self, channel_id, content, mentions=None):
            captured["mentions"] = mentions or []
            return "fake_event_id"

    monkeypatch.setattr(nb, "RelayPool", FakePool)

    app = nb.make_app(nb.DEFAULT_CONFIG)
    app["state"].registry = reg
    client = await aiohttp_client(app)

    await client.post("/v1/ui/post", json={
        "channel": "general",
        "content": "@chappy hi",
        "mentions": [pairs["chappy"].public_key_hex],
    })
    assert captured["mentions"] == [pairs["chappy"].public_key_hex]


# --------------------------------------------------------------------------- #
# Mention dispatcher helpers
# --------------------------------------------------------------------------- #

def test_extract_mentions_dedupes_and_casefolds():
    ev = {
        "tags": [
            ["p", "ABCD"],
            ["p", "abcd"],
            ["h", "general"],
            ["p", "1234"],
        ]
    }
    assert md._extract_mentions(ev) == ["abcd", "1234"]


def test_extract_mentions_handles_missing_tags():
    assert md._extract_mentions({}) == []
    assert md._extract_mentions({"tags": None}) == []


def test_channel_of_reads_h_tag():
    ev = {"tags": [["h", "general"], ["p", "x"]]}
    assert md._channel_of(ev) == "general"


def test_channel_of_returns_none_for_other_kinds():
    assert md._channel_of({}) is None
    assert md._channel_of({"tags": [["p", "x"]]}) is None


def test_build_reply_text_truncates_long_content():
    pk = "ab" * 32
    long = "x" * 500
    out = md.build_reply_text(pk, long)
    assert out.startswith("@abab…abab heard you:")
    assert len(out) < 200


def test_build_reply_text_preserves_short():
    pk = "ab" * 32
    out = md.build_reply_text(pk, "hi")
    assert out == "@abab…abab heard you: hi"


def test_short_pubkey_format():
    assert md._short_pubkey("abcdef0123456789") == "abcd…6789"
    assert md._short_pubkey("") == "anon"


def test_reverse_registry_maps_pubkey_to_name():
    reg = {"alice": {"public_key_hex": "AA", "npub": "npub_a"}, "bob": {"public_key_hex": "BB", "npub": "npub_b"}}
    rem = md._reverse_registry(reg)
    assert rem["AA"] == "alice"
    assert rem["BB"] == "bob"


# --------------------------------------------------------------------------- #
# Dedupe store
# --------------------------------------------------------------------------- #

def test_dedupe_persists_across_instances(tmp_path):
    p = tmp_path / "dedupe.json"
    s1 = md.DedupeStore(p)
    assert not s1.seen("evt1:pub1")
    s1.mark("evt1:pub1")
    assert s1.seen("evt1:pub1")

    s2 = md.DedupeStore(p)
    assert s2.seen("evt1:pub1")
    assert not s2.seen("evt2:pub1")


def test_dedupe_trims_to_5000_entries(tmp_path):
    p = tmp_path / "dedupe.json"
    s = md.DedupeStore(p)
    for i in range(5010):
        s.mark(f"e{i}:p{i}")
    assert len(s._seen) <= 5000
    assert s.seen("e5009:p5009")
    # Oldest entries dropped
    assert not s.seen("e0:p0")

"""
Tests for agentchat.nostr.client and agentchat.nostr.server (v1.2).

Scope:
- Pure-Python primitives in `server.py`: create_challenge, create_auth_event,
  verify_auth_event, AuthRateLimiter. All these run without a network.
- RelayPool unit behavior: subscription registry, publish-before-start guard,
  URL validation. We do NOT spin up a live relay here — that's covered by
  the live-interop test (separate suite, requires a local Buzz relay).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from pynostr.key import PrivateKey

from agentchat.nostr.client import (
    IncomingEvent,
    RelayEndpoint,
    RelayPool,
    load_pool,
)
from agentchat.nostr.keys import NostrKeys
from agentchat.nostr.server import (
    AuthRateLimiter,
    DEFAULT_AUTH_MAX_AGE_SECONDS,
    create_auth_event,
    create_challenge,
    verify_auth_event,
)


# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def fresh_keys() -> NostrKeys:
    """Fresh in-memory keypair (not written to disk)."""
    return NostrKeys.generate()


@pytest.fixture
def keys_on_disk(tmp_path: Path) -> Path:
    """Write a fresh keypair to chmod-600 JSON in tmp_path."""
    import json, os, stat
    keys = NostrKeys.generate()
    p = tmp_path / "agentchat-test.nostr.json"
    p.write_text(json.dumps({"private_key_hex": keys.private_key_hex, "nsec": keys.nsec}))
    os.chmod(p, 0o600)
    return p


@pytest.fixture
def challenge() -> str:
    return "0123456789abcdef0123456789abcdef"


@pytest.fixture
def relay_url() -> str:
    return "wss://relay.example.com"


# --------------------------------------------------------------------------- #
# RelayEndpoint validation
# --------------------------------------------------------------------------- #

class TestRelayEndpoint:
    def test_accepts_ws(self):
        ep = RelayEndpoint(url="ws://localhost:3000")
        assert ep.url == "ws://localhost:3000"

    def test_accepts_wss(self):
        ep = RelayEndpoint(url="wss://relay.example.com")
        assert ep.url == "wss://relay.example.com"

    def test_rejects_http(self):
        with pytest.raises(ValueError, match="ws:// or wss://"):
            RelayEndpoint(url="http://relay.example.com")

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            RelayEndpoint(url="not a url")


# --------------------------------------------------------------------------- #
# RelayPool construction + subscription registry
# --------------------------------------------------------------------------- #

class TestRelayPoolUnit:
    def test_requires_at_least_one_endpoint(self, fresh_keys):
        with pytest.raises(ValueError, match="at least one RelayEndpoint"):
            RelayPool([], fresh_keys)

    def test_initial_state(self, fresh_keys):
        pool = RelayPool([RelayEndpoint("ws://localhost:3000")], fresh_keys)
        assert pool.started is False
        assert pool.incoming.qsize() == 0

    def test_subscribe_channel_returns_id(self, fresh_keys):
        pool = RelayPool([RelayEndpoint("ws://localhost:3000")], fresh_keys)
        sub_id = pool.subscribe_channel("dinner-room")
        assert sub_id.startswith("agentchat-sub-")
        assert isinstance(sub_id, str)

    def test_subscribe_channel_increments_id(self, fresh_keys):
        pool = RelayPool([RelayEndpoint("ws://localhost:3000")], fresh_keys)
        a = pool.subscribe_channel("room-a")
        b = pool.subscribe_channel("room-b")
        assert a != b

    def test_subscribe_channel_with_options(self, fresh_keys):
        # Should not raise with since/limit
        sub_id = pool_subscribe_with_opts(fresh_keys, "room", since=100, limit=50)
        assert sub_id

    def test_unsubscribe_returns_true_for_known(self, fresh_keys):
        pool = RelayPool([RelayEndpoint("ws://localhost:3000")], fresh_keys)
        sub_id = pool.subscribe_channel("room")
        assert pool.unsubscribe(sub_id) is True

    def test_unsubscribe_returns_false_for_unknown(self, fresh_keys):
        pool = RelayPool([RelayEndpoint("ws://localhost:3000")], fresh_keys)
        assert pool.unsubscribe("agentchat-sub-9999") is False

    def test_publish_before_endpoints_init_raises(self, fresh_keys):
        # publish_channel_message needs _endpoints set — verify guard
        # (in normal flow, _endpoints is set in __init__; this simulates a
        # malformed RelayPool object).
        pool = RelayPool.__new__(RelayPool)
        pool._endpoints = []
        pool._keys = fresh_keys
        with pytest.raises(RuntimeError, match="no relay endpoints"):
            pool.publish_channel_message("room", "hello")


def pool_subscribe_with_opts(keys, room, **kw):
    pool = RelayPool([RelayEndpoint("ws://localhost:3000")], keys)
    return pool.subscribe_channel(room, **kw)


# --------------------------------------------------------------------------- #
# IncomingEvent dataclass
# --------------------------------------------------------------------------- #

class TestIncomingEvent:
    def test_event_id_property(self):
        from pynostr.event import Event
        ev = Event(kind=9, content="x", pubkey="", created_at=0, tags=[])
        incoming = IncomingEvent(subscription_id="sub-1", event=ev, relay_url="ws://x")
        # id will be the empty-string id since pubkey is empty
        assert hasattr(incoming, "event_id")
        assert incoming.kind == 9
        assert incoming.content == "x"


# --------------------------------------------------------------------------- #
# load_pool — disk-backed keypair + pool construction
# --------------------------------------------------------------------------- #

class TestLoadPool:
    def test_loads_from_disk_and_builds_pool(self, keys_on_disk):
        pool = load_pool(["ws://localhost:3000"], keys_on_disk)
        assert pool.started is False

    def test_refuses_world_readable_keyfile(self, tmp_path):
        import json, os
        keys = NostrKeys.generate()
        p = tmp_path / "agentchat-test.nostr.json"
        p.write_text(json.dumps({"private_key_hex": keys.private_key_hex}))
        os.chmod(p, 0o644)  # world-readable — must be rejected
        with pytest.raises(PermissionError, match="group/world access"):
            load_pool(["ws://localhost:3000"], p)


# --------------------------------------------------------------------------- #
# server.py — create_challenge
# --------------------------------------------------------------------------- #

class TestCreateChallenge:
    def test_returns_hex_string(self):
        c = create_challenge()
        assert isinstance(c, str)
        assert len(c) == 32
        int(c, 16)  # must be valid hex

    def test_unique_per_call(self):
        seen = {create_challenge() for _ in range(50)}
        # collisions in 50 random 128-bit values are astronomically unlikely
        assert len(seen) == 50


# --------------------------------------------------------------------------- #
# server.py — create_auth_event + verify_auth_event
# --------------------------------------------------------------------------- #

class TestAuthEventRoundTrip:
    def test_happy_path(self, fresh_keys, challenge, relay_url):
        ev = create_auth_event(
            secret_key_hex=fresh_keys.private_key_hex,
            relay_url=relay_url,
            challenge=challenge,
        )
        assert ev.kind == 22242
        assert ev.content == ""
        assert ev.verify() is True
        assert verify_auth_event(
            ev,
            expected_challenge=challenge,
            expected_relay_url=relay_url,
        ) is True

    def test_verify_accepts_dict_form(self, fresh_keys, challenge, relay_url):
        ev = create_auth_event(
            secret_key_hex=fresh_keys.private_key_hex,
            relay_url=relay_url,
            challenge=challenge,
        )
        assert verify_auth_event(
            ev.to_dict(),
            expected_challenge=challenge,
            expected_relay_url=relay_url,
        ) is True

    def test_wrong_challenge_rejected(self, fresh_keys, challenge, relay_url):
        ev = create_auth_event(
            secret_key_hex=fresh_keys.private_key_hex,
            relay_url=relay_url,
            challenge=challenge,
        )
        assert verify_auth_event(
            ev,
            expected_challenge="not-the-challenge",
            expected_relay_url=relay_url,
        ) is False

    def test_wrong_relay_url_rejected(self, fresh_keys, challenge, relay_url):
        ev = create_auth_event(
            secret_key_hex=fresh_keys.private_key_hex,
            relay_url=relay_url,
            challenge=challenge,
        )
        assert verify_auth_event(
            ev,
            expected_challenge=challenge,
            expected_relay_url="wss://other-relay.example.com",
        ) is False

    def test_tampered_event_rejected(self, fresh_keys, challenge, relay_url):
        ev = create_auth_event(
            secret_key_hex=fresh_keys.private_key_hex,
            relay_url=relay_url,
            challenge=challenge,
        )
        # Tamper: change challenge tag after signing
        ev.tags = [["relay", relay_url], ["challenge", "tampered"]]
        assert verify_auth_event(
            ev,
            expected_challenge=challenge,
            expected_relay_url=relay_url,
        ) is False

    def test_expired_event_rejected(self, fresh_keys, challenge, relay_url):
        long_ago = int(time.time()) - (DEFAULT_AUTH_MAX_AGE_SECONDS + 60)
        ev = create_auth_event(
            secret_key_hex=fresh_keys.private_key_hex,
            relay_url=relay_url,
            challenge=challenge,
            created_at=long_ago,
        )
        assert verify_auth_event(
            ev,
            expected_challenge=challenge,
            expected_relay_url=relay_url,
        ) is False

    def test_future_event_rejected(self, fresh_keys, challenge, relay_url):
        # More than max_age_seconds in the future → reject
        far_future = int(time.time()) + (DEFAULT_AUTH_MAX_AGE_SECONDS + 60)
        ev = create_auth_event(
            secret_key_hex=fresh_keys.private_key_hex,
            relay_url=relay_url,
            challenge=challenge,
            created_at=far_future,
        )
        assert verify_auth_event(
            ev,
            expected_challenge=challenge,
            expected_relay_url=relay_url,
        ) is False

    def test_wrong_kind_rejected(self, fresh_keys, challenge, relay_url):
        # kind:1 instead of kind:22242 — not an auth event
        from pynostr.event import Event
        ev = Event(
            kind=1,
            content="",
            pubkey="",
            created_at=int(time.time()),
            tags=[["relay", relay_url], ["challenge", challenge]],
        )
        ev.sign(fresh_keys.private_key_hex)
        assert verify_auth_event(
            ev,
            expected_challenge=challenge,
            expected_relay_url=relay_url,
        ) is False

    def test_unsigned_event_rejected(self, fresh_keys, challenge, relay_url):
        from pynostr.event import Event
        ev = Event(
            kind=22242,
            content="",
            pubkey="",
            created_at=int(time.time()),
            tags=[["relay", relay_url], ["challenge", challenge]],
        )
        # Do NOT sign
        assert verify_auth_event(
            ev,
            expected_challenge=challenge,
            expected_relay_url=relay_url,
        ) is False

    def test_malformed_dict_rejected(self, challenge, relay_url):
        # Garbage in, garbage out — must not crash
        assert verify_auth_event(
            {"not": "an event"},
            expected_challenge=challenge,
            expected_relay_url=relay_url,
        ) is False

    def test_wrong_signer_rejected(self, fresh_keys, challenge, relay_url):
        # Event signed by a different key than what pubkey claims
        other = NostrKeys.generate()
        ev = create_auth_event(
            secret_key_hex=other.private_key_hex,
            relay_url=relay_url,
            challenge=challenge,
        )
        # Force the pubkey to be fresh_keys's — sig will no longer match
        ev.pubkey = fresh_keys.public_key_hex
        assert verify_auth_event(
            ev,
            expected_challenge=challenge,
            expected_relay_url=relay_url,
        ) is False


# --------------------------------------------------------------------------- #
# server.py — AuthRateLimiter
# --------------------------------------------------------------------------- #

class TestAuthRateLimiter:
    def test_default_allows_up_to_max(self):
        rl = AuthRateLimiter(max_attempts=3, window_seconds=60)
        for _ in range(3):
            assert rl.allow("1.2.3.4") is True
        assert rl.allow("1.2.3.4") is False

    def test_separate_ips_independent(self):
        rl = AuthRateLimiter(max_attempts=2, window_seconds=60)
        assert rl.allow("1.2.3.4") is True
        assert rl.allow("1.2.3.4") is True
        assert rl.allow("1.2.3.4") is False
        # Different IP — fresh bucket
        assert rl.allow("5.6.7.8") is True
        assert rl.allow("5.6.7.8") is True
        assert rl.allow("5.6.7.8") is False

    def test_window_slides(self):
        rl = AuthRateLimiter(max_attempts=2, window_seconds=60)
        t = 1000.0
        assert rl.allow("1.2.3.4", now=t) is True
        assert rl.allow("1.2.3.4", now=t) is True
        assert rl.allow("1.2.3.4", now=t) is False
        # 61s later — window has slid
        assert rl.allow("1.2.3.4", now=t + 61.0) is True

    def test_reset_clears_history(self):
        rl = AuthRateLimiter(max_attempts=2, window_seconds=60)
        rl.allow("1.2.3.4")
        rl.allow("1.2.3.4")
        assert rl.allow("1.2.3.4") is False
        rl.reset("1.2.3.4")
        assert rl.allow("1.2.3.4") is True

    def test_reset_unknown_ip_is_noop(self):
        rl = AuthRateLimiter(max_attempts=2, window_seconds=60)
        rl.reset("never-seen-before")  # must not raise


# --------------------------------------------------------------------------- #
# Re-export sanity (catch accidental deletion)
# --------------------------------------------------------------------------- #

def test_exports_present():
    from agentchat.nostr import server as srv
    for name in (
        "DEFAULT_AUTH_MAX_AGE_SECONDS",
        "create_challenge",
        "create_auth_event",
        "verify_auth_event",
        "AuthRateLimiter",
    ):
        assert hasattr(srv, name), f"server.py missing {name}"

    from agentchat.nostr import client as cli
    for name in ("RelayEndpoint", "IncomingEvent", "RelayPool", "load_pool"):
        assert hasattr(cli, name), f"client.py missing {name}"
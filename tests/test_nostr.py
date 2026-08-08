"""Tests for agentchat.nostr — NIP-01/29/42 subset for v1.2."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from agentchat.nostr import (
    ChannelMeta,
    NostrKeys,
    build_channel_create,
    build_channel_message,
    build_channel_metadata,
    build_delete,
    build_reaction,
    build_user_metadata,
    bech32_to_pubkey,
    load_keys,
    parse_mentions,
    pubkey_to_npub,
    save_keys,
)
from agentchat.nostr.events import (
    KIND_DELETE,
    KIND_GROUP_CREATE,
    KIND_GROUP_MESSAGE,
    KIND_GROUP_META,
    KIND_REACTION,
    KIND_SET_METADATA,
)
from agentchat.nostr.nips import derive_challenge_id, make_challenge


# ---------------------------------------------------------------------------
# NIP-01 keypairs
# ---------------------------------------------------------------------------

class TestNostrKeys:
    def test_generate_produces_valid_pubkey(self):
        k = NostrKeys.generate()
        # 32-byte x-only schnorr pubkey = 64 hex chars
        assert len(k.public_key_hex) == 64
        assert all(c in "0123456789abcdef" for c in k.public_key_hex)

    def test_npub_is_bech32_with_npub_prefix(self):
        k = NostrKeys.generate()
        assert k.npub.startswith("npub1")

    def test_nsec_is_bech32_with_nsec_prefix_and_kept_secret(self):
        k = NostrKeys.generate()
        assert k.nsec.startswith("nsec1")
        # repr must NOT leak the nsec
        assert k.nsec not in repr(k)
        assert k.private_key_hex not in repr(k)

    def test_from_nsec_roundtrip(self):
        k1 = NostrKeys.generate()
        k2 = NostrKeys.from_nsec(k1.nsec)
        assert k1.public_key_hex == k2.public_key_hex

    def test_pubkey_is_deterministic_for_same_secret(self):
        k = NostrKeys(PrivateKey_for_test().hex())
        pk1 = k.public_key_hex
        pk2 = k.public_key_hex
        assert pk1 == pk2


def PrivateKey_for_test():
    """A tiny fixed-seed helper for tests that need a known keypair."""
    from pynostr.key import PrivateKey
    return PrivateKey(bytes.fromhex("11" * 32))


class TestKeyFileIO:
    def test_save_then_load_roundtrip(self, tmp_path: Path):
        k = NostrKeys.generate()
        path = tmp_path / "test.nsec.json"
        save_keys(k, path, name="test-agent")
        loaded = load_keys(path)
        assert loaded.public_key_hex == k.public_key_hex
        # File mode is 0o600
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_save_creates_parent_dirs(self, tmp_path: Path):
        k = NostrKeys.generate()
        nested = tmp_path / "a" / "b" / "c" / "test.nsec.json"
        save_keys(k, nested)
        assert nested.exists()

    def test_save_refuses_overwrite_without_flag(self, tmp_path: Path):
        k = NostrKeys.generate()
        path = tmp_path / "test.nsec.json"
        save_keys(k, path)
        with pytest.raises(FileExistsError):
            save_keys(k, path)
        # With the flag it works
        save_keys(k, path, overwrite=True)

    def test_load_refuses_world_readable_file(self, tmp_path: Path):
        k = NostrKeys.generate()
        path = tmp_path / "test.nsec.json"
        save_keys(k, path)
        os.chmod(path, 0o644)  # world-readable
        with pytest.raises(PermissionError):
            load_keys(path)

    def test_save_writes_valid_json(self, tmp_path: Path):
        k = NostrKeys.generate()
        path = tmp_path / "test.nsec.json"
        save_keys(k, path, name="agent")
        data = json.loads(path.read_text())
        assert data["private_key_hex"] == k.private_key_hex
        assert data["nsec"] == k.nsec
        assert data["name"] == "agent"


# ---------------------------------------------------------------------------
# NIP-29 channel events
# ---------------------------------------------------------------------------

class TestBuildEvents:
    def setup_method(self):
        self.keys = NostrKeys.generate()

    def test_channel_message_has_correct_kind_and_h_tag(self):
        ev = build_channel_message(self.keys, "g-123", "hello")
        assert ev.kind == KIND_GROUP_MESSAGE
        assert ev.kind == 9  # NIP-29 (not pynostr's NIP-28 number 42)
        assert ["h", "g-123"] in ev.tags

    def test_channel_message_with_mentions(self):
        target = NostrKeys.generate()
        ev = build_channel_message(
            self.keys, "g-123", "hi", mentions=[target.public_key_hex]
        )
        assert ["p", target.public_key_hex] in ev.tags

    def test_channel_message_with_reply(self):
        ev = build_channel_message(
            self.keys, "g-123", "reply", reply_to="a" * 64
        )
        reply_tag = [t for t in ev.tags if t[0] == "e"]
        assert len(reply_tag) == 1
        assert reply_tag[0][1] == "a" * 64

    def test_channel_message_with_subject(self):
        ev = build_channel_message(
            self.keys, "g-123", "msg", subject="thread-topic"
        )
        assert ["subject", "thread-topic"] in ev.tags

    def test_channel_create(self):
        ev = build_channel_create(self.keys, "ops-room", about="ops only")
        assert ev.kind == KIND_GROUP_CREATE
        assert ev.kind == 9007
        assert ["name", "ops-room"] in ev.tags
        assert ["about", "ops only"] in ev.tags

    def test_channel_metadata(self):
        meta = ChannelMeta(name="ops-room", about="x", visibility="private")
        ev = build_channel_metadata(self.keys, "g-abc", meta)
        assert ev.kind == KIND_GROUP_META
        assert ev.kind == 39000
        assert ["d", "g-abc"] in ev.tags
        assert ["name", "ops-room"] in ev.tags
        assert ["visibility", "private"] in ev.tags

    def test_user_metadata_serialises_to_json(self):
        ev = build_user_metadata(self.keys, "Hermes", about="agentchat agent")
        assert ev.kind == KIND_SET_METADATA
        # Content is a compact JSON profile
        profile = json.loads(ev.content)
        assert profile["name"] == "Hermes"
        assert profile["about"] == "agentchat agent"

    def test_reaction_targets_event(self):
        ev = build_reaction(self.keys, target_event_id="b" * 64, emoji="+")
        assert ev.kind == KIND_REACTION
        assert ev.kind == 7
        assert ev.content == "+"
        assert ["e", "b" * 64] in ev.tags

    def test_delete_targets_events(self):
        ev = build_delete(self.keys, ["c" * 64, "d" * 64])
        assert ev.kind == KIND_DELETE
        assert ev.kind == 5
        e_tags = [t for t in ev.tags if t[0] == "e"]
        assert {t[1] for t in e_tags} == {"c" * 64, "d" * 64}


class TestEventSigning:
    def test_signed_event_verifies(self):
        k = NostrKeys.generate()
        ev = build_channel_message(k, "g-1", "hello")
        ev.sign(k.private_key_hex)
        assert ev.sig is not None
        assert ev.id is not None
        assert len(ev.sig) == 128  # 64-byte schnorr sig in hex
        assert len(ev.id) == 64    # 32-byte sha256 event id in hex
        assert ev.verify()

    def test_tampered_event_does_not_verify(self):
        k = NostrKeys.generate()
        ev = build_channel_message(k, "g-1", "hello")
        ev.sign(k.private_key_hex)
        # Tamper with content after signing
        ev.content = "tampered"
        assert not ev.verify()


# ---------------------------------------------------------------------------
# NIP-19 bech32 helpers
# ---------------------------------------------------------------------------

class TestBech32:
    def test_npub_roundtrip(self):
        k = NostrKeys.generate()
        hex_pk = k.public_key_hex
        npub = pubkey_to_npub(hex_pk)
        assert npub.startswith("npub1")
        assert bech32_to_pubkey(npub) == hex_pk

    def test_bech32_to_pubkey_rejects_bad_hrp(self):
        with pytest.raises(ValueError):
            bech32_to_pubkey("nsec1" + "a" * 58)

    def test_bech32_to_pubkey_rejects_garbage(self):
        with pytest.raises(ValueError):
            bech32_to_pubkey("not-a-bech32-string")


# ---------------------------------------------------------------------------
# NIP-21 mention parsing
# ---------------------------------------------------------------------------

class TestParseMentions:
    def test_finds_npub_in_content(self):
        k = NostrKeys.generate()
        ms = parse_mentions(f"hello @{k.npub} how are you?")
        assert len(ms) == 1
        assert ms[0].kind == "npub"
        assert ms[0].pubkey_hex == k.public_key_hex

    def test_finds_nostr_uri_prefix(self):
        k = NostrKeys.generate()
        ms = parse_mentions(f"ping nostr:{k.npub} please")
        assert len(ms) == 1
        assert ms[0].raw == k.npub  # the regex strips the nostr: prefix

    def test_finds_multiple_mentions(self):
        a, b = NostrKeys.generate(), NostrKeys.generate()
        ms = parse_mentions(f"@{a.npub} and @{b.npub} both")
        assert len(ms) == 2
        pubs = {m.pubkey_hex for m in ms}
        assert pubs == {a.public_key_hex, b.public_key_hex}

    def test_ignores_non_nostr_words(self):
        ms = parse_mentions("just plain text with no mentions")
        assert ms == []

    def test_short_or_invalid_nostr_refs_are_skipped(self):
        # too short to be a real npub
        ms = parse_mentions("short npub1abc is not enough")
        assert ms == []


# ---------------------------------------------------------------------------
# NIP-42 auth
# ---------------------------------------------------------------------------

class TestNIP42:
    def test_make_challenge_is_random_and_url_scoped(self):
        ch1 = make_challenge("ws://localhost:3000")
        ch2 = make_challenge("ws://localhost:3000")
        assert ch1.challenge != ch2.challenge  # different randomness
        assert ch1.relay_url == "ws://localhost:3000"

    def test_challenge_length_floor(self):
        with pytest.raises(ValueError):
            make_challenge("ws://x", length=4)

    def test_auth_event_is_kind_22242_and_verifies(self):
        server_ch = make_challenge("ws://localhost:3000")
        client = NostrKeys.generate()
        auth = server_ch.build_auth_event(client)
        assert auth.kind == 22242
        assert auth.verify()
        assert server_ch.verify_response(auth)

    def test_auth_response_with_wrong_challenge_is_rejected(self):
        server_ch = make_challenge("ws://localhost:3000")
        client = NostrKeys.generate()
        auth = server_ch.build_auth_event(client)
        # Build a fake server challenge with the wrong nonce
        from agentchat.nostr.nips import NIP42Challenge
        bad_ch = NIP42Challenge(challenge="wrong", relay_url="ws://localhost:3000")
        assert not bad_ch.verify_response(auth)

    def test_auth_response_with_wrong_relay_url_is_rejected(self):
        server_ch = make_challenge("ws://localhost:3000")
        client = NostrKeys.generate()
        auth = server_ch.build_auth_event(client)
        from agentchat.nostr.nips import NIP42Challenge
        bad_ch = NIP42Challenge(challenge=server_ch.challenge, relay_url="ws://evil")
        assert not bad_ch.verify_response(auth)

    def test_auth_response_with_wrong_kind_is_rejected(self):
        server_ch = make_challenge("ws://localhost:3000")
        client = NostrKeys.generate()
        # kind:9 instead of 22242 — even if signed by the same key
        auth = build_channel_message(client, "g-1", "x")
        auth.sign(client.private_key_hex)
        assert not server_ch.verify_response(auth)

    def test_auth_response_with_expired_timestamp_is_rejected(self):
        server_ch = make_challenge("ws://localhost:3000")
        client = NostrKeys.generate()
        auth = server_ch.build_auth_event(client)
        # Re-sign with a stale timestamp
        from pynostr.event import Event
        stale = Event(
            content=auth.content,
            pubkey=auth.pubkey,
            created_at=1,  # epoch
            kind=22242,
            tags=auth.tags,
        )
        stale.sign(client.private_key_hex)
        assert not server_ch.verify_response(stale, max_age_seconds=60)

    def test_derive_challenge_id_is_deterministic_and_distinct(self):
        ch = make_challenge("ws://x")
        id1 = derive_challenge_id(ch.challenge)
        id2 = derive_challenge_id(ch.challenge)
        assert id1 == id2
        ch2 = make_challenge("ws://x")
        assert derive_challenge_id(ch2.challenge) != id1
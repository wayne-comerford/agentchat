"""
Tests for the per-agent reply loop (dev6 + dev7 persona system).

Coverage:
  - Triggers gate: mentions / keywords / all_messages / from_authors / self.
  - HermesLoop.decide_reply produces a body, strips @hermes prefix.
  - ChappyLoop.decide_reply produces a body, strips @chappy prefix.
  - WayneObserverLoop.decide_reply returns None (silent persona).
  - ReplyLoop._handle_event gating rules:
      * self-mention -> skipped (no decide call)
      * trigger mismatch -> skipped (no decide call)
      * already seen -> skipped (no decide call)
      * cooldown     -> skipped (no decide call)
      * fresh principal event -> decide called, body published
  - ReplyDedupe persistence + size cap
  - Manager only loads entries that have a factory
"""
import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentchat.agents import base as base_mod
from agentchat.agents.chappy import ChappyLoop, make_chappy_loop
from agentchat.agents.hermes import HermesLoop, make_hermes_loop
from agentchat.agents.triggers import Triggers
from agentchat.agents.wayne_observer import WayneObserverLoop
from agentchat.nostr.keys import NostrKeys


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def tmp_keys(tmp_path, monkeypatch):
    """Isolated key store with 3 identities + registry + persona triggers."""
    home = tmp_path / ".hermes"
    nostr = home / "nostr"
    personas = nostr / "personas"
    personas.mkdir(parents=True)
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
            "description": f"test {name}",
        }
        # Default triggers: hermes/chappy wake on mention; wayne silent.
        triggers = (
            {"mentions": True, "keywords": [], "all_messages": False, "from_authors": []}
            if name != "wayne-observer"
            else {"mentions": False, "keywords": [], "all_messages": False, "from_authors": []}
        )
        (personas / f"{name}.triggers.json").write_text(json.dumps(triggers))
    (nostr / "registry.json").write_text(json.dumps(reg))
    return pairs, reg


# --------------------------------------------------------------------------- #
# Triggers (Buzz pattern port)
# --------------------------------------------------------------------------- #

def _event(content="hi", tags=None, pubkey="aa" * 32, kind=9):
    return {
        "id": "e" + "0" * 63,
        "pubkey": pubkey,
        "kind": kind,
        "content": content,
        "tags": tags or [],
    }


def test_triggers_default_wakes_on_mention():
    t = Triggers()  # default mentions=True
    ev = _event(tags=[["p", "bb" * 32]])
    assert t.should_reply(ev, agent_pubkey="bb" * 32, sender_pubkey="cc" * 32)


def test_triggers_mention_requires_my_pubkey_in_p_tags():
    t = Triggers()
    ev = _event(tags=[["p", "aa" * 32]])  # someone else mentioned
    assert not t.should_reply(ev, agent_pubkey="bb" * 32, sender_pubkey="cc" * 32)


def test_triggers_keyword_match_case_insensitive():
    t = Triggers(mentions=False, keywords=["urgent", "deploy"])
    ev = _event(content="please DEPLOY now")
    assert t.should_reply(ev, agent_pubkey="bb" * 32, sender_pubkey="cc" * 32)


def test_triggers_keyword_no_match():
    t = Triggers(mentions=False, keywords=["urgent"])
    ev = _event(content="no match here")
    assert not t.should_reply(ev, agent_pubkey="bb" * 32, sender_pubkey="cc" * 32)


def test_triggers_all_messages_wakes_for_everyone():
    t = Triggers(mentions=False, all_messages=True)
    ev = _event(content="anything")
    assert t.should_reply(ev, agent_pubkey="bb" * 32, sender_pubkey="cc" * 32)


def test_triggers_self_always_false():
    t = Triggers(all_messages=True)  # even the broadest
    assert not t.should_reply(_event(), agent_pubkey="cc" * 32, sender_pubkey="cc" * 32)


def test_triggers_from_authors_allowlist():
    t = Triggers(mentions=True, from_authors=["dd" * 32])
    ev = _event(tags=[["p", "bb" * 32]])
    assert not t.should_reply(ev, agent_pubkey="bb" * 32, sender_pubkey="cc" * 32)
    assert t.should_reply(ev, agent_pubkey="bb" * 32, sender_pubkey="dd" * 32)


def test_triggers_loop_safety_by_construction():
    """Hermes's reply won't trigger hermes.  No #p=self, no keywords by default."""
    t = Triggers()  # default
    # Hermes's own reply
    reply = _event(
        content="@wayne-observer got it",
        pubkey="hermes" + "0" * 58,  # hermes's pubkey
        tags=[["p", "wayne" + "0" * 60]],  # mentions wayne, not hermes
    )
    assert not t.should_reply(reply, agent_pubkey="hermes" + "0" * 58, sender_pubkey="hermes" + "0" * 58)


def test_triggers_persists_round_trip(tmp_path):
    p = tmp_path / "t.json"
    Triggers(mentions=False, keywords=["foo"]).save(p)
    loaded = Triggers.load(p)
    assert loaded.mentions is False
    assert loaded.keywords == ["foo"]
    # Missing file → defaults
    assert Triggers.load(tmp_path / "missing.json").mentions is True


# --------------------------------------------------------------------------- #
# Concrete agent bodies
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_hermes_strips_mention_and_acknowledges(tmp_keys):
    pairs, reg = tmp_keys
    loop = make_hermes_loop()
    body = await loop.decide_reply(
        {"content": "@hermes what's the status of dev6?"},
        sender_name="wayne-observer",
    )
    assert body is not None
    assert "wayne-observer" in body
    assert "what's the status of dev6?" in body
    assert "@hermes" not in body  # stripped


@pytest.mark.asyncio
async def test_hermes_handles_no_mention(tmp_keys):
    pairs, _ = tmp_keys
    loop = make_hermes_loop()
    body = await loop.decide_reply(
        {"content": "hey hermes, status?"},
        sender_name="wayne-observer",
    )
    assert body is not None
    assert "status?" in body


@pytest.mark.asyncio
async def test_hermes_empty_content_returns_none(tmp_keys):
    loop = make_hermes_loop()
    assert await loop.decide_reply({"content": ""}, "wayne-observer") is None
    assert await loop.decide_reply({"content": "   "}, "wayne-observer") is None


@pytest.mark.asyncio
async def test_chappy_acks_with_tighter_tone(tmp_keys):
    loop = make_chappy_loop()
    body = await loop.decide_reply(
        {"content": "@chappy ping me when ready"},
        sender_name="wayne-observer",
    )
    assert body is not None
    assert "@wayne-observer" in body
    assert "ping me when ready" in body
    assert "On it" in body


@pytest.mark.asyncio
async def test_wayne_observer_never_replies(tmp_keys):
    pairs, _ = tmp_keys
    loop = WayneObserverLoop(keys=pairs["wayne-observer"])
    loop.relay_url = "ws://x"
    body = await loop.decide_reply(
        {"content": "@wayne-observer please reply"},
        sender_name="hermes",
    )
    assert body is None


# --------------------------------------------------------------------------- #
# _handle_event gating (the structural loop prevention)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_handle_event_skips_self_mention(tmp_keys, monkeypatch):
    pairs, reg = tmp_keys
    loop = make_hermes_loop()
    decide = AsyncMock(return_value="should not be called")
    monkeypatch.setattr(loop, "decide_reply", decide)
    publish = AsyncMock()
    monkeypatch.setattr(loop, "_publish_reply", publish)

    ev = {
        "id": "ev1",
        "pubkey": pairs["hermes"].public_key_hex,  # SELF
        "kind": 9,
        "content": "@hermes talking to self",
        "tags": [["h", "general"], ["p", pairs["hermes"].public_key_hex]],
    }
    await loop._handle_event(ev)
    decide.assert_not_called()
    publish.assert_not_called()


@pytest.mark.asyncio
async def test_handle_event_skips_when_trigger_does_not_fire(tmp_keys, monkeypatch):
    """chappy posts to @hermes but hermes's triggers must still allow it
    (mentions=True).  Use a non-mention event to verify the trigger gate."""
    pairs, _ = tmp_keys
    loop = make_hermes_loop()
    # Override triggers to never fire.
    loop.triggers = Triggers(mentions=False, keywords=[], all_messages=False)

    decide = AsyncMock(return_value="should not fire")
    monkeypatch.setattr(loop, "decide_reply", decide)

    ev = {
        "id": "ev_notrigger",
        "pubkey": pairs["wayne-observer"].public_key_hex,
        "kind": 9,
        "content": "no @hermes here",
        "tags": [["h", "general"], ["p", pairs["hermes"].public_key_hex]],
    }
    await loop._handle_event(ev)
    decide.assert_not_called()


@pytest.mark.asyncio
async def test_handle_event_skips_already_seen(tmp_keys, monkeypatch):
    pairs, _ = tmp_keys
    loop = make_hermes_loop()
    loop.dedupe.mark("ev_dup")
    decide = AsyncMock(return_value="x")
    monkeypatch.setattr(loop, "decide_reply", decide)

    ev = {
        "id": "ev_dup",
        "pubkey": pairs["wayne-observer"].public_key_hex,
        "kind": 9,
        "content": "hello",
        "tags": [["h", "general"], ["p", pairs["hermes"].public_key_hex]],
    }
    await loop._handle_event(ev)
    decide.assert_not_called()


@pytest.mark.asyncio
async def test_handle_event_skips_during_cooldown(tmp_keys, monkeypatch):
    pairs, _ = tmp_keys
    loop = make_hermes_loop()
    loop.reply_cooldown_seconds = 60.0
    loop._last_reply_ts = time.time()

    decide = AsyncMock(return_value="x")
    monkeypatch.setattr(loop, "decide_reply", decide)
    publish = AsyncMock()
    monkeypatch.setattr(loop, "_publish_reply", publish)

    ev = {
        "id": "ev_cd",
        "pubkey": pairs["wayne-observer"].public_key_hex,
        "kind": 9,
        "content": "hello",
        "tags": [["h", "general"], ["p", pairs["hermes"].public_key_hex]],
    }
    await loop._handle_event(ev)
    decide.assert_not_called()
    publish.assert_not_called()


@pytest.mark.asyncio
async def test_handle_event_replies_to_principal(tmp_keys, monkeypatch):
    pairs, _ = tmp_keys
    loop = make_hermes_loop()
    decide = AsyncMock(return_value="@wayne-observer got it")
    monkeypatch.setattr(loop, "decide_reply", decide)
    publish = AsyncMock()
    monkeypatch.setattr(loop, "_publish_reply", publish)

    ev = {
        "id": "ev_ok",
        "pubkey": pairs["wayne-observer"].public_key_hex,
        "kind": 9,
        "content": "@hermes what's next?",
        "tags": [["h", "general"], ["p", pairs["hermes"].public_key_hex]],
    }
    await loop._handle_event(ev)
    decide.assert_called_once()
    publish.assert_called_once()
    call = publish.call_args
    args, kwargs = call
    assert kwargs["reply_to"] == "ev_ok"
    assert args[0] == "general"
    assert kwargs["mentions"] == [pairs["wayne-observer"].public_key_hex.lower()]
    assert loop.dedupe.seen("ev_ok")


@pytest.mark.asyncio
async def test_handle_event_silent_when_decide_returns_none(tmp_keys, monkeypatch):
    pairs, _ = tmp_keys
    loop = make_hermes_loop()
    decide = AsyncMock(return_value=None)
    monkeypatch.setattr(loop, "decide_reply", decide)
    publish = AsyncMock()
    monkeypatch.setattr(loop, "_publish_reply", publish)

    ev = {
        "id": "ev_silent",
        "pubkey": pairs["wayne-observer"].public_key_hex,
        "kind": 9,
        "content": "...",
        "tags": [["h", "general"], ["p", pairs["hermes"].public_key_hex]],
    }
    await loop._handle_event(ev)
    decide.assert_called_once()
    publish.assert_not_called()
    assert not loop.dedupe.seen("ev_silent")


# --------------------------------------------------------------------------- #
# ReplyDedupe
# --------------------------------------------------------------------------- #

def test_dedupe_persists(tmp_path):
    p = tmp_path / "d.json"
    d = base_mod.ReplyDedupe(p)
    assert not d.seen("a")
    d.mark("a")
    assert d.seen("a")
    d2 = base_mod.ReplyDedupe(p)
    assert d2.seen("a")


def test_dedupe_size_cap(tmp_path):
    p = tmp_path / "d.json"
    d = base_mod.ReplyDedupe(p)
    for i in range(2500):
        d.mark(f"e{i}")
    assert len(d._seen) <= 2000


# --------------------------------------------------------------------------- #
# Manager (factory-based, no `kind` field)
# --------------------------------------------------------------------------- #

def test_manager_builds_only_factored_loops(tmp_keys):
    """Manager picks entries that have a factory (hermes, chappy).
    wayne-observer has no factory → not built.  No `kind` field needed."""
    from agentchat.agents.manager import _build_loops
    pairs, reg = tmp_keys
    loops = _build_loops()
    names = {l.name for l in loops}
    assert "hermes" in names
    assert "chappy" in names
    assert "wayne-observer" not in names


def test_manager_handles_unknown_registry_entry(tmp_keys):
    """An entry without a factory is silently skipped."""
    from agentchat.agents import manager as mgr
    _, reg = tmp_keys
    reg["phantom"] = {
        "public_key_hex": "ff" * 32,
        "npub": "npub_phantom",
    }
    reg_path = Path(os.environ["AGENTCHAT_NOSTR_DIR"]) / "registry.json"
    reg_path.write_text(json.dumps(reg))

    loops = mgr._build_loops()
    names = {l.name for l in loops}
    assert "phantom" not in names
    assert "hermes" in names
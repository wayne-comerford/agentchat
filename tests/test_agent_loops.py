"""
Tests for the per-agent reply loop (dev6).

Coverage:
  - HermesLoop.decide_reply produces a body, strips @hermes prefix.
  - ChappyLoop.decide_reply produces a body, strips @chappy prefix.
  - WayneObserverLoop.decide_reply returns None (principal).
  - ReplyLoop._handle_event gating rules:
      * self-mention -> skipped (no decide call)
      * a2a event    -> skipped (no decide call)
      * already seen -> skipped (no decide call)
      * cooldown     -> skipped (no decide call)
      * fresh principal event -> decide called, body published
  - ReplyDedupe persistence + size cap
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
from agentchat.agents.wayne_observer import WayneObserverLoop
from agentchat.nostr.keys import NostrKeys


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def tmp_keys(tmp_path, monkeypatch):
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
            "kind": "agent" if name != "wayne-observer" else "principal",
        }
    (nostr / "registry.json").write_text(json.dumps(reg))
    return pairs, reg


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
    loop = make_hermes_loop(agent_pubkeys={
        pairs["hermes"].public_key_hex.lower(),
        pairs["chappy"].public_key_hex.lower(),
    })
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
async def test_handle_event_skips_agent_to_agent(tmp_keys, monkeypatch):
    pairs, _ = tmp_keys
    agent_pubs = {
        pairs["hermes"].public_key_hex.lower(),
        pairs["chappy"].public_key_hex.lower(),
    }
    loop = make_hermes_loop(agent_pubkeys=agent_pubs)
    decide = AsyncMock(return_value="should not fire")
    monkeypatch.setattr(loop, "decide_reply", decide)

    ev = {
        "id": "ev_a2a",
        "pubkey": pairs["chappy"].public_key_hex,  # chappy is an agent
        "kind": 9,
        "content": "@hermes from chappy",
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
    # Disable cooldown for this test
    loop = make_hermes_loop()
    loop.reply_cooldown_seconds = 60.0  # huge cooldown
    loop._last_reply_ts = time.time()   # just replied

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
    # Reply should be threaded to the original event
    call = publish.call_args
    args, kwargs = call
    # _publish_reply(channel, body, mentions=[...], reply_to=...)
    assert kwargs["reply_to"] == "ev_ok"
    assert args[0] == "general"  # channel
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
# Manager
# --------------------------------------------------------------------------- #

def test_manager_builds_only_agent_loops(tmp_keys, monkeypatch):
    """Manager should skip principals and unknown names."""
    from agentchat.agents.manager import _build_loops, AGENT_FACTORIES
    pairs, reg = tmp_keys
    loops = _build_loops()
    names = {l.name for l in loops}
    # hermes + chappy are agents; wayne-observer is principal -> excluded
    assert "hermes" in names
    assert "chappy" in names
    assert "wayne-observer" not in names


def test_manager_handles_missing_factory(tmp_keys):
    """If a registry entry has kind=agent but no factory, skip with warning."""
    from agentchat.agents import manager as mgr
    _, reg = tmp_keys
    reg["phantom"] = {
        "public_key_hex": "ff" * 32,
        "npub": "npub_phantom",
        "kind": "agent",
    }
    reg_path = Path(os.environ["AGENTCHAT_NOSTR_DIR"]) / "registry.json"
    reg_path.write_text(json.dumps(reg))

    loops = mgr._build_loops()
    names = {l.name for l in loops}
    assert "phantom" not in names
    assert "hermes" in names
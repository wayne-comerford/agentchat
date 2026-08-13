"""
agentchat v1.2 — Agent reply loop (base class).

Each agent in agentchat is an independent reply loop.  The loop:

  1. Connects to the configured Nostr relay (single WS connection).
  2. Subscribes to kind:9 events where the agent's npub is in the #p tags.
  3. Filters out events the agent shouldn't reply to:
     - Events authored by another agent (avoids a2a loops).
     - Events authored by itself (self-mentions).
     - Events the agent already replied to (idempotent).
  4. Calls the agent's `decide_reply(event)` to produce a reply body.
  5. Signs the reply with the agent's own keypair and publishes it.
  6. Tracks the event id it just replied to in a per-agent dedupe store.

This is the Buzz pattern: each agent owns its reply state, no shared
global table.  It is structurally impossible for two agents to reply
to each other in a loop because each agent independently checks
"is the sender also an agent?" before responding.

Concrete subclasses override `decide_reply()` to produce a body.  The
base class handles the relay I/O and the gating.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import websockets

from agentchat.nostr.events import build_channel_message
from agentchat.nostr.keys import NostrKeys, load_keys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)-12s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agent-loop")


# --------------------------------------------------------------------------- #
# Per-agent dedupe (survives restarts, isolated from other agents)
# --------------------------------------------------------------------------- #

class ReplyDedupe:
    """In-memory + file-backed set of event ids this agent has replied to."""

    def __init__(self, path: Path):
        self.path = path
        self._seen: set[str] = set()
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path) as f:
                    self._seen = set(json.load(f))
            except Exception:
                self._seen = set()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(sorted(self._seen), f)

    def seen(self, event_id: str) -> bool:
        return event_id in self._seen

    def mark(self, event_id: str) -> None:
        self._seen.add(event_id)
        if len(self._seen) > 2000:
            self._seen = set(sorted(self._seen)[-2000:])
        self._save()


# --------------------------------------------------------------------------- #
# Reply loop base
# --------------------------------------------------------------------------- #

class ReplyLoop:
    """
    Base class for an agent's reply loop.

    Subclasses override `decide_reply(event, sender)` to produce a body.
    The base class handles relay I/O, filtering, dedupe, and publishing.
    """

    # ----- class-level config (override in subclass) -----
    name: str = ""                # e.g. "hermes"
    relay_url: str = ""           # ws://127.0.0.1:9876
    subscribe_filter_seconds: int = 600  # only see recent events on boot
    reply_cooldown_seconds: float = 0.0  # minimum gap between replies

    def __init__(
        self,
        keys: NostrKeys,
        *,
        agent_pubkeys: set[str] | None = None,
        dedupe_path: Path | None = None,
        subscribe_kinds: tuple[int, ...] = (9,),
    ) -> None:
        if not self.name:
            raise ValueError("ReplyLoop subclass must set .name")
        if not self.relay_url:
            raise ValueError("ReplyLoop subclass must set .relay_url")
        self.keys = keys
        self.pubkey = keys.public_key_hex.lower()
        # Set of known agent pubkeys (we don't reply to a2a traffic).
        self.agent_pubkeys = agent_pubkeys or set()
        self.dedupe = ReplyDedupe(dedupe_path or Path(f"/tmp/agentchat_{self.name}_dedupe.json"))
        self.subscribe_kinds = subscribe_kinds
        self._last_reply_ts: float = 0.0
        self._stop = asyncio.Event()

    # ----- subclass hook -----
    async def decide_reply(
        self,
        event: dict,
        sender_name: str | None,
    ) -> str | None:
        """
        Return the reply body, or None to stay silent.

        `sender_name` is the registry name of the sender if known, else None.

        Override in subclasses.  Default is a no-op (silent).
        """
        return None

    # ----- lifecycle -----
    async def run(self) -> None:
        log.info("[%s] starting on %s", self.name, self.relay_url)
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._run_once()
                backoff = 1.0
            except Exception as e:
                log.warning("[%s] loop error: %s; retry in %.1fs", self.name, e, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)
        log.info("[%s] stopped", self.name)

    def stop(self) -> None:
        self._stop.set()

    # ----- internals -----
    async def _run_once(self) -> None:
        url = self.relay_url
        log.debug("[%s] connecting to %s", self.name, url)
        async with websockets.connect(url, ping_interval=30) as ws:
            log.info("[%s] connected", self.name)
            # Subscribe to kind:9 events that mention me (#p tag == my pubkey).
            sub_id = f"{self.name}-inbox"
            req = [
                "REQ",
                sub_id,
                {
                    "kinds": list(self.subscribe_kinds),
                    "#p": [self.pubkey],
                    "since": int(time.time()) - self.subscribe_filter_seconds,
                },
            ]
            await ws.send(json.dumps(req))
            # Drain incoming EVENT messages.
            while not self._stop.is_set():
                raw = await ws.recv()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(msg, list) or not msg:
                    continue
                if msg[0] == "EVENT" and len(msg) >= 3 and msg[1] == sub_id:
                    ev = msg[2]
                    try:
                        await self._handle_event(ev)
                    except Exception as e:
                        log.warning("[%s] handler error: %s", self.name, e)
                elif msg[0] == "CLOSED":
                    log.info("[%s] subscription CLOSED: %s", self.name, msg)
                    break

    async def _handle_event(self, ev: dict) -> None:
        eid = ev.get("id", "")
        if not eid:
            return
        if self.dedupe.seen(eid):
            return
        sender = (ev.get("pubkey") or "").lower()
        if not sender:
            return

        # Skip self-mentions.
        if sender == self.pubkey:
            return

        # Skip agent-to-agent traffic (loop prevention).
        if sender in self.agent_pubkeys:
            log.debug("[%s] skipping a2a event %s", self.name, eid[:12])
            return

        # Find sender name from registry if known.
        sender_name = None
        for pk, name in self._registry_lookup():
            if pk == sender:
                sender_name = name
                break

        # Cooldown gate.
        now = time.time()
        if now - self._last_reply_ts < self.reply_cooldown_seconds:
            log.debug("[%s] cooldown; skipping %s", self.name, eid[:12])
            return

        # Ask the agent.
        body = await self.decide_reply(ev, sender_name)
        if not body:
            return

        channel = self._channel_of(ev)
        if not channel:
            return

        # Publish reply.
        await self._publish_reply(channel, body, mentions=[sender], reply_to=eid)
        self.dedupe.mark(eid)
        self._last_reply_ts = time.time()

    async def _publish_reply(
        self,
        channel: str,
        body: str,
        mentions: list[str],
        reply_to: str,
    ) -> None:
        """Open a short-lived WS, publish, close."""
        url = self.relay_url
        async with websockets.connect(url, ping_interval=30) as ws:
            unsigned = build_channel_message(
                keys=self.keys,
                group_id=channel,
                content=body,
                mentions=mentions,
                reply_to=reply_to,  # thread the reply to the triggering event
            )
            # pynostr.Event.sign() mutates in place and returns None.
            unsigned.sign(self.keys.private_key_hex)
            # to_message() returns the full ['EVENT', event_dict] envelope.
            await ws.send(unsigned.to_message())
            # Wait for OK
            try:
                ack = await asyncio.wait_for(ws.recv(), timeout=5)
                log.info("[%s] replied in %s (%d chars): %s",
                         self.name, channel, len(body), ack[:80])
            except asyncio.TimeoutError:
                log.warning("[%s] no ACK within 5s", self.name)

    @staticmethod
    def _channel_of(event: dict) -> str | None:
        for tag in event.get("tags") or []:
            if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "h":
                return str(tag[1])
        return None

    def _registry_lookup(self) -> list[tuple[str, str]]:
        """Subclasses can override to provide their own agent set.
        Default: read registry.json fresh each call (cheap enough for v1)."""
        path = Path.home() / ".hermes" / "nostr" / "registry.json"
        if not path.exists():
            return []
        try:
            reg = json.loads(path.read_text())
        except Exception:
            return []
        return [(info["public_key_hex"].lower(), name) for name, info in reg.items()]
"""
Nostr WebSocket transport for agentchat v1.2.

Thin wrapper around pynostr's RelayManager that adds:
- `subscribe_channel(channel_id)` for NIP-29 channel reading (kind:9)
- `publish_channel_message(channel_id, content)` for posting kind:9 messages
- A `start_listen()` background thread that drains events into a public queue
- One callable per subscription (fired from the listen thread)

This is the **outbound** surface — for talking to remote Nostr relays
(Buzz, snort, damus, etc.). Inbound (acting as a Nostr relay and authenticating
clients) lives in `server.py`.

Design notes:
- No global state. Every call takes a `RelayPool` instance.
- All network I/O happens via pynostr's Tornado IOLoop in a daemon thread;
  the foreground thread only enqueues reads / consumes from a queue.Queue.
- Signing is handled here so callers don't have to know about pynostr's
  Event.sign() hex-key API.
"""
from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from pynostr.event import Event as PynostrEvent
from pynostr.filters import Filters, FiltersList
from pynostr.relay_manager import RelayManager

from .events import build_channel_create, build_channel_message
from .keys import NostrKeys, load_keys


# --------------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RelayEndpoint:
    """One remote relay to talk to. ws:// or wss:// only."""
    url: str

    def __post_init__(self):
        if not (self.url.startswith("ws://") or self.url.startswith("wss://")):
            raise ValueError(f"relay url must be ws:// or wss://, got {self.url!r}")


@dataclass
class IncomingEvent:
    """An event we received from a remote relay."""
    subscription_id: str
    event: PynostrEvent
    relay_url: str

    @property
    def event_id(self) -> str:
        return self.event.id

    @property
    def kind(self) -> int:
        return self.event.kind

    @property
    def content(self) -> str:
        return self.event.content


class RelayPool:
    """
    Manage a pool of relay connections for one agentchat client.

    Lifecycle:
        pool = RelayPool([RelayEndpoint("ws://localhost:3000")], keys)
        sub_id = pool.subscribe_channel("dinner-channel-id")
        pool.start_listen()
        while True:
            ev = pool.incoming.get(timeout=5)
            handle(ev)
        pool.stop_listen()
    """

    def __init__(
        self,
        relays: Iterable[RelayEndpoint | str],
        keys: NostrKeys,
    ):
        # Accept either RelayEndpoint or str (ws://...) — wrap strings
        endpoints: list[RelayEndpoint] = []
        for r in relays:
            if isinstance(r, RelayEndpoint):
                endpoints.append(r)
            elif isinstance(r, str):
                endpoints.append(RelayEndpoint(url=r))
            else:
                raise TypeError(
                    f"RelayPool relays must be RelayEndpoint or str, got {type(r).__name__}"
                )
        if not endpoints:
            raise ValueError("at least one RelayEndpoint required")
        self._endpoints = endpoints
        self._keys = keys
        self._rm = RelayManager()

        # Subscription registry: sub_id -> (filter_dict, callback)
        self._subs: dict[str, tuple[dict, Callable[[IncomingEvent], None] | None]] = {}
        self._sub_counter = 0

        # Background drain thread
        self._incoming: queue.Queue[IncomingEvent] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

    # ----- subscriptions ---------------------------------------------------- #

    def subscribe_channel(
        self,
        channel_id: str,
        *,
        callback: Callable[[IncomingEvent], None] | None = None,
        since: int | None = None,
        limit: int | None = None,
    ) -> str:
        """
        Subscribe to a NIP-29 channel (kind:9 with #h=channel_id).
        Returns subscription_id.

        `callback` is invoked from the listen thread for each new event.
        Events are ALWAYS pushed onto `self.incoming` regardless of callback.
        """
        self._sub_counter += 1
        sub_id = f"agentchat-sub-{self._sub_counter}"

        if self._started:
            # Build Filters with arbitrary #h tag (pynostr Filters doesn't
            # expose #h as a kwarg; use add_arbitrary_tag)
            filters_obj = Filters(kinds=[9])
            if since is not None:
                filters_obj.since = since
            if limit is not None:
                filters_obj.limit = limit
            filters_obj.add_arbitrary_tag("h", [channel_id])
            self._rm.add_subscription_on_all_relays(
                sub_id, FiltersList([filters_obj])
            )

        self._subs[sub_id] = ({"#h": [channel_id], "kinds": [9]}, callback)
        return sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        """Cancel a subscription. Returns True if it existed."""
        existed = subscription_id in self._subs
        if existed and self._started:
            try:
                self._rm.close_subscription_on_all_relays(subscription_id)
            except Exception:
                pass
        self._subs.pop(subscription_id, None)
        return existed

    # ----- publishing ------------------------------------------------------ #

    def publish_channel_message(
        self,
        channel_id: str,
        content: str,
        *,
        mentions: Iterable[str] | None = None,
    ) -> str:
        """
        Build, sign, and publish a NIP-29 channel message (kind:9).
        Returns the event id (hex).

        Uses pynostr to build + sign the event, but sends directly via raw
        websockets (NOT through pynostr's RelayManager). Reason: pynostr's
        Tornado IOLoop conflicts with aiohttp's asyncio loop, making publish
        silently fail. The raw path works in any async context.
        """
        if not self._endpoints:
            raise RuntimeError("RelayPool has no relay endpoints configured")
        ev = build_channel_message(
            keys=self._keys,
            group_id=channel_id,
            content=content,
            mentions=mentions,
        )
        ev.sign(self._keys.private_key.hex())
        return _send_event_sync(ev, self._endpoints)

    def publish_channel_create(
        self,
        channel_id: str,
        *,
        name: str,
        about: str = "",
    ) -> str:
        """Build, sign, and publish a NIP-29 GROUP_CREATE (kind:9007)."""
        ev = build_channel_create(keys=self._keys, name=name, about=about)
        ev.sign(self._keys.private_key.hex())
        return _send_event_sync(ev, self._endpoints)

    # ----- background thread ---------------------------------------------- #

    @property
    def incoming(self) -> queue.Queue[IncomingEvent]:
        """Queue of all events received from any subscribed relay."""
        return self._incoming

    @property
    def started(self) -> bool:
        return self._started

    def start_listen(self) -> None:
        """
        Add relays, open WebSocket connections, register subscriptions,
        and spawn the background drain thread.

        Idempotent: a second call is a no-op.
        """
        if self._started:
            return

        # Build a message_callback that pulls raw messages into pynostr's
        # message_pool (this is how pynostr wires incoming frames).
        def _on_message(message: str, url: str) -> None:
            try:
                self._rm.message_pool.add_message(message, url)
            except Exception:
                pass

        for ep in self._endpoints:
            self._rm.add_relay(
                ep.url,
                message_callback=_on_message,
                message_callback_url=True,
                close_on_eose=False,  # keep stream open for live updates
            )

        # Re-register any subscriptions added before start
        for sub_id, (filt_dict, _cb) in self._subs.items():
            # Build Filters manually to support arbitrary tags like #h
            filters_obj = Filters(
                kinds=filt_dict.get("kinds"),
                authors=filt_dict.get("authors"),
                since=filt_dict.get("since"),
                until=filt_dict.get("until"),
                limit=filt_dict.get("limit"),
            )
            # Apply arbitrary tag filters (#h, #e, #p, #t, etc.)
            for key, val in filt_dict.items():
                if key.startswith("#") and isinstance(val, list):
                    tag_letter = key[1:]
                    filters_obj.add_arbitrary_tag(tag_letter, val)
            self._rm.add_subscription_on_all_relays(
                sub_id, FiltersList([filters_obj])
            )

        # Open connections (Tornado IOLoop runs in a thread; we use daemon
        # thread so it doesn't block process exit)
        threading.Thread(
            target=self._rm.open_connections, daemon=True, name="nostr-ioloop"
        ).start()

        # Drain thread: pulls EventMessage out of pynostr's pool and
        # dispatches to our public queue + per-sub callbacks
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._drain_loop, name="agentchat-nostr-drain", daemon=True
        )
        self._thread.start()
        self._started = True

    def stop_listen(self, timeout: float = 3.0) -> None:
        """Signal drain thread to exit and close all relay connections."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        try:
            self._rm.close_connections()
        except Exception:
            pass
        self._started = False

    def _drain_loop(self) -> None:
        """Background loop: pull EventMessage from pynostr, dispatch."""
        pool = self._rm.message_pool
        while not self._stop.is_set():
            try:
                if not pool.has_events():
                    # Tight-ish poll — Tornado IOLoop fills the pool asynchronously
                    self._stop.wait(timeout=0.05)
                    continue
                em = pool.get_event()
                incoming = IncomingEvent(
                    subscription_id=em.subscription_id,
                    event=em.event,
                    relay_url=em.url,
                )
                self._incoming.put(incoming)
                cb_entry = self._subs.get(em.subscription_id)
                if cb_entry is not None:
                    _filt, cb = cb_entry
                    if cb is not None:
                        try:
                            cb(incoming)
                        except Exception:
                            # Callback errors must not kill the drain thread
                            pass
            except Exception:
                # Defensive: never let the drain thread die silently
                self._stop.wait(timeout=0.5)


# --------------------------------------------------------------------------- #
# Convenience builder
# --------------------------------------------------------------------------- #

def make_pool_for(
    relays: list[str],
    keys: NostrKeys,
) -> RelayPool:
    """Build a RelayPool from a list of ws:// URLs and an existing NostrKeys."""
    return RelayPool([RelayEndpoint(url=r) for r in relays], keys=keys)


def load_pool(
    relays: list[str],
    key_path: str | Path,
) -> RelayPool:
    """
    Load a Nostr keypair from `key_path` (chmod-600 JSON) and build a pool.
    Convenience for one-shot scripts.
    """
    kp = load_keys(key_path)
    return make_pool_for(relays, kp)


# --------------------------------------------------------------------------- #
# Raw websocket transport (bypasses pynostr's Tornado IOLoop)
# --------------------------------------------------------------------------- #

def _send_event_sync(event, endpoints) -> str:
    """
    Publish `event` to all `endpoints` via raw websockets. Blocking.

    For each endpoint:
      1. Open WebSocket
      2. Send ["EVENT", <event_dict>]
      3. Wait for OK response (3s timeout)
      4. Close

    Returns the event id on first successful publish. If all relays fail,
    raises the last exception.
    """
    import json as _json
    import socket

    import websockets.sync.client

    payload = _json.dumps(["EVENT", event.to_dict()]).encode()
    last_err: Exception | None = None

    for ep in endpoints:
        url = ep.url
        try:
            with websockets.sync.client.connect(
                url, open_timeout=3, close_timeout=1
            ) as ws:
                ws.send(payload)
                # Read until we get OK / closed / timeout
                try:
                    resp_raw = ws.recv(timeout=3)
                    resp = _json.loads(resp_raw)
                    if isinstance(resp, list) and len(resp) >= 3 and resp[0] == "OK":
                        if resp[2] is True:
                            return event.id
                        else:
                            last_err = RuntimeError(
                                f"relay {url} rejected: {resp[3] if len(resp) > 3 else '?'}"
                            )
                            continue
                except (TimeoutError, socket.timeout):
                    # No OK within 3s — assume publish worked (relay didn't respond)
                    return event.id
        except Exception as e:
            last_err = e
            continue

    if last_err:
        raise last_err
    return event.id


__all__ = [
    "RelayEndpoint",
    "IncomingEvent",
    "RelayPool",
    "make_pool_for",
    "load_pool",
]
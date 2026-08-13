#!/usr/bin/env python3
"""
agentchat v1.2 — Echo Nostr relay for local testing.

A minimal NIP-11 / NIP-29 / NIP-42 compliant Nostr relay you can run
on your own machine to exercise Hermes/Chappy clients against before
shipping v1.2 to prod. NOT a production relay — pure stdlib + websockets,
no clustering, no persistence, in-memory event store.

Endpoints:
    ws://localhost:9876       — Nostr WebSocket protocol
    http://localhost:9876/.well-known/nostr.json  — NIP-11 metadata
    http://localhost:9876/health                  — liveness
    http://localhost:9876/stats                   — event count + ws clients
    http://localhost:9876/events                  — dump stored events (debug)

Supported client messages:
    ["EVENT", <event>]          — ingest + verify; ACK with OK
    ["REQ", <sub_id>, <filter>, ...] — replay matching events from store
    ["CLOSE", <sub_id>]         — drop subscription
    ["AUTH", <signed-event>]    — NIP-42 auth, verify against issued challenge

Server-initiated messages:
    ["AUTH", "<challenge>"]     — issued immediately on connect

Run:
    /home/waynec/agentchat/.venv/bin/python \\
        /home/waynec/agentchat/agentchat/nostr/echo_relay.py

    # or with custom port:
    PORT=9877 RELAY_URL=wss://relay.example.com \\
        /home/waynec/agentchat/.venv/bin/python \\
        /home/waynec/agentchat/agentchat/nostr/echo_relay.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from collections import defaultdict
from http import HTTPStatus
from pathlib import Path
from typing import Any

# Make sibling agentchat package importable when run as a script
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from agentchat.nostr.server import (  # noqa: E402
    create_challenge,
    verify_auth_event,
)

import websockets  # noqa: E402
from websockets.asyncio.server import ServerConnection, serve  # noqa: E402
from websockets.datastructures import Headers  # noqa: E402
from websockets.http11 import Response  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

PORT = int(os.environ.get("PORT", "9876"))
HOST = os.environ.get("HOST", "0.0.0.0")
RELAY_URL = os.environ.get(
    "RELAY_URL", f"ws://localhost:{PORT}"
)
MAX_STORED_EVENTS = int(os.environ.get("MAX_STORED_EVENTS", "1000"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("echo-relay")


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

class RelayState:
    """All mutable state in one place for clarity."""

    def __init__(self, max_events: int):
        self.max_events = max_events
        self.events: list[dict] = []
        self.connected: set[ServerConnection] = set()
        # sub_id -> (filters, set of event indices already sent)
        self.subs: dict[ServerConnection, dict[str, tuple[list[dict], set[int]]]] = defaultdict(dict)
        # pubkey-hex of authed connections
        self.authed: dict[ServerConnection, str] = {}
        # per-connection: issued challenge
        self.challenges: dict[ServerConnection, str] = {}

    # ----- event store ----- #

    def add_event(self, event: dict) -> int:
        idx = len(self.events)
        self.events.append(event)
        if len(self.events) > self.max_events:
            drop = len(self.events) - self.max_events
            self.events = self.events[drop:]
            for ws_map in self.subs.values():
                for sub_id, (filts, idxs) in ws_map.items():
                    ws_map[sub_id] = (filts, {i - drop for i in idxs if i >= drop})
            idx -= drop
        return idx

    def filter_events(self, filt: dict) -> list[int]:
        out: list[int] = []
        kinds = filt.get("kinds")
        authors = filt.get("authors")
        ids = filt.get("ids")
        since = filt.get("since")
        until = filt.get("until")
        e_refs = set(filt.get("#e") or [])
        p_refs = set(filt.get("#p") or [])
        h_refs = set(filt.get("#h") or [])

        for i, ev in enumerate(self.events):
            if kinds and ev.get("kind") not in kinds:
                continue
            if authors and ev.get("pubkey") not in authors:
                continue
            if ids and ev.get("id") not in ids:
                continue
            ts = ev.get("created_at", 0)
            if since is not None and ts < since:
                continue
            if until is not None and ts > until:
                continue
            if e_refs or p_refs or h_refs:
                tag_set: dict[str, set[str]] = defaultdict(set)
                for tag in (ev.get("tags") or []):
                    if len(tag) >= 2:
                        tag_set[tag[0]].add(tag[1])
                if e_refs and not (e_refs & tag_set.get("e", set())):
                    continue
                if p_refs and not (p_refs & tag_set.get("p", set())):
                    continue
                if h_refs and not (h_refs & tag_set.get("h", set())):
                    continue
            out.append(i)
        return out

    # ----- connection lifecycle ----- #

    def register(self, ws: ServerConnection) -> str:
        self.connected.add(ws)
        self.subs[ws] = {}
        challenge = create_challenge()
        self.challenges[ws] = challenge
        log.info("CONNECT from %s, challenge=%s...", ws.remote_address, challenge[:8])
        return challenge

    def unregister(self, ws: ServerConnection) -> None:
        self.connected.discard(ws)
        self.subs.pop(ws, None)
        self.challenges.pop(ws, None)
        npub = self.authed.pop(ws, None)
        log.info("DISCONNECT from %s (was auth'd as %s)", ws.remote_address, npub or "<unauthed>")

    def mark_authed(self, ws: ServerConnection, pubkey: str) -> None:
        self.authed[ws] = pubkey
        log.info("AUTH OK from %s → pubkey=%s...", ws.remote_address, pubkey[:8])


STATE = RelayState(MAX_STORED_EVENTS)


# --------------------------------------------------------------------------- #
# NIP-11 metadata
# --------------------------------------------------------------------------- #

NIP11_METADATA = {
    "name": "agentchat-v1.2-echo",
    "description": "Local echo Nostr relay for testing agentchat v1.2 Nostr primitives",
    "pubkey": "0000000000000000000000000000000000000000000000000000000000000000",
    "contact": "npub1d8em5mg3ve5hvuqxywmf08xr7tggjadcyav04pn0yyr2fef9fjksavdaqj",
    "supported_nips": [1, 11, 29, 42],
    "software": "https://github.com/yourname/agentchat",
    "version": "1.2.0.echo",
    "limitation": {
        "max_message_length": 65536,
        "max_subscriptions": 20,
        "max_filters": 10,
        "max_event_tags": 100,
        "max_content_length": 16384,
        "min_pow_difficulty": 0,
        "auth_required": False,
        "payment_required": False,
    },
    "relay_countries": ["IE"],
    "language_tags": ["en"],
    "tags": ["test", "agentchat", "v1.2"],
}


# --------------------------------------------------------------------------- #
# Nostr protocol handler
# --------------------------------------------------------------------------- #

async def ws_handler(ws: ServerConnection) -> None:
    challenge = STATE.register(ws)

    # Issue AUTH challenge immediately (NIP-42 standard)
    try:
        await ws.send(json.dumps(["AUTH", challenge]))
    except Exception as e:
        log.warning("Failed to send AUTH challenge: %s", e)

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Bad JSON from %s: %r", ws.remote_address, raw[:80])
                continue
            await handle_message(ws, msg)
    except ConnectionClosed:
        pass
    finally:
        STATE.unregister(ws)


async def handle_message(ws: ServerConnection, msg: list[Any]) -> None:
    if not msg or not isinstance(msg, list):
        return
    typ = msg[0]

    if typ == "EVENT":
        await on_event(ws, msg)
    elif typ == "REQ":
        await on_req(ws, msg)
    elif typ == "CLOSE":
        on_close(ws, msg)
    elif typ == "AUTH":
        await on_auth(ws, msg)
    else:
        log.warning("Unknown message type from %s: %r", ws.remote_address, typ)


async def on_event(ws: ServerConnection, msg: list[Any]) -> None:
    if len(msg) < 2 or not isinstance(msg[1], dict):
        await ok(ws, "?", False, "malformed: EVENT payload must be object")
        return
    event = msg[1]
    eid = event.get("id", "?")[:16]

    for k in ("id", "pubkey", "created_at", "kind", "tags", "content", "sig"):
        if k not in event:
            await ok(ws, eid, False, f"missing field: {k}")
            return

    try:
        from pynostr.event import Event
        ev = Event.from_dict(event)
        if not ev.verify():
            await ok(ws, eid, False, "signature invalid")
            return
    except Exception as e:
        await ok(ws, eid, False, f"verify error: {e}")
        return

    idx = STATE.add_event(event)
    log.info(
        "EVENT kind=%d from pubkey=%s... id=%s...",
        event["kind"], event["pubkey"][:8], eid,
    )
    await ok(ws, event["id"], True, "stored")

    # Fan out to all matching subscriptions across all clients
    for other_ws, sub_map in list(STATE.subs.items()):
        for sub_id, (filts, sent) in sub_map.items():
            if idx in sent:
                continue
            # Re-check filters
            if any(_filter_matches(ev_dict := event, f) for f in filts):
                try:
                    await other_ws.send(json.dumps(["EVENT", sub_id, event]))
                    sent.add(idx)
                except Exception:
                    pass


def _filter_matches(event: dict, filt: dict) -> bool:
    if kinds := filt.get("kinds"):
        if event.get("kind") not in kinds:
            return False
    if authors := filt.get("authors"):
        if event.get("pubkey") not in authors:
            return False
    if ids := filt.get("ids"):
        if event.get("id") not in ids:
            return False
    if (since := filt.get("since")) is not None and event.get("created_at", 0) < since:
        return False
    if (until := filt.get("until")) is not None and event.get("created_at", 0) > until:
        return False
    for tag_key in ("#e", "#p", "#h"):
        if tag_vals := filt.get(tag_key):
            tag_set = set()
            for tag in (event.get("tags") or []):
                if len(tag) >= 2 and tag[0] == tag_key[1:]:
                    tag_set.add(tag[1])
            if not (set(tag_vals) & tag_set):
                return False
    return True


async def on_req(ws: ServerConnection, msg: list[Any]) -> None:
    if len(msg) < 3:
        return
    sub_id = msg[1]
    filters = [f for f in msg[2:] if isinstance(f, dict)]
    matched: set[int] = set()
    for filt in filters:
        matched.update(STATE.filter_events(filt))
    STATE.subs[ws][sub_id] = (filters, set(matched))
    log.info("REQ %s from %s → %d matches", sub_id, ws.remote_address, len(matched))
    for idx in sorted(matched):
        try:
            await ws.send(json.dumps(["EVENT", sub_id, STATE.events[idx]]))
        except Exception:
            break
    try:
        await ws.send(json.dumps(["EOSE", sub_id]))
    except Exception:
        pass


def on_close(ws: ServerConnection, msg: list[Any]) -> None:
    if len(msg) >= 2:
        sub_id = msg[1]
        STATE.subs.get(ws, {}).pop(sub_id, None)
        log.info("CLOSE %s from %s", sub_id, ws.remote_address)


async def on_auth(ws: ServerConnection, msg: list[Any]) -> None:
    if len(msg) < 2:
        return
    event = msg[1]
    expected_challenge = STATE.challenges.get(ws, "")
    if not expected_challenge:
        await ok(ws, "?", False, "no challenge issued for this connection")
        return
    if verify_auth_event(
        event,
        expected_challenge=expected_challenge,
        expected_relay_url=RELAY_URL,
    ):
        STATE.mark_authed(ws, event.get("pubkey", "?"))
        await ok(ws, event.get("id", "?"), True, "auth ok")
    else:
        await ok(ws, event.get("id", "?"), False, "auth verify failed")


async def ok(ws: ServerConnection, event_id: str, success: bool, reason: str) -> None:
    try:
        await ws.send(json.dumps(["OK", event_id, success, reason]))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# HTTP endpoints (NIP-11 + debug)
# --------------------------------------------------------------------------- #

def _json_response(body: bytes) -> Response:
    return Response(
        HTTPStatus.OK.value, "OK",
        Headers([("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")]),
        body,
    )


def _process_request(connection, request) -> Response | None:
    """websockets v17 process_request hook: returns Response to short-circuit, None to proceed.

    If the request has an `Upgrade: websocket` header, it's a WS handshake
    attempt and we MUST return None to let websockets complete the handshake.
    Only intercept plain HTTP requests.
    """
    # WS upgrade attempts carry the Upgrade header — let them through
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None

    path = request.path.split("?")[0]

    if path == "/.well-known/nostr.json":
        return _json_response(json.dumps(NIP11_METADATA, indent=2).encode())

    if path == "/health":
        return _json_response(b'{"status":"ok"}')

    if path == "/stats":
        body = json.dumps({
            "events_stored": len(STATE.events),
            "connected_clients": len(STATE.connected),
            "authed_clients": len(STATE.authed),
            "active_subscriptions": sum(len(m) for m in STATE.subs.values()),
            "relay_url": RELAY_URL,
            "max_stored_events": STATE.max_events,
        }, indent=2).encode()
        return _json_response(body)

    if path == "/events":
        return _json_response(json.dumps(STATE.events, indent=2).encode())

    if path == "/" or path == "":
        body = json.dumps({
            "name": "agentchat v1.2 echo relay",
            "endpoints": [
                "GET /.well-known/nostr.json",
                "GET /health",
                "GET /stats",
                "GET /events",
                f"WS {RELAY_URL}",
            ],
        }, indent=2).encode()
        return _json_response(body)

    return None  # unknown path → let WS handshake proceed (will fail gracefully)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

async def main() -> None:
    log.info("Starting echo relay on %s:%d (advertising %s)", HOST, PORT, RELAY_URL)
    log.info("NIP-11: http://%s:%d/.well-known/nostr.json", HOST, PORT)
    log.info("Health: http://%s:%d/health", HOST, PORT)
    log.info("Stats:  http://%s:%d/stats", HOST, PORT)
    log.info("Events: http://%s:%d/events", HOST, PORT)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with serve(
        ws_handler,
        HOST,
        PORT,
        process_request=_process_request,
        ping_interval=30,
        ping_timeout=10,
    ):
        log.info("Relay ready.")
        await stop.wait()
        log.info("Shutting down...")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Interrupted, exiting.")
        sys.exit(0)
#!/usr/bin/env python3
"""Smoke test: connect to the local echo relay, post a kind:9, subscribe, fetch.

Run from the repo root:
    .venv/bin/python tests/manual/echo_relay_smoke.py

Requires the echo relay to be running first:
    RELAY_URL=ws://127.0.0.1:9876 .venv/bin/python agentchat/nostr/echo_relay.py
"""
import asyncio
import json
import sys
from pathlib import Path

# Make sibling agentchat package importable when run from anywhere
REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from agentchat.nostr.keys import NostrKeys
from agentchat.nostr.events import build_channel_message
from agentchat.nostr.server import create_auth_event, verify_auth_event

import websockets


RELAY = "ws://127.0.0.1:9876"
RELAY_HTTP = "http://127.0.0.1:9876"


async def main():
    keys = NostrKeys.generate()
    print(f"Generated ephemeral keypair (pubkey={keys.public_key_hex[:16]}...)")

    async with websockets.connect(RELAY) as ws:
        # 1. Receive AUTH challenge
        first = json.loads(await ws.recv())
        assert first[0] == "AUTH", f"expected AUTH challenge, got {first}"
        challenge = first[1]
        print(f"[1] Got AUTH challenge: {challenge[:16]}...")

        # 2. Sign and send AUTH response
        auth_ev = create_auth_event(
            secret_key_hex=keys.private_key_hex,
            relay_url=RELAY,
            challenge=challenge,
        )
        await ws.send(json.dumps(["AUTH", auth_ev.to_dict()]))
        auth_ok = json.loads(await ws.recv())
        assert auth_ok[0] == "OK" and auth_ok[2] is True, f"auth failed: {auth_ok}"
        print(f"[2] AUTH OK: {auth_ok[3]}")

        # 3. Post a kind:9 channel message
        msg_ev = build_channel_message(
            keys=keys,
            group_id="dinner-channel",
            content="hello from the smoke test",
        )
        msg_ev.sign(keys.private_key_hex)
        await ws.send(json.dumps(["EVENT", msg_ev.to_dict()]))
        ok_resp = json.loads(await ws.recv())
        assert ok_resp[0] == "OK" and ok_resp[2] is True, f"event rejected: {ok_resp}"
        print(f"[3] EVENT stored: {ok_resp[3]} (id={ok_resp[1][:16]}...)")

        # 4. REQ to read back — filter for our own message to avoid stale events
        await ws.send(json.dumps([
            "REQ", "sub-1",
            {"kinds": [9], "#h": ["dinner-channel"], "authors": [keys.public_key_hex]},
        ]))
        seen = []
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
            m = json.loads(raw)
            if m[0] == "EOSE":
                break
            if m[0] == "EVENT":
                seen.append(m)
        assert len(seen) == 1, f"expected 1 replayed event, got {len(seen)}"
        assert seen[0][2]["content"] == "hello from the smoke test"
        print(f"[4] REQ replay: {len(seen)} event(s), content matches")

        # 5. CLOSE
        await ws.send(json.dumps(["CLOSE", "sub-1"]))
        print("[5] CLOSE sent")

    print("\n✅ All 5 steps passed.")


if __name__ == "__main__":
    asyncio.run(main())
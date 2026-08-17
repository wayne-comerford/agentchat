"""
Tests for the agent_status / focus subsystem on the Nostr bridge.

Stdlib only. These tests exercise the helpers + in-process aiohttp
app via aiohttp.test_utils.TestClient.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from agentchat.web.nostr_bridge import (
    BridgeState,
    compute_status,
    record_activity,
    set_focus,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Helpers — pure-function tests
# --------------------------------------------------------------------------- #


class TestComputeStatus:
    def test_zero_age_is_active(self):
        now = time.time()
        st = {"last_activity_ts": now, "focused_channel": None, "last_message": None}
        assert compute_status(st) == "active"

    def test_above_idle_threshold_is_idle(self):
        # Threshold is 120s — pretend activity was 200s ago.
        st = {
            "last_activity_ts": time.time() - (BridgeState.IDLE_AFTER_SECONDS + 10),
            "focused_channel": None,
            "last_message": None,
        }
        assert compute_status(st) == "idle"

    def test_above_disconnected_threshold_is_disconnected(self):
        st = {
            "last_activity_ts": time.time() - (BridgeState.DISCONNECTED_AFTER_SECONDS + 10),
            "focused_channel": None,
            "last_message": None,
        }
        assert compute_status(st) == "disconnected"


class TestRecordActivity:
    def test_creates_entry_for_unknown_agent(self):
        record_activity("fresh_agent", channel="general", last_message="hello")
        entry = BridgeState.agent_status["fresh_agent"]
        assert entry["status"] == "active"
        assert entry["focused_channel"] == "general"
        assert entry["last_message"] == "hello"
        # last_activity_ts should be very recent (within last 2s).
        assert time.time() - entry["last_activity_ts"] < 2

    def test_updates_existing_entry(self):
        record_activity("a", channel="general", last_message="first")
        first_ts = BridgeState.agent_status["a"]["last_activity_ts"]
        time.sleep(0.05)
        record_activity("a", channel="ops", last_message="second")
        entry = BridgeState.agent_status["a"]
        assert entry["focused_channel"] == "ops"
        assert entry["last_message"] == "second"
        assert entry["last_activity_ts"] >= first_ts

    def test_truncates_long_message(self):
        long_msg = "x" * 500
        record_activity("a", channel="general", last_message=long_msg)
        assert len(BridgeState.agent_status["a"]["last_message"]) == 120

    def test_broadcasts_to_subscribers(self):
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        BridgeState.agent_status_subs.append(q)
        try:
            record_activity("a", channel="general", last_message="hi")
            payload = q.get_nowait()
            assert payload["type"] == "agent_status"
            assert payload["agent"] == "a"
            assert payload["state"]["focused_channel"] == "general"
        finally:
            try:
                BridgeState.agent_status_subs.remove(q)
            except ValueError:
                pass


class TestSetFocus:
    def test_pin_channel_creates_focus_entry(self):
        set_focus("a", "general")
        assert BridgeState.focus_map["a"]["channel"] == "general"
        # Side-effect: agent_status gets an entry (idle, no activity yet).
        assert BridgeState.agent_status["a"]["focused_channel"] == "general"

    def test_clear_channel_removes_focus(self):
        set_focus("a", "general")
        set_focus("a", None)
        assert "a" not in BridgeState.focus_map
        assert BridgeState.agent_status["a"]["focused_channel"] is None

    def test_focus_pin_broadcasts_event(self):
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        BridgeState.agent_status_subs.append(q)
        try:
            set_focus("a", "ops")
            payload = q.get_nowait()
            assert payload["type"] == "focus"
            assert payload["agent"] == "a"
            assert payload["channel"] == "ops"
        finally:
            try:
                BridgeState.agent_status_subs.remove(q)
            except ValueError:
                pass


# --------------------------------------------------------------------------- #
# Route tests — exercise the in-process aiohttp app via TestClient
# --------------------------------------------------------------------------- #


def _make_app():
    """Build a fresh aiohttp app with startup stubbed out (no real keys)."""
    from agentchat.web import nostr_bridge as nb

    config = {
        "listen": {"host": "127.0.0.1", "port": 0},
        "relays": ["ws://127.0.0.1:9876"],
        "identity": {"key_path": "nokey", "name": "test"},
        "channels": [{"id": "general", "name": "#general"}, {"id": "ops", "name": "#ops"}],
    }
    app = nb.make_app(config)  # type: ignore[attr-defined]

    async def _noop_startup(self):
        self.keys = None
        self.pool = None
        self.registry = {}

    app.on_startup.clear()
    # Patch + remember the original so we can restore it for other tests.
    original_startup = nb.BridgeState.startup
    nb.BridgeState.startup = _noop_startup  # type: ignore[assignment]
    app._original_startup = original_startup  # type: ignore[attr-defined]
    return app


@pytest.mark.asyncio
async def test_focus_get_returns_empty_when_unset():
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app()
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        resp = await cli.get("/v1/ui/focus")
        assert resp.status == 200
        body = await resp.json()
        assert body == {}
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_focus_post_requires_session():
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app()
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        resp = await cli.post("/v1/ui/focus", json={"agent": "a", "channel": "general"})
        assert resp.status == 401
        body = await resp.json()
        assert "login required" in body["error"]
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_focus_post_requires_agent():
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app()
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        resp = await cli.post(
            "/v1/ui/focus",
            json={"channel": "general"},
            headers={"Cookie": "agentchat_session=hermes"},
        )
        assert resp.status == 400
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_focus_post_pin_and_clear():
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app()
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        # Pin
        resp = await cli.post(
            "/v1/ui/focus",
            json={"agent": "hermes", "channel": "general"},
            headers={"Cookie": "agentchat_session=hermes"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"ok": True, "agent": "hermes", "channel": "general"}
        # Confirm via GET
        resp2 = await cli.get("/v1/ui/focus")
        assert resp2.status == 200
        focus = await resp2.json()
        assert focus["hermes"]["channel"] == "general"
        # Clear
        resp3 = await cli.post(
            "/v1/ui/focus",
            json={"agent": "hermes", "channel": None},
            headers={"Cookie": "agentchat_session=hermes"},
        )
        assert resp3.status == 200
        resp4 = await cli.get("/v1/ui/focus")
        focus2 = await resp4.json()
        assert "hermes" not in focus2
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_agents_endpoint_includes_status_entry():
    """After record_activity + set_focus, /v1/ui/agents returns status_entry."""
    from aiohttp.test_utils import TestClient, TestServer
    from agentchat.web import nostr_bridge as nb

    app = _make_app()
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        # Pretend there's an agent in the registry (set directly on the app state).
        state = cli.app["state"]
        state.registry = {"hermes": {"npub": "npub1hermes", "public_key_hex": "aa" * 32}}
        record_activity("hermes", channel="general", last_message="hi")
        set_focus("hermes", "general")
        resp = await cli.get("/v1/ui/agents")
        assert resp.status == 200
        body = await resp.json()
        assert len(body) == 1
        a = body[0]
        assert a["name"] == "hermes"
        se = a["status_entry"]
        assert se is not None
        assert se["status"] == "active"
        assert se["focused_channel"] == "general"
        assert "age_seconds" in se
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_agent_status_sse_sends_snapshot_then_events():
    """Connect to /v1/ui/stream?channel=agent_status and assert the initial
    snapshot contains current state, plus a subsequent event arrives when
    record_activity() is called from another task."""
    import json as _json
    from aiohttp.test_utils import TestClient, TestServer
    from agentchat.web import nostr_bridge as nb

    app = _make_app()
    # Seed AFTER _make_app (which resets state).
    record_activity("hermes", channel="general", last_message="seed")

    received: list[tuple[str, dict]] = []
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        # Open the SSE stream in a background task.
        async def consume():
            async with cli.get("/v1/ui/stream?channel=agent_status") as resp:
                assert resp.status == 200
                count = 0
                buf = b""
                async for chunk in resp.content.iter_any():
                    buf += chunk
                    while b"\n\n" in buf:
                        block, buf = buf.split(b"\n\n", 1)
                        lines = block.decode(errors="replace").splitlines()
                        evt_name = "message"
                        data_lines = []
                        for ln in lines:
                            if ln.startswith("event: "):
                                evt_name = ln[len("event: "):].strip()
                            elif ln.startswith("data: "):
                                data_lines.append(ln[len("data: "):])
                        if data_lines:
                            try:
                                payload = _json.loads("\n".join(data_lines))
                            except Exception:
                                continue
                            received.append((evt_name, payload))
                            count += 1
                            if count >= 2:
                                return

        task = asyncio.create_task(consume())
        # Give the SSE handler a moment to register its queue.
        await asyncio.sleep(0.2)
        # Trigger a second activity — should be pushed to all subscribers.
        record_activity("hermes", channel="ops", last_message="trigger")
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()
            raise

        evt_names = [n for n, _ in received]
        assert "snapshot" in evt_names, f"expected snapshot, got {evt_names}"
        assert "agent_status" in evt_names, f"expected agent_status, got {evt_names}"
        # Snapshot had hermes seeded.
        snap = next(p for n, p in received if n == "snapshot")
        assert "hermes" in snap["agents"]
        # Triggered event had the new channel.
        trig = next(p for n, p in received if n == "agent_status")
        assert trig["state"]["focused_channel"] == "ops"
    finally:
        await cli.close()

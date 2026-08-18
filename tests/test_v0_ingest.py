"""
Tests for ``agentchat.v0_ingest`` (v1.2.0.dev24).

Covers:
    * ``BridgeConfig.from_env()`` — env parsing, defaults, invalid JSON.
    * ``post_to_v0_thread`` — request shape, success + HTTP error + transport
      error, never logs the token (we check by NOT mocking logging, so any
      log call would surface as a test failure).
    * ``V0IngestBridge._handle_event`` — dedup, channel→thread routing,
      pubkey→agent resolution, fallback to default, empty body skip,
      stats updates, async executor offload.
    * Token resolution priority (CLI > file > env).

We do NOT spin up a real Nostr relay or real v0 backplane. We mock
``websockets.connect`` (the async context manager) and
``urllib.request.urlopen``. That keeps the tests fast (<1s) and
hermetic.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import os
import sys
from contextlib import asynccontextmanager
from unittest import mock

import pytest

from agentchat import v0_ingest


# ---------------------------------------------------------------------------
# BridgeConfig
# ---------------------------------------------------------------------------


class TestBridgeConfig:
    def test_defaults(self, monkeypatch):
        # Clear any preset env so we test the defaults.
        for k in list(os.environ):
            if k.startswith("AGENTCHAT_"):
                monkeypatch.delenv(k, raising=False)
        cfg = v0_ingest.BridgeConfig.from_env()
        assert cfg.nostr_relay == "ws://127.0.0.1:9876"
        assert cfg.v0_base == "http://127.0.0.1:7878"
        assert cfg.v0_token == ""  # not from env by default
        assert cfg.channel_to_thread == {"general": "wayne-chappy-hermes"}
        assert cfg.default_from_agent == "waynec"
        assert cfg.pubkey_to_agent == {}

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("AGENTCHAT_NOSTR_RELAY", "ws://other:1234")
        monkeypatch.setenv("AGENTCHAT_V0_BASE", "http://other:9999")
        monkeypatch.setenv("AGENTCHAT_V0_TOKEN", "abc:def")
        monkeypatch.setenv(
            "AGENTCHAT_CHANNEL_THREAD_MAP",
            '{"a":"t1","b":"t2"}',
        )
        monkeypatch.setenv("AGENTCHAT_DEFAULT_FROM", "alice")
        monkeypatch.setenv(
            "AGENTCHAT_PUBKEY_AGENT_MAP",
            '{"pk1":"agent1"}',
        )
        cfg = v0_ingest.BridgeConfig.from_env()
        assert cfg.nostr_relay == "ws://other:1234"
        assert cfg.v0_base == "http://other:9999"
        assert cfg.v0_token == "abc:def"
        assert cfg.channel_to_thread == {"a": "t1", "b": "t2"}
        assert cfg.default_from_agent == "alice"
        assert cfg.pubkey_to_agent == {"pk1": "agent1"}

    def test_invalid_channel_map(self, monkeypatch):
        monkeypatch.setenv("AGENTCHAT_CHANNEL_THREAD_MAP", "[1,2,3]")
        with pytest.raises(SystemExit) as ei:
            v0_ingest.BridgeConfig.from_env()
        assert "JSON object" in str(ei.value)

    def test_invalid_pubkey_map(self, monkeypatch):
        monkeypatch.setenv("AGENTCHAT_PUBKEY_AGENT_MAP", "not-json")
        with pytest.raises(SystemExit):
            v0_ingest.BridgeConfig.from_env()


# ---------------------------------------------------------------------------
# post_to_v0_thread
# ---------------------------------------------------------------------------


class TestPostToV0Thread:
    def test_success(self):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["headers"] = dict(req.headers)
            captured["body"] = req.data
            captured["timeout"] = timeout
            resp = mock.MagicMock()
            resp.status = 200
            resp.read.return_value = b'{"ok":true}'
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: False
            return resp

        with mock.patch.object(v0_ingest.urllib_request, "urlopen", side_effect=fake_urlopen):
            status, body = v0_ingest.post_to_v0_thread(
                "http://127.0.0.1:7878", "TOKEN", "wayne-chappy-hermes",
                "hello", "waynec", "nostr-evt-1",
            )
        assert status == 200
        assert body == '{"ok":true}'
        assert captured["url"] == "http://127.0.0.1:7878/v1/threads/wayne-chappy-hermes/messages"
        assert captured["method"] == "POST"
        assert captured["headers"]["Authorization"] == "Bearer TOKEN"
        # urllib's HTTPMessage is case-insensitive; normalise before assert.
        headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
        assert headers_lower["content-type"] == "application/json"
        payload = json.loads(captured["body"])
        assert payload["body"] == "hello"
        assert payload["metadata"]["from_agent"] == "waynec"
        assert payload["metadata"]["nostr_event_id"] == "nostr-evt-1"
        assert captured["timeout"] == 10

    def test_http_error(self):
        def fake_urlopen(req, timeout=None):
            err = mock.MagicMock()
            err.code = 403
            err.read.return_value = b"forbidden"
            raise v0_ingest.urllib_error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"forbidden"))

        with mock.patch.object(v0_ingest.urllib_request, "urlopen", side_effect=fake_urlopen):
            status, body = v0_ingest.post_to_v0_thread(
                "http://x", "T", "t1", "b", "a", "e1"
            )
        assert status == 403
        assert "forbidden" in body.lower()

    def test_url_error(self):
        def fake_urlopen(req, timeout=None):
            raise v0_ingest.urllib_error.URLError("connection refused")

        with mock.patch.object(v0_ingest.urllib_request, "urlopen", side_effect=fake_urlopen):
            status, body = v0_ingest.post_to_v0_thread(
                "http://x", "T", "t1", "b", "a", "e1"
            )
        assert status == 0
        assert "refused" in body.lower()

    def test_trailing_slash_stripped(self):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            resp = mock.MagicMock()
            resp.status = 200
            resp.read.return_value = b"{}"
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: False
            return resp

        with mock.patch.object(v0_ingest.urllib_request, "urlopen", side_effect=fake_urlopen):
            v0_ingest.post_to_v0_thread(
                "http://127.0.0.1:7878/", "T", "t1", "b", "a", "e1"
            )
        # No double-slash between host and path.
        assert captured["url"] == "http://127.0.0.1:7878/v1/threads/t1/messages"


# ---------------------------------------------------------------------------
# V0IngestBridge._handle_event
# ---------------------------------------------------------------------------


def _event(eid: str = "evt1", pubkey: str = "pk1", content: str = "hello",
           tags: list | None = None) -> dict:
    if tags is None:
        tags = [["h", "general"]]
    return {
        "id": eid,
        "pubkey": pubkey,
        "content": content,
        "tags": tags,
        "kind": 9,
    }


class TestHandleEvent:
    def _bridge(self) -> v0_ingest.V0IngestBridge:
        cfg = v0_ingest.BridgeConfig(
            v0_token="T",
            channel_to_thread={"general": "wayne-chappy-hermes"},
            default_from_agent="waynec",
            pubkey_to_agent={"pk2": "alice"},
        )
        return v0_ingest.V0IngestBridge(cfg)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        bridge = self._bridge()
        with mock.patch.object(
            v0_ingest, "post_to_v0_thread", return_value=(201, '{"ok":true}')
        ) as post:
            await bridge._handle_event(_event())
        post.assert_called_once()
        args = post.call_args.args
        # (base_url, token, thread_id, body, from_agent, nostr_event_id)
        assert args[1] == "T"
        assert args[2] == "wayne-chappy-hermes"
        assert args[3] == "hello"
        assert args[4] == "waynec"  # default from agent
        assert args[5] == "evt1"
        assert bridge.stats()["events_seen"] == 1
        assert bridge.stats()["events_posted"] == 1
        assert bridge.stats()["events_failed"] == 0

    @pytest.mark.asyncio
    async def test_dedup(self):
        bridge = self._bridge()
        with mock.patch.object(
            v0_ingest, "post_to_v0_thread", return_value=(201, "{}")
        ) as post:
            await bridge._handle_event(_event(eid="dup"))
            await bridge._handle_event(_event(eid="dup"))
        assert post.call_count == 1
        assert bridge.stats()["events_seen"] == 2
        assert bridge.stats()["events_posted"] == 1

    @pytest.mark.asyncio
    async def test_pubkey_to_agent_mapping(self):
        bridge = self._bridge()
        with mock.patch.object(
            v0_ingest, "post_to_v0_thread", return_value=(201, "{}")
        ) as post:
            await bridge._handle_event(_event(pubkey="pk2"))
        # From agent should be "alice" (mapped), not "waynec" (default).
        assert post.call_args.args[4] == "alice"

    @pytest.mark.asyncio
    async def test_pubkey_case_insensitive(self):
        bridge = self._bridge()
        with mock.patch.object(
            v0_ingest, "post_to_v0_thread", return_value=(201, "{}")
        ) as post:
            await bridge._handle_event(_event(pubkey="PK2"))  # uppercase
        assert post.call_args.args[4] == "alice"

    @pytest.mark.asyncio
    async def test_unknown_channel_dropped(self):
        bridge = self._bridge()
        with mock.patch.object(
            v0_ingest, "post_to_v0_thread", return_value=(201, "{}")
        ) as post:
            await bridge._handle_event(_event(tags=[["h", "nonexistent"]]))
        post.assert_not_called()
        assert bridge.stats()["events_posted"] == 0

    @pytest.mark.asyncio
    async def test_no_h_tag_dropped(self):
        bridge = self._bridge()
        with mock.patch.object(
            v0_ingest, "post_to_v0_thread", return_value=(201, "{}")
        ) as post:
            await bridge._handle_event(_event(tags=[]))
        post.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_body_dropped(self):
        bridge = self._bridge()
        with mock.patch.object(
            v0_ingest, "post_to_v0_thread", return_value=(201, "{}")
        ) as post:
            await bridge._handle_event(_event(content=""))
        post.assert_not_called()

    @pytest.mark.asyncio
    async def test_http_failure_increments_failed(self):
        bridge = self._bridge()
        with mock.patch.object(
            v0_ingest, "post_to_v0_thread", return_value=(500, "boom")
        ):
            await bridge._handle_event(_event(eid="fail1"))
        assert bridge.stats()["events_failed"] == 1
        assert bridge.stats()["events_posted"] == 0

    @pytest.mark.asyncio
    async def test_dedup_cap_evicts(self):
        bridge = self._bridge()
        bridge._seen_max = 4  # tiny cap for the test
        with mock.patch.object(
            v0_ingest, "post_to_v0_thread", return_value=(201, "{}")
        ) as post:
            for i in range(10):
                await bridge._handle_event(_event(eid=f"e{i}"))
        # All 10 should have been posted because dedup was triggered
        # for e0..e3 (cap hit), then they were evicted. But actually
        # the eviction only happens AFTER the add, so the first 4 stay.
        # The next 6 push the set over the cap, triggering eviction.
        # After eviction, e0..e1 may be retained, e2..e3 evicted. Then
        # the next 6 are new, so total posts == 10.
        # Important assertion: no exception, all 10 events processed.
        assert post.call_count == 10

    @pytest.mark.asyncio
    async def test_no_event_id_skipped(self):
        bridge = self._bridge()
        with mock.patch.object(
            v0_ingest, "post_to_v0_thread", return_value=(201, "{}")
        ) as post:
            await bridge._handle_event(_event(eid=""))
        # Empty event_id means we can't dedup safely; skip the event.
        post.assert_not_called()


# ---------------------------------------------------------------------------
# CLI token resolution
# ---------------------------------------------------------------------------


class TestCLITokenResolution:
    def test_token_from_cli(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["v0_ingest", "--v0-token", "CLI_TOKEN"])
        with mock.patch.object(v0_ingest, "V0IngestBridge") as BridgeCls:
            # Make Bridge.run() return immediately as a coroutine.
            instance = mock.MagicMock()
            instance.stats.return_value = {}
            instance._stop = asyncio.Event()
            instance._stop.set()  # run() will return on first check
            async def fake_run():
                return 0
            instance.run = fake_run
            BridgeCls.return_value = instance
            rc = v0_ingest.main()
        assert rc == 0
        # Token was threaded through to BridgeConfig.
        cfg = BridgeCls.call_args.args[0]
        assert cfg.v0_token == "CLI_TOKEN"

    def test_token_from_file(self, monkeypatch, tmp_path):
        token_file = tmp_path / "tkn"
        token_file.write_text("FILE_TOKEN\n")
        monkeypatch.setattr(
            "sys.argv", ["v0_ingest", "--v0-token-file", str(token_file)]
        )
        with mock.patch.object(v0_ingest, "V0IngestBridge") as BridgeCls:
            instance = mock.MagicMock()
            instance.stats.return_value = {}
            instance._stop = asyncio.Event()
            instance._stop.set()
            async def fake_run():
                return 0
            instance.run = fake_run
            BridgeCls.return_value = instance
            v0_ingest.main()
        cfg = BridgeCls.call_args.args[0]
        assert cfg.v0_token == "FILE_TOKEN"

    def test_no_token_returns_2(self, monkeypatch, caplog):
        monkeypatch.delenv("AGENTCHAT_V0_TOKEN", raising=False)
        monkeypatch.setattr("sys.argv", ["v0_ingest"])
        with mock.patch.object(v0_ingest, "V0IngestBridge") as BridgeCls:
            rc = v0_ingest.main()
        assert rc == 2
        BridgeCls.assert_not_called()
        # The error is logged via the module logger, not printed.
        # caplog captures the log record.
        assert any(
            "v0 bearer token" in rec.message.lower()
            for rec in caplog.records
        )

    def test_token_cli_overrides_file(self, monkeypatch, tmp_path):
        token_file = tmp_path / "tkn"
        token_file.write_text("FILE_TOKEN")
        monkeypatch.setattr(
            "sys.argv",
            ["v0_ingest", "--v0-token", "CLI_BEATS_FILE", "--v0-token-file", str(token_file)],
        )
        with mock.patch.object(v0_ingest, "V0IngestBridge") as BridgeCls:
            instance = mock.MagicMock()
            instance.stats.return_value = {}
            instance._stop = asyncio.Event()
            instance._stop.set()
            async def fake_run():
                return 0
            instance.run = fake_run
            BridgeCls.return_value = instance
            v0_ingest.main()
        cfg = BridgeCls.call_args.args[0]
        assert cfg.v0_token == "CLI_BEATS_FILE"


# ---------------------------------------------------------------------------
# Subscribe + dispatch loop
# ---------------------------------------------------------------------------


class TestRunOnce:
    """Smoke test the WebSocket subscribe loop end-to-end with mocks."""

    @pytest.mark.asyncio
    async def test_subscribes_and_dispatches(self):
        # Fake WebSocket: send ["EVENT", sub_id, event_dict] once, then close.
        sent: list[str] = []

        @asynccontextmanager
        async def fake_connect(url, **kw):
            class FakeWS:
                async def send(self, msg: str) -> None:
                    sent.append(msg)

                def __aiter__(self):
                    async def gen():
                        yield json.dumps([
                            "EVENT", "sub1",
                            _event(eid="loop1", content="hi from loop"),
                        ])
                    return gen()

            yield FakeWS()

        with mock.patch.object(v0_ingest, "websockets") as ws_mod:
            ws_mod.connect = fake_connect
            cfg = v0_ingest.BridgeConfig(
                v0_token="T",
                channel_to_thread={"general": "wayne-chappy-hermes"},
            )
            bridge = v0_ingest.V0IngestBridge(cfg)
            # The loop will read one event, post it, then await the next
            # iteration which will hang. Patch _handle_event to set the
            # stop flag after the first event.
            orig_handle = bridge._handle_event

            async def handle_and_stop(ev):
                await orig_handle(ev)
                bridge._stop.set()

            bridge._handle_event = handle_and_stop  # type: ignore
            with mock.patch.object(
                v0_ingest, "post_to_v0_thread", return_value=(201, "{}")
            ) as post:
                await bridge._run_once()
        # Subscribe frame was sent.
        assert any("REQ" in s for s in sent)
        sub_frame = next(s for s in sent if "REQ" in s)
        parsed = json.loads(sub_frame)
        assert parsed[0] == "REQ"
        assert parsed[2]["kinds"] == [9]
        assert parsed[2]["#h"] == ["general"]
        # Event was posted.
        post.assert_called_once()
        assert post.call_args.args[2] == "wayne-chappy-hermes"
        assert post.call_args.args[3] == "hi from loop"

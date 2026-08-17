"""
Tests for the shared team-memory HTTP bridge (t_35158960).

Stdlib only. Uses a per-test isolated ``AGENTCHAT_MEMORY_DIR`` so tests
don't trample each other or the real ``~/.hermes/memory/``.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from agentchat import memory as memory_store


REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Pure-helper tests (no HTTP) — exercise the shared tier directly
# --------------------------------------------------------------------------- #


class TestSharedMemoryHelpers:
    def test_list_team_sections_empty(self, memroot):
        sections = memory_store.list_team_sections()
        # Empty markdown → either an empty list, or a single (None, '') preamble.
        # The H1 preamble rule means we get at least the title-None entry.
        assert isinstance(sections, list)

    def test_replace_then_list(self, memroot):
        memory_store.replace_team_section("Decisions", ["Use X for Y", "X is fast"])
        sections = memory_store.list_team_sections()
        titles = [s["title"] for s in sections]
        assert "Decisions" in titles
        idx = titles.index("Decisions")
        assert sections[idx]["lines"] == ["Use X for Y", "X is fast"]

    def test_replace_creates_section(self, memroot):
        memory_store.replace_team_section("NewOne", ["first line"])
        # Replace again — should not duplicate.
        memory_store.replace_team_section("NewOne", ["first line", "second line"])
        sections = memory_store.list_team_sections()
        titles = [s["title"] for s in sections]
        idx = titles.index("NewOne")
        assert sections[idx]["lines"] == ["first line", "second line"]
        # Count how many "NewOne" sections exist — should be exactly 1.
        assert titles.count("NewOne") == 1

    def test_append_with_attribution(self, memroot):
        memory_store.append_team_line("Log", "First event", author="hermes")
        memory_store.append_team_line("Log", "Second event", author="chappy")
        sections = memory_store.list_team_sections()
        titles = [s["title"] for s in sections]
        idx = titles.index("Log")
        lines = sections[idx]["lines"]
        # Lines include the attribution trail on the appended entry.
        assert any("First event" in ln for ln in lines)
        assert any("Second event" in ln for ln in lines)
        # Each line carries an author tag from the writer.
        author_lines = [ln for ln in lines if "hermes" in ln or "chappy" in ln]
        assert len(author_lines) >= 2

    def test_remove_line(self, memroot):
        memory_store.replace_team_section("Items", ["a", "b", "c"])
        ok = memory_store.remove_team_line("Items", 1)
        assert ok is True
        sections = memory_store.list_team_sections()
        titles = [s["title"] for s in sections]
        idx = titles.index("Items")
        assert sections[idx]["lines"] == ["a", "c"]

    def test_remove_missing_returns_false(self, memroot):
        memory_store.replace_team_section("Items", ["a"])
        # Out of range
        assert memory_store.remove_team_line("Items", 5) is False
        # Section missing
        assert memory_store.remove_team_line("Nope", 0) is False

    def test_concurrent_writes_dont_corrupt(self, memroot):
        """Two threads racing on the same file should both succeed with
        flock serialization — no torn writes, no exceptions."""
        results: list[bool] = []
        errors: list[Exception] = []

        def writer(label: str, n: int):
            try:
                for i in range(n):
                    memory_store.append_team_line(
                        "Concurrent", f"{label}-{i}", author=label
                    )
                results.append(True)
            except Exception as e:
                errors.append(e)
                results.append(False)

        t1 = threading.Thread(target=writer, args=("hermes", 5))
        t2 = threading.Thread(target=writer, args=("chappy", 5))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert errors == [], f"concurrent writes raised: {errors}"
        assert results == [True, True]
        # All 10 logical writes should be present. Each append emits a
        # content line + a <sub>— author @ ISO</sub> attribution line, so
        # we expect ~20 lines (filter by content to assert 10 actual
        # events landed).
        sections = memory_store.list_team_sections()
        titles = [s["title"] for s in sections]
        idx = titles.index("Concurrent")
        content_lines = [
            ln for ln in sections[idx]["lines"]
            if "<sub>" not in ln
        ]
        assert len(content_lines) == 10


# --------------------------------------------------------------------------- #
# HTTP route tests — exercise the in-process aiohttp app
# --------------------------------------------------------------------------- #


def _make_app():
    """Build a fresh aiohttp app with startup stubbed out (no real keys)."""
    from agentchat.web import nostr_bridge as nb

    config = {
        "listen": {"host": "127.0.0.1", "port": 0},
        "relays": ["ws://127.0.0.1:9876"],
        "identity": {"key_path": "nokey", "name": "test"},
        "channels": [{"id": "general", "name": "#general"}],
    }
    app = nb.make_app(config)  # type: ignore[attr-defined]

    async def _noop_startup(self):
        self.keys = None
        self.pool = None
        self.registry = {}

    app.on_startup.clear()
    nb.BridgeState.startup = _noop_startup  # type: ignore[assignment]
    return app


@pytest.fixture
def memroot(monkeypatch, tmp_path):
    """Per-test isolated memory root."""
    monkeypatch.setenv("AGENTCHAT_MEMORY_DIR", str(tmp_path))
    yield tmp_path


@pytest.mark.asyncio
async def test_shared_get_returns_sections(memroot):
    from aiohttp.test_utils import TestClient, TestServer
    memory_store.replace_team_section("Goals", ["ship v1", "keep tests green"])
    app = _make_app()
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        resp = await cli.get("/v1/ui/memory/shared")
        assert resp.status == 200
        body = await resp.json()
        assert "sections" in body
        assert "raw" in body
        titles = [s["title"] for s in body["sections"]]
        assert "Goals" in titles
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_shared_replace_requires_session(memroot):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app()
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        resp = await cli.put(
            "/v1/ui/memory/shared/sections/Goals",
            json={"lines": ["x"]},
        )
        assert resp.status == 401
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_shared_replace_with_session_succeeds(memroot):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app()
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        resp = await cli.put(
            "/v1/ui/memory/shared/sections/Goals",
            json={"lines": ["ship v1", "keep tests green"]},
            headers={"Cookie": "agentchat_session=hermes"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
        assert body["section"] == "Goals"
        assert body["by"] == "hermes"
        # Verify it persisted
        sections = memory_store.list_team_sections()
        titles = [s["title"] for s in sections]
        idx = titles.index("Goals")
        assert sections[idx]["lines"] == ["ship v1", "keep tests green"]
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_shared_append_then_delete_line(memroot):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app()
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        # Append as hermes
        r1 = await cli.post(
            "/v1/ui/memory/shared/sections/Log/lines",
            json={"line": "first event"},
            headers={"Cookie": "agentchat_session=hermes"},
        )
        assert r1.status == 200
        # Append as chappy
        r2 = await cli.post(
            "/v1/ui/memory/shared/sections/Log/lines",
            json={"line": "second event"},
            headers={"Cookie": "agentchat_session=chappy"},
        )
        assert r2.status == 200
        # Verify both persisted with attribution
        sections = memory_store.list_team_sections()
        titles = [s["title"] for s in sections]
        idx = titles.index("Log")
        lines = sections[idx]["lines"]
        assert any("first event" in ln for ln in lines)
        assert any("second event" in ln for ln in lines)
        # Delete the first line (0)
        r3 = await cli.delete(
            "/v1/ui/memory/shared/sections/Log/lines/0",
            headers={"Cookie": "agentchat_session=hermes"},
        )
        assert r3.status == 200
        # Delete an out-of-range line → 404
        r4 = await cli.delete(
            "/v1/ui/memory/shared/sections/Log/lines/99",
            headers={"Cookie": "agentchat_session=hermes"},
        )
        assert r4.status == 404
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_shared_delete_requires_session(memroot):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app()
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        resp = await cli.delete("/v1/ui/memory/shared/sections/X/lines/0")
        assert resp.status == 401
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_shared_concurrent_writes_via_http(memroot):
    """End-to-end: 5 concurrent PUT requests from different sessions all
    succeed; no torn writes; final state has all 5 sections."""
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app()
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        async def put(i: int):
            r = await cli.put(
                f"/v1/ui/memory/shared/sections/Item{i}",
                json={"lines": [f"line {i}"]},
                headers={"Cookie": f"agentchat_session=agent{i}"},
            )
            return r.status

        results = await asyncio.gather(*[put(i) for i in range(5)])
        assert results == [200, 200, 200, 200, 200]
        # All 5 sections present
        sections = memory_store.list_team_sections()
        titles = [s["title"] for s in sections]
        for i in range(5):
            assert f"Item{i}" in titles
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_shared_replace_validates_lines_must_be_list(memroot):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app()
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        resp = await cli.put(
            "/v1/ui/memory/shared/sections/Goals",
            json={"lines": "not a list"},
            headers={"Cookie": "agentchat_session=hermes"},
        )
        assert resp.status == 400
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_shared_append_validates_line_required(memroot):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app()
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        resp = await cli.post(
            "/v1/ui/memory/shared/sections/Log/lines",
            json={"line": "   "},
            headers={"Cookie": "agentchat_session=hermes"},
        )
        assert resp.status == 400
    finally:
        await cli.close()

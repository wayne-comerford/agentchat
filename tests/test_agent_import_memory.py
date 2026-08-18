"""
Tests for the single-file memory import flow + atomic agent create.

Powers the Add Agent wizard in /settings. See kanban t_20f29edb.

Coverage:

  memory_import module (pure parse + write):
    1.  test_parse_basic              — 3 sections parsed, counts right
    2.  test_parse_empty              — empty input → warning, no crash
    3.  test_parse_intro_preserved    — H1 preamble kept as a section
    4.  test_parse_warnings           — duplicate section, blank line warnings
    5.  test_import_text_writes_file  — import_text writes to disk
    6.  test_import_text_makes_backup — existing file → .bak created
    7.  test_import_text_oversize     — > cap → OversizeInput
    8.  test_import_text_invalid_name — bad name → InvalidAgentName
    9.  test_import_file_round_trip   — file on disk → import_text equivalent

  bridge handlers (atomic create, preview, import-memory):
    10. test_create_agent_with_memory_atomic — POST /v1/ui/agents
    11. test_create_agent_no_memory           — just create, no memory
    12. test_create_agent_duplicate           — 409 on existing name
    13. test_create_agent_no_session          — 401
    14. test_create_agent_invalid_role        — 400
    15. test_preview_endpoint                 — GET parses without writing
    16. test_preview_read_only                — preview does NOT create a file
    17. test_import_memory_inline_json       — POST import-memory JSON
    18. test_import_memory_upload_multipart   — POST import-memory multipart
    19. test_import_memory_no_session         — 401
    20. test_import_memory_oversize_inline    — 413
    21. test_import_memory_creates_backup     — re-import makes .bak

Uses the in-process aiohttp TestClient (no real keypair, no real network)
following the same pattern as tests/test_agent_status.py.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agentchat import memory as memory_store
from agentchat import memory_import


# --------------------------------------------------------------------------- #
# App factory — bridge with stubbed startup
# --------------------------------------------------------------------------- #

def _make_app(tmp_path, monkeypatch):
    """Bridge app with stubbed startup; memory path sandboxed to tmp_path."""
    from agentchat.web import nostr_bridge as nb

    # Sandbox the memory file writes
    monkeypatch.setattr(
        memory_store, "agent_memory_path",
        lambda name: tmp_path / f"{name}.md",
    )

    # Sandbox the nostr registry dir
    nostr_dir = tmp_path / "nostr"
    nostr_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENTCHAT_NOSTR_DIR", str(nostr_dir))

    config = {
        "listen": {"host": "127.0.0.1", "port": 0},
        "relays": ["ws://127.0.0.1:9876"],
        "identity": {"key_path": "nokey", "name": "test"},
        "channels": [{"id": "general", "name": "#general"}],
    }
    bridge_app = nb.make_app(config)

    async def _noop_startup(self):
        self.keys = None
        self.pool = None
        self.registry = {}

    bridge_app.on_startup.clear()
    nb.BridgeState.startup = _noop_startup  # type: ignore[assignment]
    return bridge_app


AUTH_HEADERS = {"Cookie": "agentchat_session=hermes"}


# --------------------------------------------------------------------------- #
# memory_import module — pure parse + write tests
# --------------------------------------------------------------------------- #

def test_parse_basic():
    text = (
        "# claude-code\n\n"
        "## Identity\n"
        "- Anthropic's coding agent\n"
        "- Markdown-first\n\n"
        "## Preferences\n"
        "- terse replies\n"
        "- code blocks over prose\n"
    )
    p = memory_import.parse_memory_md(text)
    # H1 + 2 H2 = 3 section titles
    assert p.section_titles == ["claude-code", "Identity", "Preferences"]
    assert p.total_line_count == 4
    assert p.intro_line_count == 0
    assert p.warnings == []


def test_parse_empty():
    p = memory_import.parse_memory_md("")
    assert "input is empty" in p.warnings
    assert p.sections == []


def test_parse_intro_preserved():
    # H1 + intro line, then H2 section. Per _split_sections, the
    # H1 itself is a section (title='hermes', body='') and the
    # intro line is its body. Lines preserve bullet markers — that
    # matches list_agent_sections() behaviour.
    text = (
        "# hermes\n\n"
        "An AI assistant for Wayne Comerford.\n\n"
        "## Identity\n"
        "- male\n"
    )
    p = memory_import.parse_memory_md(text)
    # H1 is the first section; intro text belongs to it.
    assert p.section_titles[0] == "hermes"
    assert "Identity" in p.section_titles
    # H1 section has the intro as its body lines.
    h1_section = p.sections[0]
    assert h1_section.title == "hermes"
    assert "An AI assistant for Wayne Comerford." in h1_section.lines
    # Find the Identity section and confirm it has '- male'
    identity = next(s for s in p.sections if s.title == "Identity")
    assert identity.lines == ["- male"]


def test_parse_warnings():
    # Duplicates + blank line inside section
    text = (
        "## Identity\n"
        "- line 1\n"
        "\n"
        "- line 2\n\n"
        "## identity\n"   # duplicate (case-insensitive)
        "- other\n"
    )
    p = memory_import.parse_memory_md(text)
    joined = " | ".join(p.warnings)
    assert "duplicate section" in joined
    assert "blank line" in joined


def test_import_text_writes_file(tmp_path):
    text = (
        "# claude-code\n\n"
        "## Identity\n"
        "- Anthropic\n"
    )
    with patch.object(memory_store, "agent_memory_path",
                      lambda name: tmp_path / f"{name}.md"):
        r = memory_import.import_text("claude-code", text)
    assert r.agent == "claude-code"
    # H1 + Identity = 2 sections
    assert r.sections_imported == 2
    assert r.lines_imported == 1
    assert r.backup_path is None
    assert "Anthropic" in (tmp_path / "claude-code.md").read_text()


def test_import_text_makes_backup(tmp_path):
    (tmp_path / "hermes.md").write_text("# hermes\n\n## Old\n- a\n")
    text = "# hermes\n\n## New\n- b\n"
    with patch.object(memory_store, "agent_memory_path",
                      lambda name: tmp_path / f"{name}.md"):
        r = memory_import.import_text("hermes", text)
    assert r.backup_path is not None
    bak = Path(r.backup_path)
    assert bak.exists()
    assert "Old" in bak.read_text()
    live = (tmp_path / "hermes.md").read_text()
    assert "New" in live
    assert "Old" not in live


def test_import_text_oversize():
    huge = "x" * (memory_import.MAX_INLINE_BYTES + 1)
    with pytest.raises(memory_import.OversizeInput) as ei:
        memory_import.import_text("any", huge)
    assert ei.value.cap == memory_import.MAX_INLINE_BYTES
    assert ei.value.size > memory_import.MAX_INLINE_BYTES


def test_import_text_invalid_name():
    with pytest.raises(memory_import.InvalidAgentName):
        memory_import.import_text("", "anything")
    with pytest.raises(memory_import.InvalidAgentName):
        memory_import.import_text("-bad", "anything")
    with pytest.raises(memory_import.InvalidAgentName):
        memory_import.import_text("bad name!", "anything")


def test_import_file_round_trip(tmp_path):
    src = tmp_path / "incoming.md"
    src.write_text(
        "# imported\n\n## Long-term\n- lives on Node2\n- works with hermes\n"
    )
    with patch.object(memory_store, "agent_memory_path",
                      lambda name: tmp_path / f"{name}.md"):
        r = memory_import.import_file("imported", src)
    # H1 + Long-term = 2 sections
    assert r.sections_imported == 2
    assert r.lines_imported == 2
    assert (tmp_path / "imported.md").read_text() == src.read_text()


# --------------------------------------------------------------------------- #
# Bridge handler tests — full HTTP round-trip via TestClient
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_create_agent_with_memory_atomic(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app(tmp_path, monkeypatch)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        body = {
            "name": "claude-code",
            "npub": "npub1claudecode",
            "color": "#a78bfa",
            "role": "member",
            "memory_md": (
                "# claude-code\n\n"
                "## Identity\n"
                "- Anthropic's coding agent\n"
                "- Markdown-first\n\n"
                "## Preferences\n"
                "- terse replies\n"
            ),
        }
        resp = await cli.post("/v1/ui/agents", json=body, headers=AUTH_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["agent"]["name"] == "claude-code"
        # H1 + Identity + Preferences = 3 sections
        assert data["memory"]["sections_imported"] == 3
        assert data["memory"]["lines_imported"] == 3
        # Memory file is on disk
        assert (tmp_path / "claude-code.md").exists()
        # Registry entry mirrors
        assert data["agent"]["color"] == "#a78bfa"
        assert data["agent"]["role"] == "member"
        assert data["agent"]["source_ecosystem"] == "local"
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_create_agent_no_memory(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app(tmp_path, monkeypatch)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        resp = await cli.post(
            "/v1/ui/agents",
            json={"name": "observer-1", "role": "observer"},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["memory"] is None
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_create_agent_duplicate(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app(tmp_path, monkeypatch)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        r1 = await cli.post(
            "/v1/ui/agents", json={"name": "dup"}, headers=AUTH_HEADERS
        )
        assert r1.status == 200
        r2 = await cli.post(
            "/v1/ui/agents", json={"name": "dup"}, headers=AUTH_HEADERS
        )
        assert r2.status == 409
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_create_agent_no_session(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app(tmp_path, monkeypatch)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        resp = await cli.post("/v1/ui/agents", json={"name": "x"})
        assert resp.status == 401
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_create_agent_invalid_role(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app(tmp_path, monkeypatch)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        resp = await cli.post(
            "/v1/ui/agents",
            json={"name": "bad-role", "role": "godmode"},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 400
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_preview_endpoint(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app(tmp_path, monkeypatch)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        body = {"memory_md": "## A\n- one\n- two\n\n## B\n- three\n"}
        resp = await cli.get("/v1/ui/memory/preview", json=body)
        assert resp.status == 200
        data = await resp.json()
        assert data["section_titles"] == ["A", "B"]
        assert data["total_line_count"] == 3
        assert data["warnings"] == []
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_preview_read_only(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app(tmp_path, monkeypatch)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        before = set(tmp_path.iterdir())
        body = {"memory_md": "## A\n- one\n"}
        resp = await cli.get("/v1/ui/memory/preview", json=body)
        assert resp.status == 200
        after = set(tmp_path.iterdir())
        assert before == after, "preview must not write to disk"
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_import_memory_inline_json(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app(tmp_path, monkeypatch)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        # Pre-existing memory
        (tmp_path / "claude-code.md").write_text("# claude-code\n\n## Old\n- a\n")
        body = {
            "agent": "claude-code",
            "memory_md": "# claude-code\n\n## New\n- b\n- c\n",
        }
        resp = await cli.post(
            "/v1/ui/agents/import-memory", json=body, headers=AUTH_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["agent"] == "claude-code"
        # H1 + New = 2 sections
        assert data["sections_imported"] == 2
        assert data["lines_imported"] == 2
        assert data["backup_path"] is not None
        live = (tmp_path / "claude-code.md").read_text()
        assert "New" in live
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_import_memory_upload_multipart(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app(tmp_path, monkeypatch)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        boundary = "----TestBoundaryABCDEF"
        file_body = (
            "# uploaded\n\n"
            "## Identity\n"
            "- from a file\n"
            "- round 2\n"
        )
        parts: list[bytes] = []
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(b'Content-Disposition: form-data; name="agent"\r\n\r\n')
        parts.append(b"uploaded\r\n")
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            b'Content-Disposition: form-data; name="file"; filename="memory.md"\r\n'
            b'Content-Type: text/markdown\r\n\r\n'
        )
        parts.append(file_body.encode())
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        payload = b"".join(parts)
        resp = await cli.post(
            "/v1/ui/agents/import-memory",
            data=payload,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                **AUTH_HEADERS,
            },
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["agent"] == "uploaded"
        # H1 + Identity = 2 sections
        assert data["sections_imported"] == 2
        assert data["lines_imported"] == 2
        assert (tmp_path / "uploaded.md").exists()
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_import_memory_no_session(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app(tmp_path, monkeypatch)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        resp = await cli.post(
            "/v1/ui/agents/import-memory",
            json={"agent": "x", "memory_md": "y"},
        )
        assert resp.status == 401
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_import_memory_oversize_inline(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    app = _make_app(tmp_path, monkeypatch)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        huge = "x" * (memory_import.MAX_INLINE_BYTES + 1)
        resp = await cli.post(
            "/v1/ui/agents/import-memory",
            json={"agent": "big", "memory_md": huge},
            headers=AUTH_HEADERS,
        )
        assert resp.status == 413
        data = await resp.json()
        assert data["cap"] == memory_import.MAX_INLINE_BYTES
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_import_memory_creates_backup(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from pathlib import Path
    app = _make_app(tmp_path, monkeypatch)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        (tmp_path / "doomed.md").write_text("# doomed\n\n## Old\n- ORIGINAL\n")
        body = {
            "agent": "doomed",
            "memory_md": "# doomed\n\n## New\n- REPLACEMENT\n",
        }
        resp = await cli.post(
            "/v1/ui/agents/import-memory", json=body, headers=AUTH_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        bak = Path(data["backup_path"])
        assert bak.exists()
        assert "ORIGINAL" in bak.read_text()
    finally:
        await cli.close()

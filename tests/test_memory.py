"""
Tests for agentchat.memory — per-agent + shared team + project tiers.

Stdlib only. Each test gets an isolated tempdir via AGENTCHAT_MEMORY_DIR
through a pytest fixture so tests don't bleed into each other or into the
real ~/.hermes/memory/.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentchat import memory


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def memroot(monkeypatch, tmp_path):
    """Per-test isolated memory root. Sets AGENTCHAT_MEMORY_DIR so every call
    to memory.memory_root() returns tmp_path. Also returns the path so tests
    can assert on filesystem state directly."""
    monkeypatch.setenv("AGENTCHAT_MEMORY_DIR", str(tmp_path))
    # The memory module caches TEAM_FOCUS_PATH / TEAM_SHARED_PATH at import
    # time. Those point to wherever AGENTCHAT_MEMORY_DIR was when memory.py
    # was first imported. For tests, recompute via the env var directly:
    # monkeypatch.setenv above updates os.environ, so memory_root() now
    # returns tmp_path on each call, and writes go to the right place.
    return tmp_path


def _cli(args: list[str], memroot: Path) -> subprocess.CompletedProcess:
    """Invoke agentchat.memory CLI in a subprocess with the test's memroot."""
    return subprocess.run(
        [sys.executable, "-c",
         "from agentchat.memory import main; import sys; sys.exit(main(sys.argv[1:]))",
         *args],
        env={**os.environ, "AGENTCHAT_MEMORY_DIR": str(memroot)},
        capture_output=True, text=True,
    )


# --------------------------------------------------------------------------- #
# Per-agent private memory
# --------------------------------------------------------------------------- #

class TestAgentMemory:
    def test_write_and_read(self, memroot):
        memory.write_agent("hermes", "# Hermes\n\n## Today\nSent PSP pack.\n")
        assert memory.read_agent("hermes") == "# Hermes\n\n## Today\nSent PSP pack.\n"

    def test_append_under_existing_section(self, memroot):
        memory.write_agent("hermes", "## Today\nFirst line.\n")
        memory.append_agent("hermes", "Today", "Second line.")
        body = memory.read_agent("hermes")
        assert "First line." in body
        assert "Second line." in body

    def test_append_creates_section(self, memroot):
        memory.write_agent("hermes", "## Existing\ncontent\n")
        memory.append_agent("hermes", "Brand New", "fresh content")
        body = memory.read_agent("hermes")
        assert "## Existing" in body
        assert "## Brand New" in body
        assert "fresh content" in body

    def test_invalid_agent_name_rejected(self, memroot):
        with pytest.raises(ValueError):
            memory.write_agent("../etc/passwd", "pwned")
        with pytest.raises(ValueError):
            memory.write_agent("name with spaces", "nope")

    def test_list_agents(self, memroot):
        memory.write_agent("hermes", "h")
        memory.write_agent("chappy", "c")
        assert set(memory.list_agents()) == {"hermes", "chappy"}

    def test_atomic_write_no_torn_file(self, memroot):
        memory.write_agent("hermes", "x" * 10000)
        tmp_files = list(memroot.glob("agents/*/*.tmp"))
        assert tmp_files == []

    def test_isolation_between_tests(self, memroot):
        """The fixture must give each test a clean slate, regardless of
        which other tests ran first."""
        agents_before = set(memory.list_agents())
        memory.write_agent("isolation-check", "fresh")
        agents_after = set(memory.list_agents())
        assert agents_before == set()
        assert agents_after == {"isolation-check"}


# --------------------------------------------------------------------------- #
# Team shared memory
# --------------------------------------------------------------------------- #

class TestTeamMemory:
    def test_team_round_trip(self, memroot):
        memory.write_team("# Team\n\n## Active Projects\n- RestTech\n")
        assert memory.read_team() == "# Team\n\n## Active Projects\n- RestTech\n"

    def test_team_append_attribution(self, memroot):
        memory.write_team("# Team\n\n## Notes\nold\n")
        memory.append_team("Notes", "Chappy is on morlife.ie", author="chappy")
        body = memory.read_team()
        assert "Chappy is on morlife.ie" in body
        assert "chappy" in body  # attribution


# --------------------------------------------------------------------------- #
# Agent focus (structured live state)
# --------------------------------------------------------------------------- #

class TestFocus:
    def test_set_and_read(self, memroot):
        memory.set_focus("chappy", focus="morlife.ie AdSense audit", status="active")
        state = memory.read_focus()
        assert "chappy" in state.agents
        assert state.agents["chappy"].focus == "morlife.ie AdSense audit"
        assert state.agents["chappy"].status == "active"

    def test_focus_persists_to_json(self, memroot):
        memory.set_focus("hermes", focus="RestTech PSP pack", notes="waiting for Chappy")
        # Read the JSON file directly so we exercise the on-disk format.
        raw_path = memroot / "team" / "focus.json"
        assert raw_path.exists(), f"focus.json not written at {raw_path}"
        raw = json.loads(raw_path.read_text())
        assert raw["agents"]["hermes"]["focus"] == "RestTech PSP pack"
        assert raw["agents"]["hermes"]["notes"] == "waiting for Chappy"

    def test_focus_update_keeps_other_agents(self, memroot):
        memory.set_focus("chappy", focus="morlife.ie")
        memory.set_focus("hermes", focus="RestTech")
        state = memory.read_focus()
        assert "chappy" in state.agents
        assert "hermes" in state.agents

    def test_priorities(self, memroot):
        memory.set_wayne_priorities(["Land RestTech PSP", "morlife.ie AdSense revenue"])
        state = memory.read_focus()
        assert len(state.wayne_priorities) == 2


# --------------------------------------------------------------------------- #
# Project memory
# --------------------------------------------------------------------------- #

class TestProjectMemory:
    def test_write_read(self, memroot):
        memory.write_project("resttech", "# RestTech\n\n## Status\nLive\n")
        assert "RestTech" in memory.read_project("resttech")

    def test_append(self, memroot):
        memory.write_project("resttech", "## Decisions\n- use Stripe\n")
        memory.append_project("resttech", "Decisions", "- consider Adyen")
        body = memory.read_project("resttech")
        assert "use Stripe" in body
        assert "consider Adyen" in body

    def test_invalid_slug_rejected(self, memroot):
        with pytest.raises(ValueError):
            memory.write_project("with spaces", "nope")


# --------------------------------------------------------------------------- #
# Snapshot / import / export
# --------------------------------------------------------------------------- #

class TestSnapshotImport:
    def test_snapshot_creates_archive(self, memroot):
        memory.write_agent("hermes", "# Hermes\n")
        memory.write_team("# Team\n")
        memory.write_project("resttech", "# RestTech\n")
        dest = memory.snapshot(label="test")
        assert dest.exists()
        assert (dest / "agents" / "hermes" / "MEMORY.md").exists()
        assert (dest / "team" / "SHARED.md").exists()
        assert (dest / "projects" / "resttech" / "NOTES.md").exists()

    def test_import_into_new_agent(self, memroot):
        # Source agent writes its memory.
        memory.write_agent("hermes", "# Hermes\n\n## Today\nPSP pack sent.\n")
        # Snapshot the whole store.
        snap = memory.snapshot(label="export-test")
        # Simulate a new agent joining: import into a different name.
        memory.import_memory(snap, target_agent="chappy-v2")
        body = memory.read_agent("chappy-v2")
        assert "PSP pack sent" in body

    def test_import_into_existing_agent_merges(self, memroot):
        memory.write_agent("chappy", "## Existing\nold content\n")
        memory.write_agent("hermes", "## Today\nnew stuff\n")
        snap = memory.snapshot(label="re")
        memory.import_memory(snap, target_agent="chappy", mode="merge")
        body = memory.read_agent("chappy")
        assert "old content" in body
        assert "## Today" in body

    def test_import_into_project(self, memroot):
        memory.write_project("morlife", "## Audit\nAdSense review\n")
        snap = memory.snapshot()
        memory.import_memory(snap, target_project="morlife-ie")
        body = memory.read_project("morlife-ie")
        assert "AdSense review" in body


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

class TestCli:
    def test_cli_focus_set_and_read(self, memroot):
        r = _cli(["focus", "chappy", "morlife.ie AdSense audit", "--status", "active"], memroot)
        assert r.returncode == 0, r.stderr
        r2 = _cli(["focus", "chappy"], memroot)
        assert "morlife.ie" in r2.stdout

    def test_cli_ls_agents(self, memroot):
        memory.write_agent("hermes", "h")
        memory.write_agent("chappy", "c")
        r = _cli(["ls", "agents"], memroot)
        assert "hermes" in r.stdout
        assert "chappy" in r.stdout

    # ----- `init` subcommand ------------------------------------------------

    def test_cli_init_merges_into_existing_agent(self, memroot):
        """init --agent NAME --import-from DIR merges prior MEMORY.md into
        an existing agent's store under the same headings."""
        # 1. Existing target agent with some prior knowledge.
        memory.write_agent("newagent", "# newagent — Agent Memory\n\n## Prefs\nloves coffee\n")
        # 2. Source export bundle from another agent.
        bundle = memroot / "incoming-bundle"
        (bundle / "agents" / "oldagent").mkdir(parents=True)
        (bundle / "agents" / "oldagent" / "MEMORY.md").write_text(
            "# oldagent — Agent Memory\n\n## Prefs\nprefers Python\n\n## Tools\nlikes vi\n",
            encoding="utf-8",
        )
        r = _cli(
            ["init", "--agent", "newagent", "--import-from", str(bundle),
             "--no-archive"],
            memroot,
        )
        assert r.returncode == 0, r.stderr + r.stdout
        summary = json.loads(r.stdout)
        assert summary["target_agent"] == "newagent"
        assert summary["mode"] == "merge"
        # merge → both prefs lines present
        body = memory.read_agent("newagent")
        assert "loves coffee" in body
        assert "prefers Python" in body
        assert "vi" in body

    def test_cli_init_create_if_missing(self, memroot):
        """--create-if-missing scaffolds an empty MEMORY.md then imports."""
        assert "newagent2" not in memory.list_agents()
        bundle = memroot / "incoming"
        (bundle / "agents" / "source1").mkdir(parents=True)
        (bundle / "agents" / "source1" / "MEMORY.md").write_text(
            "# source1 — Memory\n\n## Role\nresearcher\n",
            encoding="utf-8",
        )
        r = _cli(
            ["init", "--agent", "newagent2", "--import-from", str(bundle),
             "--create-if-missing", "--no-archive"],
            memroot,
        )
        assert r.returncode == 0, r.stderr + r.stdout
        assert "newagent2" in memory.list_agents()
        body = memory.read_agent("newagent2")
        assert "researcher" in body

    def test_cli_init_without_create_fails(self, memroot):
        """Without --create-if-missing and without an existing MEMORY.md,
        init must exit 2 and not create anything."""
        bundle = memroot / "incoming"
        (bundle / "agents" / "source2").mkdir(parents=True)
        (bundle / "agents" / "source2" / "MEMORY.md").write_text("x", encoding="utf-8")
        r = _cli(
            ["init", "--agent", "ghost", "--import-from", str(bundle),
             "--no-archive"],
            memroot,
        )
        assert r.returncode == 2
        assert "ghost" not in memory.list_agents()

    def test_cli_init_missing_source_fails(self, memroot):
        memory.write_agent("newagent3", "# newagent3\n")
        r = _cli(
            ["init", "--agent", "newagent3", "--import-from",
             str(memroot / "does-not-exist"), "--no-archive"],
            memroot,
        )
        assert r.returncode == 2
        assert "not found" in r.stderr.lower()

    def test_cli_init_invalid_agent_name_fails(self, memroot):
        bundle = memroot / "incoming"
        bundle.mkdir(exist_ok=True)
        r = _cli(
            ["init", "--agent", "bad name with spaces", "--import-from",
             str(bundle), "--no-archive"],
            memroot,
        )
        assert r.returncode == 2

    def test_cli_init_replace_mode_overwrites(self, memroot):
        """mode=replace should overwrite the target tier with the source."""
        memory.write_agent("newagent4", "# old\n\n## Prefs\nold pref\n")
        bundle = memroot / "incoming"
        (bundle / "agents" / "src").mkdir(parents=True)
        (bundle / "agents" / "src" / "MEMORY.md").write_text(
            "# fresh\n\n## Role\nonly this\n",
            encoding="utf-8",
        )
        r = _cli(
            ["init", "--agent", "newagent4", "--import-from", str(bundle),
             "--mode", "replace", "--no-archive"],
            memroot,
        )
        assert r.returncode == 0, r.stderr + r.stdout
        body = memory.read_agent("newagent4")
        assert "old pref" not in body
        assert "only this" in body

    def test_cli_init_default_creates_archive(self, memroot):
        """Without --no-archive, init should produce an archive snapshot first."""
        memory.write_agent("newagent5", "# newagent5\n\n## A\nfoo\n")
        bundle = memroot / "incoming"
        (bundle / "agents" / "src").mkdir(parents=True)
        (bundle / "agents" / "src" / "MEMORY.md").write_text(
            "# src\n\n## A\nbar\n",
            encoding="utf-8",
        )
        r = _cli(
            ["init", "--agent", "newagent5", "--import-from", str(bundle)],
            memroot,
        )
        assert r.returncode == 0, r.stderr + r.stdout
        summary = json.loads(r.stdout)
        assert "archive" in summary
        archive_path = Path(summary["archive"])
        assert archive_path.exists()
        # the archive should contain the pre-import state
        archived = (archive_path / "agents" / "newagent5" / "MEMORY.md").read_text()
        assert "foo" in archived


# --------------------------------------------------------------------------- #
# Memory Transparency helpers (used by the right-side Memories drawer)
# --------------------------------------------------------------------------- #

class TestMemoryTransparency:
    """Direct unit tests for the structured read/edit helpers added in
    dev13.  These power the GET/PUT/POST/DELETE bridge endpoints."""

    def test_list_agent_sections_parses_h1_preamble(self, memroot):
        memory.write_agent(
            "agentA",
            "# Agent A — display name\n\nintro text line\n\n## Prefs\nterse\nmulti\n\n## Tools\nvi\n",
        )
        sections = memory.list_agent_sections("agentA")
        # First section is the H1 preamble (title carries the H1's text,
        # lines carry any intro text before the first H2).
        assert sections[0]["title"] == "Agent A — display name"
        assert sections[0]["lines"] == ["intro text line"]
        titles = [s["title"] for s in sections[1:]]
        assert "Prefs" in titles
        assert "Tools" in titles
        prefs = next(s for s in sections if s["title"] == "Prefs")
        assert prefs["lines"] == ["terse", "multi"]
        tools = next(s for s in sections if s["title"] == "Tools")
        assert tools["lines"] == ["vi"]

    def test_list_agent_sections_empty(self, memroot):
        memory.write_agent("agentB", "")
        assert memory.list_agent_sections("agentB") == []

    def test_list_agent_sections_no_h2(self, memroot):
        memory.write_agent("agentC", "# just a title\n\nbody without sections\n")
        sections = memory.list_agent_sections("agentC")
        # Only the H1 preamble; no H2 sections.
        assert len(sections) == 1
        assert sections[0]["title"] == "just a title"

    def test_replace_agent_section_replaces_body(self, memroot):
        memory.write_agent("agentD", "# d\n\n## Prefs\nold1\nold2\n\n## Tools\nvi\n")
        memory.replace_agent_section("agentD", "Prefs", ["new1", "new2", "new3"])
        sections = memory.list_agent_sections("agentD")
        prefs = next(s for s in sections if s["title"] == "Prefs")
        assert prefs["lines"] == ["new1", "new2", "new3"]
        # other sections untouched
        tools = next(s for s in sections if s["title"] == "Tools")
        assert tools["lines"] == ["vi"]

    def test_replace_agent_section_creates_if_missing(self, memroot):
        memory.write_agent("agentE", "# e\n\n## Prefs\nexisting\n")
        memory.replace_agent_section("agentE", "NewSection", ["first"])
        sections = memory.list_agent_sections("agentE")
        new = next(s for s in sections if s["title"] == "NewSection")
        assert new["lines"] == ["first"]
        # existing preserved
        prefs = next(s for s in sections if s["title"] == "Prefs")
        assert prefs["lines"] == ["existing"]

    def test_replace_agent_section_empty_clears_body(self, memroot):
        memory.write_agent("agentF", "# f\n\n## Prefs\nold1\nold2\n")
        memory.replace_agent_section("agentF", "Prefs", [])
        sections = memory.list_agent_sections("agentF")
        prefs = next(s for s in sections if s["title"] == "Prefs")
        assert prefs["lines"] == []

    def test_replace_agent_section_is_case_insensitive(self, memroot):
        memory.write_agent("agentG", "# g\n\n## My Prefs\nx\n")
        memory.replace_agent_section("agentG", "my prefs", ["y", "z"])
        sections = memory.list_agent_sections("agentG")
        prefs = next(s for s in sections if s["title"] == "My Prefs")
        assert prefs["lines"] == ["y", "z"]

    def test_remove_agent_line_by_index(self, memroot):
        memory.write_agent("agentH", "# h\n\n## Tasks\nt1\nt2\nt3\n")
        assert memory.remove_agent_line("agentH", "Tasks", 1) is True
        sections = memory.list_agent_sections("agentH")
        tasks = next(s for s in sections if s["title"] == "Tasks")
        assert tasks["lines"] == ["t1", "t3"]

    def test_remove_agent_line_out_of_range_returns_false(self, memroot):
        memory.write_agent("agentI", "# i\n\n## Tasks\nonly\n")
        assert memory.remove_agent_line("agentI", "Tasks", 5) is False
        assert memory.remove_agent_line("agentI", "NotHere", 0) is False

    def test_atomic_write_no_torn_file_on_replace(self, memroot):
        """Replace must atomically replace the file (no torn state visible)."""
        memory.write_agent("agentJ", "# j\n\n## A\norig\n")
        # Simulate concurrent read while writing by reading before/after.
        before = memory.read_agent("agentJ")
        memory.replace_agent_section("agentJ", "A", ["new"])
        after = memory.read_agent("agentJ")
        # No torn state — either fully old or fully new, never partial.
        assert "orig" in before or "new" in before
        assert "new" in after
        assert "orig" not in after


# --------------------------------------------------------------------------- #
# Markdown helpers
# --------------------------------------------------------------------------- #

class TestMarkdownSectionSplit:
    def test_split_simple(self):
        body = "# Title\n\nintro\n\n## A\nbody A\n\n## B\nbody B\n"
        sections = memory._split_sections(body)
        assert len(sections) == 3
        assert sections[0][0] == "Title"
        assert sections[1][0] == "A"
        assert sections[2][0] == "B"

    def test_append_under_section_preserves_h1(self):
        body = "# Title\n\nintro\n\n## A\nbody A\n"
        out = memory._append_under_section(body, "A", "new line")
        assert out.startswith("# Title")

    def test_merge_memory_distinct_sections(self):
        existing = "## A\nexisting A\n"
        incoming = "## B\nincoming B\n"
        merged = memory._merge_memory(existing, incoming)
        assert "existing A" in merged
        assert "incoming B" in merged


# --------------------------------------------------------------------------- #
# Cross-isolation smoke: prove the fixture works even after another test
# file imports the memory module. This is what was failing before.
# --------------------------------------------------------------------------- #

def test_isolation_across_modules(monkeypatch, tmp_path):
    """Even if another test (e.g. test_agent_loops) imported the memory
    module first and the env var was different then, this test gets its
    own tmp_path and its own env via monkeypatch."""
    # Simulate a prior import having seen a different env var.
    monkeypatch.setenv("AGENTCHAT_MEMORY_DIR", str(tmp_path))
    # memory_root() must honor the current env var.
    assert memory.memory_root() == tmp_path
    # And writes go to the right place.
    memory.write_team("# fresh\n")
    assert (tmp_path / "team" / "SHARED.md").read_text() == "# fresh\n"
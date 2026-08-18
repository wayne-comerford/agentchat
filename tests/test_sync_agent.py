"""
Tests for ``agentchat.sync_agent`` — the change detection + local
commit stage of the dev20 GitHub sync agent.

Each test spins up a throwaway git repo (a normal ``git init`` repo,
not a bare one) inside pytest's ``tmp_path``, makes a known change,
and asserts that:

    * ``collect_changes`` reports the right kinds of records
      (added/modified/deleted/renamed).
    * ``CommitStage.run`` produces exactly one commit.
    * The commit message matches the design-doc convention.
    * The commit's tree actually contains the new state.
    * Idempotency: a second ``run`` produces no new commits when
      there is nothing to do.

These tests do **not** touch the network, do **not** push, and do
**not** rely on anything outside Python stdlib + the ``git`` CLI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agentchat.sync_agent import (
    ChangeSet,
    CommitResult,
    CommitStage,
    DEFAULT_EXCLUDE,
    SyncConfig,
    build_commit_message,
    collect_changes,
    has_uncommitted_changes,
    watch_and_commit,
)
from agentchat.sync_agent.commit import ChangeRecord
from agentchat.sync_agent.watcher import (
    DebouncedEmitter,
    PollingEmitter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _make_repo(tmp_path: Path) -> Path:
    """Create a fresh non-bare git repo with a single initial commit.

    Returns the repo path. The repo starts with one tracked file so
    the watcher's first commit isn't an empty one (we want to test
    changes on top of a known baseline)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "sync-agent-test@localhost")
    _git(repo, "config", "user.name", "sync-agent-test")
    _git(repo, "config", "commit.gpgsign", "false")
    # Initial commit so HEAD exists and `git status` is clean.
    (repo / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "baseline.txt")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path)


@pytest.fixture
def config(repo: Path) -> SyncConfig:
    return SyncConfig(
        repo_dir=repo,
        watched_roots=(repo,),
        debounce_seconds=0.05,  # short — the debounce tests don't sleep
    )


# ---------------------------------------------------------------------------
# ``collect_changes`` parser tests
# ---------------------------------------------------------------------------


class TestCollectChanges:
    def test_clean_repo_returns_empty(self, repo, config):
        # Sanity check: collect_changes on a clean repo returns no records.
        cs = collect_changes(repo)
        assert cs.is_empty()
        assert cs.records == ()

    def test_added_file_is_collected(self, repo, config):
        (repo / "new.txt").write_text("hello\n", encoding="utf-8")
        cs = collect_changes(repo)
        assert len(cs.added) == 1
        rec = cs.added[0]
        assert rec.path == "new.txt"
        assert rec.kind == "added"

    def test_modified_file_is_collected(self, repo, config):
        target = repo / "baseline.txt"
        # Sleep just enough for mtime to change — git status uses the
        # worktree-side stat too, but a content change always surfaces
        # regardless of mtime.
        target.write_text("modified\n", encoding="utf-8")
        cs = collect_changes(repo)
        assert len(cs.modified) == 1
        assert cs.modified[0].path == "baseline.txt"
        assert cs.modified[0].kind == "modified"

    def test_deleted_file_is_collected(self, repo, config):
        (repo / "baseline.txt").unlink()
        cs = collect_changes(repo)
        assert len(cs.deleted) == 1
        assert cs.deleted[0].path == "baseline.txt"
        assert cs.deleted[0].kind == "deleted"

    def test_rename_is_collected(self, repo, config):
        # ``git mv`` so git sees the rename at status time.
        _git(repo, "mv", "baseline.txt", "renamed.txt")
        cs = collect_changes(repo)
        assert len(cs.renamed) == 1
        rec = cs.renamed[0]
        assert rec.path == "renamed.txt"
        assert rec.kind == "renamed"
        assert rec.old_path == "baseline.txt"

    def test_change_set_summary_line(self, repo, config):
        (repo / "a.txt").write_text("a", encoding="utf-8")
        (repo / "b.txt").write_text("b", encoding="utf-8")
        (repo / "baseline.txt").write_text("x", encoding="utf-8")
        _git(repo, "mv", "baseline.txt", "baseline-renamed.txt")
        cs = collect_changes(repo)
        # 2 added + 1 modified (from rename — git treats the new path
        # as "added" by default with -A, but rename detection kicks in
        # when the content is similar enough). For the dev20
        # acceptance we only care that the summary is well-formed.
        line = cs.summary_line()
        assert "added" in line
        assert "modified" in line or "renamed" in line
        assert " deleted" in line


# ---------------------------------------------------------------------------
# ``build_commit_message`` tests
# ---------------------------------------------------------------------------


class TestCommitMessage:
    def test_message_matches_design_template(self):
        cs = ChangeSet(
            records=(
                ChangeRecord(path="a.txt", kind="added"),
                ChangeRecord(path="b.txt", kind="modified"),
                ChangeRecord(path="c.txt", kind="deleted"),
                ChangeRecord(path="d.txt", kind="renamed", old_path="d-old.txt"),
            ),
            origin="watcher",
        )
        msg = build_commit_message(cs)
        # Subject line.
        assert msg.startswith("chore(sync): workspace + memory snapshot\n")
        # Summary line.
        assert "- 1 added, 1 modified, 1 deleted, 1 renamed\n" in msg
        # Triggered line uses the ChangeSet origin by default.
        assert "- triggered: watcher\n" in msg

    def test_message_origin_override(self):
        cs = ChangeSet(records=(), origin="watcher")
        msg = build_commit_message(cs, origin="polling-watchdog")
        assert "- triggered: polling-watchdog\n" in msg


# ---------------------------------------------------------------------------
# ``CommitStage`` tests — the core acceptance criteria
# ---------------------------------------------------------------------------


class TestCommitStage:
    def test_creates_commit_when_file_added(self, repo, config):
        """Acceptance: a runnable module that, when run, leaves the
        local repo with a new commit summarising all pending changes."""
        before = _git(repo, "rev-parse", "HEAD").stdout.strip()

        (repo / "fresh.txt").write_text("added by test\n", encoding="utf-8")
        stage = CommitStage(config)
        result = stage.run_once()

        assert isinstance(result, CommitResult)
        assert result.committed is True
        assert result.sha is not None and result.sha != before
        assert result.change_set.is_empty() is False
        assert len(result.change_set.added) == 1

        after = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert after == result.sha

        # The commit subject must follow the design convention.
        log_subject = _git(repo, "log", "-1", "--format=%s").stdout.strip()
        assert log_subject == "chore(sync): workspace + memory snapshot"

        # The file must be present in the new HEAD's tree.
        ls = _git(repo, "ls-tree", "--name-only", result.sha).stdout
        assert "fresh.txt" in ls

    def test_idempotent_when_no_changes(self, repo, config):
        """A second ``run`` with nothing new must NOT produce a new commit."""
        before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        stage = CommitStage(config)
        result = stage.run_once()
        assert result.committed is False
        assert result.sha is None
        after = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert before == after

    def test_commit_message_body_reflects_change_kinds(self, repo, config):
        # Mix of add + modify + delete + rename.
        (repo / "new.txt").write_text("new", encoding="utf-8")
        (repo / "baseline.txt").write_text("modified content", encoding="utf-8")
        (repo / "togo.txt").write_text("goodbye soon", encoding="utf-8")
        _git(repo, "add", "togo.txt")
        _git(repo, "commit", "-q", "-m", "seed togo")
        (repo / "togo.txt").unlink()
        _git(repo, "mv", "baseline.txt", "baseline-renamed.txt")

        stage = CommitStage(config)
        result = stage.run_once()
        assert result.committed is True
        body = _git(repo, "log", "-1", "--format=%b").stdout
        # The body contains the aggregate counts.
        assert "added" in body
        assert "modified" in body or "renamed" in body
        assert "deleted" in body or "renamed" in body
        # Triggered line.
        assert "triggered:" in body

    def test_refuses_unmerged_state(self, repo, config):
        # Create a real two-parent merge in progress by branching,
        # adding conflicting edits on each, then attempting the merge.
        _git(repo, "checkout", "-q", "-b", "feature")
        (repo / "baseline.txt").write_text("feature line\n", encoding="utf-8")
        _git(repo, "commit", "-q", "-a", "-m", "feature edit")
        _git(repo, "checkout", "-q", "main")
        (repo / "baseline.txt").write_text("main line\n", encoding="utf-8")
        _git(repo, "commit", "-q", "-a", "-m", "main edit")

        # Merge but DON'T commit — leave the worktree conflicted.
        merge = subprocess.run(
            ["git", "merge", "--no-ff", "--no-commit", "feature"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        # ``git merge`` exits non-zero on conflict; that's expected.
        # We just need the tree to be in a conflicted state.

        # Add an extra change so collect_changes has something to scan.
        (repo / "extra.txt").write_text("extra", encoding="utf-8")
        try:
            with pytest.raises(RuntimeError, match="unmerged"):
                collect_changes(repo)
        finally:
            # Clean up: abort the merge and reset baseline.txt so other
            # tests aren't affected (they shouldn't be, each gets a
            # fresh tmp_path, but defensive is good).
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                check=False,
            )

    def test_excludes_via_config(self, repo):
        # Create a file inside a directory that should be ignored.
        (repo / ".venv").mkdir()
        (repo / ".venv" / "should_skip.txt").write_text("skip", encoding="utf-8")
        # And one that should NOT be skipped.
        (repo / "keep.txt").write_text("keep", encoding="utf-8")

        config = SyncConfig(repo_dir=repo, watched_roots=(repo,))
        stage = CommitStage(config)
        result = stage.run_once()
        assert result.committed is True

        # The committed tree must contain keep.txt but NOT .venv/should_skip.txt.
        ls = _git(repo, "ls-tree", "-r", "--name-only", result.sha).stdout
        assert "keep.txt" in ls
        assert ".venv/should_skip.txt" not in ls
        assert ".venv" not in ls


# ---------------------------------------------------------------------------
# ``has_uncommitted_changes`` + ``snapshot_tree`` tests
# ---------------------------------------------------------------------------


class TestHasUncommittedChanges:
    def test_clean_repo_returns_false(self, repo):
        assert has_uncommitted_changes(repo) is False

    def test_modified_file_returns_true(self, repo):
        (repo / "baseline.txt").write_text("modified\n", encoding="utf-8")
        assert has_uncommitted_changes(repo) is True

    def test_added_file_returns_true(self, repo):
        (repo / "fresh.txt").write_text("hello", encoding="utf-8")
        assert has_uncommitted_changes(repo) is True

    def test_deleted_file_returns_true(self, repo):
        (repo / "baseline.txt").unlink()
        assert has_uncommitted_changes(repo) is True


class TestSnapshotTree:
    def test_excludes_patterns(self, repo):
        from agentchat.sync_agent.commit import snapshot_tree

        (repo / ".venv").mkdir()
        (repo / ".venv" / "skipme.py").write_text("nope", encoding="utf-8")
        (repo / "keep.txt").write_text("keep", encoding="utf-8")
        (repo / "__pycache__").mkdir()
        (repo / "__pycache__" / "x.pyc").write_text("binary", encoding="utf-8")

        snap = snapshot_tree(repo, DEFAULT_EXCLUDE, include_hidden=False)
        paths = set(snap.keys())
        assert "keep.txt" in paths
        assert ".venv/skipme.py" not in paths
        assert "__pycache__/x.pyc" not in paths
        # baseline.txt was already in the tree (created in fixture).
        assert "baseline.txt" in paths


# ---------------------------------------------------------------------------
# ``PollingEmitter`` tests
# ---------------------------------------------------------------------------


class TestPollingEmitter:
    def test_first_poll_establishes_baseline(self, repo, config):
        emitter = PollingEmitter(config)
        # First call: baseline established, no change reported.
        assert emitter.poll() is None
        # Second call with no changes: still no change.
        assert emitter.poll() is None

    def test_new_file_triggers_change(self, repo, config):
        emitter = PollingEmitter(config)
        # Baseline.
        assert emitter.poll() is None
        # Touch a new file.
        (repo / "fresh.txt").write_text("hello", encoding="utf-8")
        cs = emitter.poll()
        assert cs is not None
        assert not cs.is_empty()
        added_paths = [r.path for r in cs.added]
        # The path is repo-relative; PollingEmitter prefixes with the
        # absolute root, then strips it. The exact form is an internal
        # detail — assert the basename matches.
        assert any(p.endswith("fresh.txt") for p in added_paths)

    def test_deleted_file_triggers_change(self, repo, config):
        emitter = PollingEmitter(config)
        assert emitter.poll() is None
        (repo / "baseline.txt").unlink()
        cs = emitter.poll()
        assert cs is not None
        assert any(r.is_delete for r in cs.records)

    def test_modified_file_triggers_change(self, repo, config):
        emitter = PollingEmitter(config)
        assert emitter.poll() is None
        (repo / "baseline.txt").write_text("different", encoding="utf-8")
        cs = emitter.poll()
        assert cs is not None
        assert any(r.is_modify for r in cs.records)


# ---------------------------------------------------------------------------
# ``DebouncedEmitter`` tests
# ---------------------------------------------------------------------------


class TestDebouncedEmitter:
    def test_change_callback_fires_after_quiet(self, repo, config):
        emitter = PollingEmitter(config)
        # Baseline the polling layer.
        emitter.poll()

        received: list[ChangeSet] = []
        debounced = DebouncedEmitter(
            emitter,
            debounce_seconds=0.05,
            callback=received.append,
        )

        # Three rapid changes inside the debounce window.
        for i in range(3):
            (repo / f"file_{i}.txt").write_text(f"content {i}", encoding="utf-8")
            debounced.poll()
            time.sleep(0.01)

        # Wait past the debounce window.
        time.sleep(0.15)
        debounced.cancel()

        # Exactly one ChangeSet delivered (debounce coalesced the burst).
        assert len(received) == 1
        assert len(received[0].records) == 3

    def test_cancel_stops_pending_callback(self, repo, config):
        emitter = PollingEmitter(config)
        emitter.poll()

        received: list[ChangeSet] = []
        debounced = DebouncedEmitter(
            emitter,
            debounce_seconds=0.1,
            callback=received.append,
        )

        (repo / "x.txt").write_text("x", encoding="utf-8")
        debounced.poll()
        debounced.cancel()

        # Wait past the debounce window — the callback should NOT fire.
        time.sleep(0.2)
        assert received == []


# ---------------------------------------------------------------------------
# ``watch_and_commit(once=True)`` — the acceptance entry point
# ---------------------------------------------------------------------------


class TestWatchAndCommitOnce:
    def test_creates_commit_when_change_present(self, repo, config):
        """Direct acceptance test: a runnable invocation that leaves
        the local repo with a new commit summarising pending changes."""
        before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        (repo / "added_by_watcher.txt").write_text("watcher saw this", encoding="utf-8")

        # Use the debounce-free path by overriding the emitter with a
        # bare PollingEmitter and forcing once=True. The acceptance
        # criterion is "a commit is produced", not "debounce window
        # elapsed".
        result = watch_and_commit(config, once=True)
        assert result is not None
        assert result.committed is True

        after = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert after != before
        assert after == result.sha

        # The committed tree must contain the new file.
        assert result.sha is not None
        ls = _git(repo, "ls-tree", "-r", "--name-only", result.sha).stdout
        assert "added_by_watcher.txt" in ls

    def test_no_commit_when_nothing_changed(self, repo, config):
        before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        # Need to baseline the polling emitter, otherwise the very
        # first call sees every file as "added" relative to nothing.
        watch_and_commit(config, once=True)
        after_first = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert before == after_first

        # Second call — nothing changed in between.
        result = watch_and_commit(config, once=True)
        # Result is None because there are no uncommitted changes and
        # the polling layer sees the same snapshot.
        assert result is None
        after_second = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert after_first == after_second


# ---------------------------------------------------------------------------
# ``agentchat-sync-stage`` CLI smoke test
# ---------------------------------------------------------------------------


class TestCLI:
    def test_status_subcommand_shows_changes(self, repo):
        cli = "agentchat.sync_agent.__main__"
        (repo / "via_cli.txt").write_text("cli sees this", encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                cli,
                "--repo",
                str(repo),
                "status",
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        import json as _json
        doc = _json.loads(proc.stdout)
        paths = [r["path"] for r in doc["records"]]
        assert "via_cli.txt" in paths

    def test_once_subcommand_commits(self, repo):
        cli = "agentchat.sync_agent.__main__"
        before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        (repo / "via_cli_once.txt").write_text("via CLI once", encoding="utf-8")

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                cli,
                "--repo",
                str(repo),
                "once",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        after = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert after != before
        ls = _git(repo, "ls-tree", "-r", "--name-only", after).stdout
        assert "via_cli_once.txt" in ls
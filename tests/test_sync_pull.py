"""Tests for agentchat.sync_agent.pull (v1.2.0.dev27)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from agentchat.sync_agent import pull as pull_mod
from agentchat.sync_agent.pull import (
    DEFAULT_CONFLICT_DIR,
    DivergedError,
    GitClient,
    GitError,
    LocalDirtyError,
    NoRemoteError,
    PullConfig,
    PullResult,
    PullStage,
    detect_ahead_behind,
    pull_remote,
)


class StubGitClient(GitClient):
    """Programmable git stub. Each test sets `responses` to a list of (rc, stdout, stderr)
    tuples, one per `run()` call. `current` tracks the index."""

    def __init__(self, responses: list[tuple[int, str, str]] | None = None):
        self.responses = responses or []
        self.current = 0
        self.calls: list[tuple[str, ...]] = []

    def run(self, args, *, cwd, timeout=30):
        self.calls.append(tuple(args))
        if self.current >= len(self.responses):
            return 0, "", ""  # default: success, empty
        r = self.responses[self.current]
        self.current += 1
        return r


class TestDetectAheadBehind(unittest.TestCase):
    def test_parses_rev_list_output(self):
        stub = StubGitClient([
            (0, "abc123\n", ""),                              # local HEAD
            (0, "def456\n", ""),                              # remote SHA
            (0, "2\t3\n", ""),                                # ahead 2, behind 3
        ])
        local, remote, ahead, behind = detect_ahead_behind(
            Path("/x"), "origin", "main", git=stub,
        )
        self.assertEqual(local, "abc123")
        self.assertEqual(remote, "def456")
        self.assertEqual(ahead, 2)
        self.assertEqual(behind, 3)

    def test_zero_behind(self):
        stub = StubGitClient([
            (0, "abc\n", ""),
            (0, "abc\n", ""),
            (0, "0\t0\n", ""),
        ])
        _, _, ahead, behind = detect_ahead_behind(
            Path("/x"), "origin", "main", git=stub,
        )
        self.assertEqual((ahead, behind), (0, 0))

    def test_handles_missing_rev_list(self):
        stub = StubGitClient([
            (0, "abc\n", ""),
            (0, "def\n", ""),
            (1, "", "fatal: bad revision"),  # rev-list fails
        ])
        _, _, ahead, behind = detect_ahead_behind(
            Path("/x"), "origin", "main", git=stub,
        )
        self.assertEqual((ahead, behind), (0, 0))  # defaults to 0

    def test_handles_garbled_rev_list(self):
        stub = StubGitClient([
            (0, "abc\n", ""),
            (0, "def\n", ""),
            (0, "garbage\n", ""),
        ])
        _, _, ahead, behind = detect_ahead_behind(
            Path("/x"), "origin", "main", git=stub,
        )
        self.assertEqual((ahead, behind), (0, 0))


class TestPullStageNoRemote(unittest.TestCase):
    def test_no_remote_returns_no_remote_status(self):
        stub = StubGitClient([
            (1, "", "fatal: No such remote 'origin'"),  # remote get-url fails
        ])
        cfg = PullConfig(repo_dir=Path("/x"), git=stub)
        result = PullStage(cfg).pull()
        self.assertEqual(result.status, "no_remote")
        # "no_remote" is a valid state (user just hasn't configured the
        # remote yet), so we treat it as not-an-error. The caller can
        # inspect the status and act on it.
        self.assertTrue(result.ok)

    def test_empty_remote_url(self):
        stub = StubGitClient([
            (0, "\n", ""),  # remote get-url returns empty
        ])
        cfg = PullConfig(repo_dir=Path("/x"), git=stub)
        result = PullStage(cfg).pull()
        self.assertEqual(result.status, "no_remote")


class TestPullStageFetch(unittest.TestCase):
    def test_fetch_failure_returns_error(self):
        stub = StubGitClient([
            (0, "git@github.com:foo/bar\n", ""),    # remote get-url ok
            (1, "", "fatal: could not fetch"),     # fetch fails
        ])
        cfg = PullConfig(repo_dir=Path("/x"), git=stub)
        result = PullStage(cfg).pull()
        self.assertEqual(result.status, "error")
        self.assertIn("fetch failed", result.error)
        self.assertFalse(result.ok)

    def test_up_to_date_short_circuits(self):
        stub = StubGitClient([
            (0, "git@github.com:foo/bar\n", ""),  # remote
            (0, "", ""),                          # fetch
            (0, "abc123\n", ""),                  # local HEAD
            (0, "abc123\n", ""),                  # remote SHA
            (0, "0\t0\n", ""),                    # ahead/behind
        ])
        cfg = PullConfig(repo_dir=Path("/x"), git=stub)
        result = PullStage(cfg).pull()
        self.assertEqual(result.status, "up_to_date")
        self.assertTrue(result.ok)
        self.assertEqual(result.ahead, 0)
        self.assertEqual(result.behind, 0)
        # No rebase call should have been made
        cmds = [c[0] for c in stub.calls if c]
        self.assertNotIn("rebase", cmds)

    def test_local_ahead_no_action(self):
        stub = StubGitClient([
            (0, "git@github.com:foo/bar\n", ""),  # remote
            (0, "", ""),                          # fetch
            (0, "local_sha\n", ""),               # local
            (0, "remote_sha\n", ""),              # remote
            (0, "2\t0\n", ""),                    # ahead=2, behind=0
        ])
        cfg = PullConfig(repo_dir=Path("/x"), git=stub)
        result = PullStage(cfg).pull()
        self.assertEqual(result.status, "up_to_date")
        self.assertEqual(result.ahead, 2)


class TestPullStageFastForward(unittest.TestCase):
    def test_clean_fast_forward(self):
        stub = StubGitClient([
            (0, "git@github.com:foo/bar\n", ""),  # remote
            (0, "", ""),                          # fetch
            (0, "old_sha\n", ""),                 # local
            (0, "new_sha\n", ""),                 # remote
            (0, "0\t3\n", ""),                    # ahead=0, behind=3
            (0, "", ""),                          # status --porcelain (clean)
            (0, "", ""),                          # merge --ff-only
            (0, "new_sha\n", ""),                 # rev-parse HEAD
        ])
        cfg = PullConfig(repo_dir=Path("/x"), git=stub)
        result = PullStage(cfg).pull()
        self.assertEqual(result.status, "fast_forwarded")
        self.assertTrue(result.ok)
        self.assertEqual(result.pulled_sha, "new_sha")
        # No rebase call should have been made
        self.assertNotIn("rebase", [c[0] for c in stub.calls])

    def test_dirty_local_refuses_and_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            conflict_root = Path(tmp) / "conflicts"
            stub = StubGitClient([
                (0, "git@github.com:foo/bar\n", ""),  # remote
                (0, "", ""),                          # fetch
                (0, "old_sha\n", ""),                 # local
                (0, "new_sha\n", ""),                 # remote
                (0, "0\t3\n", ""),                    # ahead=0, behind=3
                (0, " M foo.txt\n", ""),              # status --porcelain (dirty)
                (0, "@@ diff @@\n+incoming\n", ""),   # diff incoming
            ])
            cfg = PullConfig(
                repo_dir=Path("/x"), git=stub, conflict_dir=conflict_root,
            )
            result = PullStage(cfg).pull()
            self.assertEqual(result.status, "local_dirty")
            self.assertFalse(result.ok)
            self.assertIsNotNone(result.conflict_dir)
            assert result.conflict_dir is not None
            self.assertTrue(result.conflict_dir.exists())
            # Verify the snapshot contents
            files = sorted(p.name for p in result.conflict_dir.iterdir())
            self.assertIn("status.txt", files)
            self.assertIn("incoming.diff", files)
            self.assertIn("pull_result.json", files)
            self.assertIn("conflict_report.md", files)
            # Verify no merge was attempted
            merge_calls = [c for c in stub.calls if c and c[0] == "merge"]
            self.assertEqual(merge_calls, [])

    def test_dry_run_does_not_mutate(self):
        stub = StubGitClient([
            (0, "git@github.com:foo/bar\n", ""),
            (0, "", ""),                          # fetch
            (0, "old_sha\n", ""),
            (0, "new_sha\n", ""),
            (0, "0\t3\n", ""),
            (0, "", ""),                          # status clean
        ])
        cfg = PullConfig(repo_dir=Path("/x"), git=stub, dry_run=True)
        result = PullStage(cfg).pull()
        self.assertEqual(result.status, "fast_forwarded")
        # No merge should have been called in dry-run
        merge_calls = [c for c in stub.calls if c and c[0] == "merge"]
        self.assertEqual(merge_calls, [])

    def test_merge_failure_returns_error(self):
        stub = StubGitClient([
            (0, "git@github.com:foo/bar\n", ""),
            (0, "", ""),
            (0, "old_sha\n", ""),
            (0, "new_sha\n", ""),
            (0, "0\t3\n", ""),
            (0, "", ""),                              # clean
            (1, "", "fatal: not possible to fast-forward"),
        ])
        cfg = PullConfig(repo_dir=Path("/x"), git=stub)
        result = PullStage(cfg).pull()
        self.assertEqual(result.status, "error")
        self.assertIn("fast-forward failed", result.error)


class TestPullStageDiverged(unittest.TestCase):
    def test_diverged_without_rebase_raises(self):
        stub = StubGitClient([
            (0, "git@github.com:foo/bar\n", ""),
            (0, "", ""),
            (0, "local_sha\n", ""),
            (0, "remote_sha\n", ""),
            (0, "2\t3\n", ""),   # both ahead and behind
        ])
        cfg = PullConfig(repo_dir=Path("/x"), git=stub)
        with self.assertRaises(DivergedError):
            PullStage(cfg).pull()

    def test_diverged_dry_run_returns_diverged(self):
        stub = StubGitClient([
            (0, "git@github.com:foo/bar\n", ""),
            (0, "", ""),
            (0, "local_sha\n", ""),
            (0, "remote_sha\n", ""),
            (0, "2\t3\n", ""),
        ])
        cfg = PullConfig(repo_dir=Path("/x"), git=stub, dry_run=True)
        result = PullStage(cfg).pull()
        self.assertEqual(result.status, "diverged")
        self.assertFalse(result.ok)

    def test_diverged_with_rebase(self):
        stub = StubGitClient([
            (0, "git@github.com:foo/bar\n", ""),
            (0, "", ""),
            (0, "local_sha\n", ""),
            (0, "remote_sha\n", ""),
            (0, "2\t3\n", ""),
            (0, "", ""),                          # rebase ok
            (0, "rebased_sha\n", ""),             # new HEAD
        ])
        cfg = PullConfig(repo_dir=Path("/x"), git=stub, allow_rebase=True)
        result = PullStage(cfg).pull()
        self.assertEqual(result.status, "fast_forwarded")
        self.assertEqual(result.pulled_sha, "rebased_sha")

    def test_rebase_failure_aborts(self):
        stub = StubGitClient([
            (0, "git@github.com:foo/bar\n", ""),
            (0, "", ""),
            (0, "local_sha\n", ""),
            (0, "remote_sha\n", ""),
            (0, "2\t3\n", ""),
            (1, "", "CONFLICT (content): Merge conflict in foo.txt"),  # rebase fails
            (0, "", ""),                                                # rebase --abort
        ])
        cfg = PullConfig(repo_dir=Path("/x"), git=stub, allow_rebase=True)
        with self.assertRaises(GitError):
            PullStage(cfg).pull()
        # Verify abort was called
        abort_calls = [c for c in stub.calls if c and c[0] == "rebase" and len(c) > 1 and c[1] == "--abort"]
        self.assertEqual(len(abort_calls), 1)


class TestPullResult(unittest.TestCase):
    def test_to_dict_serializes_paths(self):
        r = PullResult(status="local_dirty", conflict_dir=Path("/tmp/conflict"))
        d = r.to_dict()
        self.assertEqual(d["conflict_dir"], "/tmp/conflict")
        self.assertEqual(d["status"], "local_dirty")

    def test_ok_statuses(self):
        self.assertTrue(PullResult(status="up_to_date").ok)
        self.assertTrue(PullResult(status="fast_forwarded").ok)
        self.assertTrue(PullResult(status="no_remote").ok)
        self.assertFalse(PullResult(status="diverged").ok)
        self.assertFalse(PullResult(status="local_dirty").ok)
        self.assertFalse(PullResult(status="error").ok)


class TestPullRemoteConvenience(unittest.TestCase):
    def test_pull_remote_delegates_to_stage(self):
        stub = StubGitClient([
            (0, "git@github.com:foo/bar\n", ""),
            (0, "", ""),
            (0, "abc\n", ""),
            (0, "abc\n", ""),
            (0, "0\t0\n", ""),
        ])
        result = pull_remote(
            Path("/x"), remote="origin", branch="main",
        )
        # Note: pull_remote creates its own SubprocessGitClient, so the stub
        # is not used. Just verify the function signature works.
        self.assertIsNotNone(result)


class TestSubprocessGitClient(unittest.TestCase):
    def test_run_returns_tuple(self):
        client = pull_mod.SubprocessGitClient()
        rc, out, err = client.run(["--version"], cwd=Path("/tmp"))
        self.assertEqual(rc, 0)
        self.assertIn("git version", out)

    def test_run_handles_missing_git(self):
        from unittest import mock
        client = pull_mod.SubprocessGitClient()
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("no")):
            rc, out, err = client.run(["anything"], cwd=Path("/tmp"))
            self.assertEqual(rc, 127)


class TestErrorClasses(unittest.TestCase):
    def test_no_remote_error_includes_remote(self):
        e = NoRemoteError("missing", remote="origin", branch="main")
        self.assertEqual(e.remote, "origin")
        self.assertEqual(e.branch, "main")

    def test_diverged_error(self):
        e = DivergedError("diverged", remote="origin", branch="main")
        self.assertIsInstance(e, pull_mod.PullError)

    def test_local_dirty_error(self):
        e = LocalDirtyError("dirty", remote="origin", branch="main")
        self.assertIsInstance(e, pull_mod.PullError)

    def test_git_error_carries_stderr(self):
        e = GitError("fail", remote="origin", branch="main", stderr="oops")
        self.assertEqual(e.stderr, "oops")


class TestSnapshotConflicts(unittest.TestCase):
    def test_conflict_dir_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            conflict_root = Path(tmp) / "conflicts"
            stub = StubGitClient([
                (0, "@@ diff @@\n+line\n", ""),  # diff incoming
            ])
            cfg = PullConfig(repo_dir=Path("/x"), git=stub, conflict_dir=conflict_root)
            stage = PullStage(cfg)
            result = PullResult(
                status="local_dirty",
                local_sha="abc",
                remote_sha="def",
                ahead=0,
                behind=1,
            )
            snapshot = stage._snapshot_conflicts(result, " M file.txt\n")
            self.assertTrue(snapshot.exists())
            self.assertEqual(snapshot.parent, conflict_root)
            # Read the report
            report = (snapshot / "conflict_report.md").read_text()
            self.assertIn("# Pull conflict report", report)
            self.assertIn("Local HEAD: abc", report)
            self.assertIn("Remote HEAD: def", report)


class TestDefaultConfigPath(unittest.TestCase):
    def test_default_conflict_dir_under_hermes_home(self):
        # Just check the path is sensible
        self.assertIn(".hermes", str(DEFAULT_CONFLICT_DIR))
        self.assertTrue(str(DEFAULT_CONFLICT_DIR).endswith("pull_conflicts"))


if __name__ == "__main__":
    unittest.main()

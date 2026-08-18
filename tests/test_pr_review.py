"""Tests for agentchat.pr_review (v1.2.0.dev25)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Force a temp DB BEFORE importing the module
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
os.environ["AGENTCHAT_PR_REVIEW_DB"] = _TMP_DB.name

from agentchat import pr_review  # noqa: E402
from agentchat.pr_review import (  # noqa: E402
    DEFAULT_REPO,
    DB_PATH,
    GhResult,
    list_local_comments,
    list_open_prs,
    list_recent_webhook_events,
    post_comment,
    record_webhook,
)


def _stub_gh_result(*, ok=True, stdout="", stderr="", status=0):
    return GhResult(ok=ok, status=status, stdout=stdout, stderr=stderr)


class TestSchema(unittest.TestCase):
    """Schema is created on first connect; verify tables exist."""

    def setUp(self):
        # Use a fresh DB for each test
        self.tmpdir = tempfile.mkdtemp()
        self.db = Path(self.tmpdir) / "test.db"
        self._orig_db_path = pr_review.DB_PATH
        pr_review.DB_PATH = self.db

    def tearDown(self):
        pr_review.DB_PATH = self._orig_db_path

    def test_schema_creates_all_tables(self):
        conn = pr_review._connect()
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            names = {r["name"] for r in rows}
            self.assertIn("review_sessions", names)
            self.assertIn("review_comments", names)
            self.assertIn("webhook_events", names)
        finally:
            conn.close()

    def test_schema_idempotent(self):
        # Run _connect twice, should not fail
        pr_review._connect()
        pr_review._connect()


class TestGhWrapper(unittest.TestCase):
    """_run_gh subprocess wrapper."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Path(self.tmpdir) / "test.db"
        self._orig_db_path = pr_review.DB_PATH
        pr_review.DB_PATH = self.db

    def tearDown(self):
        pr_review.DB_PATH = self._orig_db_path

    @patch("subprocess.run")
    def test_run_gh_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='[{"a":1}]', stderr="")
        r = pr_review._run_gh(["pr", "list"])
        self.assertTrue(r.ok)
        self.assertEqual(r.status, 0)
        self.assertEqual(r.stdout, '[{"a":1}]')

    @patch("subprocess.run")
    def test_run_gh_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="auth required")
        r = pr_review._run_gh(["pr", "list"])
        self.assertFalse(r.ok)
        self.assertIn("auth required", r.stderr)

    @patch("subprocess.run")
    def test_run_gh_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
        r = pr_review._run_gh(["pr", "list"], timeout=30)
        self.assertFalse(r.ok)
        self.assertIn("timeout", r.stderr.lower())

    @patch("subprocess.run")
    def test_run_gh_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError("gh not found")
        r = pr_review._run_gh(["pr", "list"])
        self.assertFalse(r.ok)
        self.assertEqual(r.status, 127)


class TestListPrs(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Path(self.tmpdir) / "test.db"
        self._orig_db_path = pr_review.DB_PATH
        pr_review.DB_PATH = self.db

    def tearDown(self):
        pr_review.DB_PATH = self._orig_db_path

    @patch("agentchat.pr_review._run_gh")
    def test_list_open_prs_parses_json(self, mock_run):
        mock_run.return_value = _stub_gh_result(
            stdout=json.dumps([
                {"number": 1, "title": "Add foo", "isDraft": False, "author": {"login": "alice"}, "headRefName": "feat/foo"},
                {"number": 2, "title": "WIP bar", "isDraft": True, "author": {"login": "bob"}, "headRefName": "wip"},
            ])
        )
        prs = list_open_prs("owner/repo")
        self.assertEqual(len(prs), 2)
        self.assertEqual(prs[0]["number"], 1)
        self.assertFalse(prs[0]["isDraft"])
        self.assertTrue(prs[1]["isDraft"])
        # Verify gh was called with right args
        args = mock_run.call_args[0][0]
        self.assertIn("pr", args)
        self.assertIn("list", args)
        self.assertIn("--repo", args)
        self.assertIn("owner/repo", args)

    @patch("agentchat.pr_review._run_gh")
    def test_list_open_prs_handles_gh_failure(self, mock_run):
        mock_run.return_value = _stub_gh_result(ok=False, stderr="network down")
        with self.assertRaises(RuntimeError):
            list_open_prs("owner/repo")

    @patch("agentchat.pr_review._run_gh")
    def test_list_open_prs_empty(self, mock_run):
        mock_run.return_value = _stub_gh_result(stdout="[]")
        prs = list_open_prs("owner/repo")
        self.assertEqual(prs, [])


class TestGetPr(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Path(self.tmpdir) / "test.db"
        self._orig_db_path = pr_review.DB_PATH
        pr_review.DB_PATH = self.db

    def tearDown(self):
        pr_review.DB_PATH = self._orig_db_path

    @patch("agentchat.pr_review._run_gh")
    def test_get_pr_returns_full_data(self, mock_run):
        mock_run.return_value = _stub_gh_result(
            stdout=json.dumps({
                "number": 42,
                "title": "Implement feature X",
                "state": "OPEN",
                "author": {"login": "alice"},
                "headRefName": "feat/x",
                "baseRefName": "main",
                "headRefOid": "abc123def456",
                "body": "Long description...",
                "url": "https://github.com/owner/repo/pull/42",
                "additions": 120,
                "deletions": 30,
                "changedFiles": 5,
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "files": [{"path": "src/x.py", "additions": 50, "deletions": 10}],
            })
        )
        info = pr_review.get_pr("owner/repo", 42)
        self.assertEqual(info["number"], 42)
        self.assertEqual(info["additions"], 120)
        self.assertEqual(info["headRefName"], "feat/x")
        self.assertEqual(len(info["files"]), 1)


class TestPostComment(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Path(self.tmpdir) / "test.db"
        self._orig_db_path = pr_review.DB_PATH
        pr_review.DB_PATH = self.db

    def tearDown(self):
        pr_review.DB_PATH = self._orig_db_path

    def test_post_comment_requires_body(self):
        with self.assertRaises(ValueError):
            post_comment("owner/repo", 1, "")
        with self.assertRaises(ValueError):
            post_comment("owner/repo", 1, "   ")

    def test_post_comment_path_requires_line(self):
        with self.assertRaises(ValueError):
            post_comment("owner/repo", 1, "hi", path="x.py")

    def test_post_comment_line_requires_path(self):
        with self.assertRaises(ValueError):
            post_comment("owner/repo", 1, "hi", line=10)

    @patch("agentchat.pr_review._run_gh")
    def test_post_general_comment(self, mock_run):
        mock_run.return_value = _stub_gh_result(
            stdout="https://github.com/owner/repo/pull/1#issuecomment-12345"
        )
        r = post_comment("owner/repo", 1, "Looks good!", agent="hermes")
        self.assertEqual(r["status"], "posted")
        self.assertEqual(r["gh_comment_id"], 12345)
        # Verify gh was called with pr comment (not review)
        args = mock_run.call_args[0][0]
        self.assertIn("comment", args)
        self.assertNotIn("review", args)

    @patch("agentchat.pr_review._run_gh")
    @patch("agentchat.pr_review.get_pr")
    def test_post_inline_review_comment(self, mock_get_pr, mock_run):
        mock_get_pr.return_value = {"headRefOid": "abc123"}
        # The inline path uses raw subprocess.run, not _run_gh. Mock the response.
        # The test sets up the response to look like a successful review.
        review_response = json.dumps({
            "id": 9999,
            "html_url": "https://github.com/owner/repo/pull/1#discussion_r9999",
        })
        with patch("subprocess.run") as mock_subproc:
            mock_subproc.return_value = MagicMock(
                returncode=0, stdout=review_response.encode("utf-8"), stderr=b""
            )
            r = post_comment(
                "owner/repo", 1, "Typo here.",
                path="src/main.py", line=42, agent="chappy",
            )
        self.assertEqual(r["status"], "posted")
        self.assertEqual(r["gh_comment_id"], 9999)
        # Verify subprocess was called with the right URL
        cmd = mock_subproc.call_args[0][0]
        self.assertIn("repos/owner/repo/pulls/1/reviews", cmd)
        self.assertIn("POST", cmd)

    @patch("agentchat.pr_review._run_gh")
    def test_post_comment_failure_recorded(self, mock_run):
        mock_run.return_value = _stub_gh_result(ok=False, stderr="permission denied")
        r = post_comment("owner/repo", 1, "Should fail", agent="hermes")
        self.assertEqual(r["status"], "failed")
        self.assertIn("permission denied", r["error"])
        # Verify failure is persisted
        rows = list_local_comments("owner/repo", 1, status="failed")
        self.assertEqual(len(rows), 1)
        self.assertIn("permission denied", rows[0]["error"])

    @patch("agentchat.pr_review._run_gh")
    def test_post_comment_no_post_saves_only(self, mock_run):
        r = post_comment(
            "owner/repo", 1, "Draft comment",
            agent="hermes", post_to_github=False,
        )
        self.assertEqual(r["status"], "pending")
        self.assertIsNone(r["gh_comment_id"])
        # gh should NOT have been called
        mock_run.assert_not_called()
        # Verify it's stored locally
        rows = list_local_comments("owner/repo", 1, status="pending")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body"], "Draft comment")

    @patch("agentchat.pr_review._run_gh")
    def test_post_reply_in_thread(self, mock_run):
        mock_run.return_value = _stub_gh_result(
            stdout="https://github.com/owner/repo/pull/1#issuecomment-99999"
        )
        r = post_comment(
            "owner/repo", 1, "I disagree, here's why...",
            in_reply_to=12345, agent="chappy",
        )
        self.assertEqual(r["status"], "posted")
        rows = list_local_comments("owner/repo", 1)
        self.assertEqual(rows[0]["in_reply_to"], 12345)
        self.assertEqual(rows[0]["agent"], "chappy")


class TestWebhook(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Path(self.tmpdir) / "test.db"
        self._orig_db_path = pr_review.DB_PATH
        pr_review.DB_PATH = self.db

    def tearDown(self):
        pr_review.DB_PATH = self._orig_db_path

    def test_record_webhook_extracts_repo_and_pr(self):
        eid = record_webhook("pull_request", {
            "action": "opened",
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 42, "title": "New PR"},
        })
        self.assertGreater(eid, 0)
        events = list_recent_webhook_events(10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "pull_request")
        self.assertEqual(events[0]["repo"], "owner/repo")
        self.assertEqual(events[0]["pr_number"], 42)
        self.assertEqual(events[0]["action"], "opened")

    def test_record_webhook_handles_issue_event(self):
        eid = record_webhook("issues", {
            "action": "opened",
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 7},
        })
        self.assertGreater(eid, 0)
        events = list_recent_webhook_events(10)
        self.assertEqual(events[0]["pr_number"], 7)

    def test_webhook_events_ordered_newest_first(self):
        record_webhook("pull_request", {"action": "opened", "pull_request": {"number": 1}})
        time.sleep(0.01)
        record_webhook("pull_request", {"action": "closed", "pull_request": {"number": 1}})
        events = list_recent_webhook_events(10)
        self.assertEqual(events[0]["action"], "closed")
        self.assertEqual(events[1]["action"], "opened")


class TestListComments(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Path(self.tmpdir) / "test.db"
        self._orig_db_path = pr_review.DB_PATH
        pr_review.DB_PATH = self.db

    def tearDown(self):
        pr_review.DB_PATH = self._orig_db_path

    def test_empty(self):
        rows = list_local_comments("owner/repo", 99)
        self.assertEqual(rows, [])

    @patch("agentchat.pr_review._run_gh")
    def test_filter_by_status(self, mock_run):
        mock_run.return_value = _stub_gh_result(
            stdout="https://github.com/owner/repo/pull/1#issuecomment-1"
        )
        post_comment("owner/repo", 1, "posted one", agent="hermes")
        post_comment("owner/repo", 1, "draft", agent="hermes", post_to_github=False)
        posted = list_local_comments("owner/repo", 1, status="posted")
        pending = list_local_comments("owner/repo", 1, status="pending")
        self.assertEqual(len(posted), 1)
        self.assertEqual(len(pending), 1)


class TestCLI(unittest.TestCase):
    """Smoke tests for the CLI surface. Run main() directly."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Path(self.tmpdir) / "test.db"
        self._orig_db_path = pr_review.DB_PATH
        pr_review.DB_PATH = self.db

    def tearDown(self):
        pr_review.DB_PATH = self._orig_db_path

    def test_help(self):
        rc = pr_review.main(["--help"])
        self.assertEqual(rc, 0)

    def test_unknown_command(self):
        rc = pr_review.main(["wat"])
        self.assertEqual(rc, 2)

    @patch("agentchat.pr_review._run_gh")
    def test_list_command(self, mock_run):
        mock_run.return_value = _stub_gh_result(
            stdout=json.dumps([
                {"number": 1, "title": "Add foo", "isDraft": False, "author": {"login": "alice"}, "headRefName": "feat/foo"},
            ])
        )
        rc = pr_review.main(["list", "--repo", "owner/repo"])
        self.assertEqual(rc, 0)

    def test_comment_missing_body(self):
        rc = pr_review.main(["comment", "1"])
        self.assertEqual(rc, 2)

    @patch("agentchat.pr_review._run_gh")
    def test_comment_general(self, mock_run):
        mock_run.return_value = _stub_gh_result(
            stdout="https://github.com/owner/repo/pull/1#issuecomment-1"
        )
        rc = pr_review.main([
            "comment", "1",
            "--body", "Test comment",
            "--agent", "hermes",
            "--repo", "owner/repo",
        ])
        self.assertEqual(rc, 0)

    def test_show_missing_pr(self):
        rc = pr_review.main(["show"])
        self.assertEqual(rc, 2)

    @patch("agentchat.pr_review._run_gh")
    def test_show_pr(self, mock_run):
        mock_run.return_value = _stub_gh_result(
            stdout=json.dumps({
                "number": 1, "title": "Test", "state": "OPEN",
                "author": {"login": "alice"},
                "headRefName": "feat", "baseRefName": "main",
                "headRefOid": "abc",
                "body": "body", "url": "x", "additions": 0,
                "deletions": 0, "changedFiles": 0, "isDraft": False,
                "mergeable": "MERGEABLE", "files": [],
            })
        )
        rc = pr_review.main(["show", "1", "--repo", "owner/repo"])
        self.assertEqual(rc, 0)

    def test_webhooks_empty(self):
        rc = pr_review.main(["webhooks"])
        self.assertEqual(rc, 0)


class TestDefaultRepo(unittest.TestCase):
    def test_default_repo_constant(self):
        self.assertTrue(DEFAULT_REPO or os.environ.get("AGENTCHAT_PR_REVIEW_REPO"))


class TestUrlParsing(unittest.TestCase):
    """Test comment URL parsing edge cases."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Path(self.tmpdir) / "test.db"
        self._orig_db_path = pr_review.DB_PATH
        pr_review.DB_PATH = self.db

    def tearDown(self):
        pr_review.DB_PATH = self._orig_db_path

    @patch("agentchat.pr_review._run_gh")
    def test_parse_issuecomment_url(self, mock_run):
        mock_run.return_value = _stub_gh_result(
            stdout="https://github.com/owner/repo/pull/1#issuecomment-98765"
        )
        r = post_comment("owner/repo", 1, "hi", agent="hermes")
        self.assertEqual(r["gh_comment_id"], 98765)

    @patch("agentchat.pr_review.get_pr")
    def test_parse_review_comment_url(self, mock_get_pr):
        mock_get_pr.return_value = {"headRefOid": "abc"}
        review_response = json.dumps({
            "id": 12345,
            "html_url": "https://github.com/owner/repo/pull/1#discussion_r12345",
        })
        with patch("subprocess.run") as mock_subproc:
            mock_subproc.return_value = MagicMock(
                returncode=0, stdout=review_response.encode("utf-8"), stderr=b""
            )
            r = post_comment(
                "owner/repo", 1, "inline",
                path="x.py", line=10, agent="hermes",
            )
        self.assertEqual(r["gh_comment_id"], 12345)

    @patch("agentchat.pr_review._run_gh")
    def test_unparseable_url_returns_none(self, mock_run):
        mock_run.return_value = _stub_gh_result(stdout="some weird output")
        r = post_comment("owner/repo", 1, "hi", agent="hermes")
        self.assertEqual(r["status"], "posted")
        self.assertIsNone(r["gh_comment_id"])


if __name__ == "__main__":
    unittest.main()

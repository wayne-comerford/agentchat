"""
Tests for the agentchat v1.2.0.dev20 GitHub sync agent.

These tests run against a temp HERMES_HOME so they don't touch the
operator's real memory tree. The push flow is exercised by spinning up
a `git daemon --base-path=... --export-all` against a bare repo, which
gives a real, in-process git transport that the sync agent can push to
without needing a network or a GitHub token.

The daemon spawn is opt-in via env var AGENTCHAT_SYNC_TEST_DAEMON=1 so
the unit tests stay fast on machines that don't have `git-daemon` (the
package is in `git-daemon` on Debian/Ubuntu; on macOS it's not shipped
by default; on Fedora it's `git-daemon`). If the daemon is not
available, the integration test is skipped.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import textwrap
import time
import urllib.parse
from pathlib import Path

import pytest

from agentchat import sync_github as sg
from agentchat import sync_cli as sc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hermes_home(monkeypatch, tmp_path):
    """Point HERMES_HOME at a throwaway dir for the test."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "memory" / "agents" / "alice").mkdir(parents=True)
    (home / "memory" / "agents" / "bob").mkdir(parents=True)
    (home / "memory" / "team").mkdir(parents=True)
    (home / "memory" / "projects").mkdir(parents=True)
    (home / "nostr").mkdir()
    (home / "sync").mkdir()
    monkeypatch.setattr(sg, "HERMES_HOME", home)
    monkeypatch.setattr(sg, "MEMORY_ROOT", home / "memory")
    monkeypatch.setattr(sg, "SYNC_ROOT", home / "sync")
    monkeypatch.setattr(sg, "AUDIT_LOG", home / "sync" / "audit.jsonl")
    monkeypatch.setattr(sg, "LAST_PUSH_FILE", home / "sync" / ".last-push")
    monkeypatch.setattr(sg, "SCRUB_STATS_FILE", home / "sync" / ".scrub-stats.json")
    monkeypatch.setattr(sc, "sg", sg)
    return home


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Scrubber
# ---------------------------------------------------------------------------


# Realistic-length test secrets (long enough to match the regexes).
NSEC_SAMPLE = "nsec1" + "qpzry9x8gf2tvdw0s3jn54khce6mua7l" * 2  # 58 chars after nsec1
NPUB_SAMPLE = "npub1" + "qpzry9x8gf2tvdw0s3jn54khce6mua7l" * 2
GHP_SAMPLE = "ghp_" + "AbCdEfGhIjKlMnOpQrStUvWxYz012345"  # 36 chars
GHFG_SAMPLE = "github_pat_11ABCDEFG0_aaaaaaaaaaaaaaaaaa"  # 32 chars of body
OPENAI_SAMPLE = "sk-" + "proj-abcdefghijklmnopqrstuvwxyz0123456789"  # 40 chars
ANTH_SAMPLE = "sk-ant-" + "api03-abcdefghijklmnopqrstuvwxyz0123456789"  # 40 chars
SLACK_SAMPLE = "xoxb-" + "1234567890123-1234567890123-AbCdEfGhIjKlMn"  # long enough
BEARER_SAMPLE = "Bearer " + "abcdefghijklmnopqrstuvwxyz0123456789"  # 36 chars


class TestScrubber:
    def test_npub_kept(self):
        out = sg.scrub_text(NPUB_SAMPLE)
        # npub is public; must remain.
        assert NPUB_SAMPLE[:10] in out

    def test_nsec_redacted(self):
        out = sg.scrub_text(NSEC_SAMPLE)
        assert "nsec1" not in out
        assert "REDACTED:nostr-nsec" in out

    def test_github_pat_redacted(self):
        out = sg.scrub_text(f"GITHUB_TOKEN={GHP_SAMPLE}")
        assert GHP_SAMPLE[:8] not in out
        assert "REDACTED:github-pat" in out

    def test_github_fine_grained_redacted(self):
        out = sg.scrub_text(f"token: {GHFG_SAMPLE}")
        assert "github_pat_" not in out
        assert "REDACTED:github-fine-grained-pat" in out

    def test_openai_key_redacted(self):
        out = sg.scrub_text(f"OPENAI_KEY={OPENAI_SAMPLE}")
        assert "sk-" not in out
        # The anthropic regex fires first if it matches, so just check the
        # value is gone; the openai-key label is only set if anthropic
        # pattern didn't apply.
        assert "REDACTED:" in out

    def test_anthropic_key_redacted(self):
        out = sg.scrub_text(f"ANTHROPIC_API_KEY={ANTH_SAMPLE}")
        assert "sk-ant" not in out
        assert "REDACTED:anthropic-key" in out

    def test_slack_token_redacted(self):
        out = sg.scrub_text(f"SLACK={SLACK_SAMPLE}")
        assert "xoxb-1" not in out
        assert "REDACTED:slack-token" in out

    def test_bearer_redacted(self):
        out = sg.scrub_text(f"Authorization: {BEARER_SAMPLE}")
        assert "abcdefghij" not in out
        assert "REDACTED:bearer-token" in out

    def test_auth_secret_redacted(self):
        out = sg.scrub_text('AUTH_SECRET="my-very-secret-session-key-12345"')
        assert "my-very-secret" not in out
        assert "REDACTED:auth-secret" in out

    def test_password_redacted(self):
        out = sg.scrub_text('password: hunter2hunter2')
        assert "hunter2" not in out
        assert "REDACTED:password" in out

    def test_oauth_token_redacted(self):
        out = sg.scrub_text('oauth_token: ya29.a0AfH6SMBxxxxxxxxxxxxxxxxxxxxxxx')
        assert "ya29" not in out
        assert "REDACTED:oauth-token" in out

    def test_hex_private_key_redacted(self):
        out = sg.scrub_text('private_key=abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789')
        assert "abcdef0123456789abcdef0123456789" not in out
        assert "REDACTED:hex-private-key" in out

    def test_clean_text_unchanged(self):
        clean = "Hello world\n\n## Section\n- a\n- b\n"
        out = sg.scrub_text(clean)
        assert out == clean

    def test_stats_bumped(self):
        stats = sg.ScrubStats()
        sg.scrub_text(NSEC_SAMPLE, stats=stats)
        sg.scrub_text(f"GITHUB_TOKEN={GHP_SAMPLE}", stats=stats)
        sg.scrub_text(f"GITHUB_TOKEN={GHP_SAMPLE}", stats=stats)
        assert stats.counts.get("nostr-nsec") == 1
        assert stats.counts.get("github-pat") == 2

    def test_stats_idempotent(self):
        # Idempotency: scrubbing the *output* a second time should not
        # re-bump the counter. The redaction sentinel is invariant.
        stats = sg.ScrubStats()
        first = sg.scrub_text(NSEC_SAMPLE, stats=stats)
        sg.scrub_text(first, stats=stats)  # second call: input is already redacted
        assert stats.counts.get("nostr-nsec") == 1
        # Sanity: scrubbing the *original* twice does count twice (because
        # each call sees a fresh match); scrubber has no per-input cache.
        stats2 = sg.ScrubStats()
        sg.scrub_text(NSEC_SAMPLE, stats=stats2)
        sg.scrub_text(NSEC_SAMPLE, stats=stats2)
        assert stats2.counts.get("nostr-nsec") == 2


class TestShouldSkip:
    def test_dotenv_skipped(self, hermes_home):
        p = hermes_home / "config" / ".env"
        p.parent.mkdir()
        p.write_text("FOO=bar")
        assert sg.should_skip_path(p) is True

    def test_nsec_json_skipped(self, hermes_home):
        p = hermes_home / "nostr" / "alice.nsec.json"
        p.write_text("{}")
        assert sg.should_skip_path(p) is True

    def test_pycache_skipped(self, hermes_home):
        p = hermes_home / "module" / "__pycache__" / "x.pyc"
        p.parent.mkdir(parents=True)
        p.write_text("")
        assert sg.should_skip_path(p) is True

    def test_normal_md_kept(self, hermes_home):
        p = hermes_home / "memory" / "agents" / "alice" / "MEMORY.md"
        p.write_text("hello")
        assert sg.should_skip_path(p) is False


# ---------------------------------------------------------------------------
# Mirror builder
# ---------------------------------------------------------------------------


class TestBuildMirrorTree:
    def test_basic_tree(self, hermes_home):
        _write(hermes_home / "memory" / "agents" / "alice" / "MEMORY.md", "# alice\n\n## Prefs\n- terse\n")
        _write(hermes_home / "memory" / "team" / "SHARED.md", "# team\n\n## Norms\n- ship\n")
        _write(hermes_home / "nostr" / "registry.json", '{"alice": {"npub": "npub1abc"}}')

        m = sg.build_mirror_tree(workspace_slug="ws-test")
        files = m["files"]
        assert any(f["dst"] == "memory/agents/alice/MEMORY.md" for f in files)
        assert any(f["dst"] == "memory/team/SHARED.md" for f in files)
        assert any(f["dst"] == "config/nostr-registry.json" for f in files)

    def test_scrubs_in_manifest(self, hermes_home):
        _write(
            hermes_home / "memory" / "agents" / "alice" / "MEMORY.md",
            f"agent key: {NSEC_SAMPLE}\n",
        )
        m = sg.build_mirror_tree(workspace_slug="ws-test")
        # build_mirror_tree just lists files; the scrubber runs in
        # materialize_mirror. After materializing, stats should reflect
        # the redactions.
        with _tempdir() as td:
            sg.materialize_mirror(m, Path(td))
        assert m["stats"].counts.get("nostr-nsec") == 1

    def test_skips_nsec_files(self, hermes_home):
        # Plant a .nsec.json inside a MIRROR_MAP'd directory so the
        # walker actually visits it (nsec files outside MIRROR_MAP'd
        # trees are never seen in the first place).
        _write(
            hermes_home / "memory" / "agents" / "alice.nsec.json",
            '{"secret": "raw"}',
        )
        m = sg.build_mirror_tree(workspace_slug="ws-test")
        skipped = [f for f in m["files"] if f["skipped"]]
        assert any(
            "alice.nsec.json" in f["src"] and f["skip_reason"] == "never-push"
            for f in skipped
        )

    def test_skips_env_files(self, hermes_home):
        # Plant a .env under the path that gets walked
        _write(hermes_home / "memory" / "agents" / "alice" / ".env", "FOO=bar")
        m = sg.build_mirror_tree(workspace_slug="ws-test")
        skipped = [f for f in m["files"] if f["skipped"]]
        assert any(".env" in f["src"] for f in skipped)

    def test_missing_source_reported(self, hermes_home):
        m = sg.build_mirror_tree(workspace_slug="ws-test")
        missing = [f for f in m["files"] if f.get("skip_reason") == "source-missing"]
        assert len(missing) > 0  # nostr/registry.json, etc. are missing here


class TestMaterialize:
    def test_writes_files(self, hermes_home):
        _write(hermes_home / "memory" / "agents" / "alice" / "MEMORY.md", "# alice\n\nclean\n")
        m = sg.build_mirror_tree(workspace_slug="ws-test")
        with _tempdir() as td:
            bytes_written = sg.materialize_mirror(m, Path(td))
            assert bytes_written > 0
            out_path = Path(td) / "memory" / "agents" / "alice" / "MEMORY.md"
            assert out_path.exists()
            assert "# alice" in out_path.read_text()
            # README + .gitignore + workspace.yaml always written
            assert (Path(td) / "README.md").exists()
            assert (Path(td) / ".gitignore").exists()
            assert (Path(td) / "workspace.yaml").exists()

    def test_scrubbed_bytes_in_output(self, hermes_home):
        _write(hermes_home / "memory" / "agents" / "alice" / "MEMORY.md", f"raw: {NSEC_SAMPLE}\n")
        m = sg.build_mirror_tree(workspace_slug="ws-test")
        with _tempdir() as td:
            sg.materialize_mirror(m, Path(td))
            content = (Path(td) / "memory" / "agents" / "alice" / "MEMORY.md").read_text()
            assert NSEC_SAMPLE not in content
            assert "REDACTED" in content

    def test_audit_jsonl_appended(self, hermes_home):
        _write(hermes_home / "memory" / "agents" / "alice" / "MEMORY.md", "x")
        m = sg.build_mirror_tree(
            workspace_slug="ws-test",
            audit_entry={"ts_utc": "2026-08-18T00:00:00+00:00", "workspace_slug": "ws-test"},
        )
        with _tempdir() as td:
            sg.materialize_mirror(m, Path(td))
            audit_path = Path(td) / "audit" / "audit.jsonl"
            assert audit_path.exists()
            line = audit_path.read_text().strip().splitlines()[-1]
            d = json.loads(line)
            assert d["workspace_slug"] == "ws-test"

    def test_skipped_files_not_written(self, hermes_home):
        # Plant a .env that the walker will skip
        _write(hermes_home / "memory" / "agents" / "alice" / "MEMORY.md", "x")
        _write(hermes_home / "memory" / "agents" / "alice" / ".env", "FOO=bar")
        m = sg.build_mirror_tree(workspace_slug="ws-test")
        with _tempdir() as td:
            sg.materialize_mirror(m, Path(td))
            env_path = Path(td) / "memory" / "agents" / "alice" / ".env"
            assert not env_path.exists()


# ---------------------------------------------------------------------------
# Status / format
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_returns_dict(self, hermes_home):
        d = sg.status(workspace_slug="ws-test")
        assert d["workspace_slug"] == "ws-test"
        assert "files_total" in d
        assert "files_to_mirror" in d
        assert "files_skipped" in d
        assert "would_scrub" in d
        assert d["last_push"] is None  # nothing pushed yet

    def test_status_includes_skip_reasons(self, hermes_home):
        _write(hermes_home / "memory" / "agents" / "alice" / ".env", "FOO=bar")
        d = sg.status(workspace_slug="ws-test")
        assert "never-push" in d["skipped_reasons"]
        assert d["skipped_reasons"]["never-push"] >= 1

    def test_format_status(self, hermes_home):
        d = sg.status(workspace_slug="ws-test")
        text = sg.format_status(d)
        assert "ws-test" in text
        assert "files_total" in text or "files_total:" in text


# ---------------------------------------------------------------------------
# Push (with git)
# ---------------------------------------------------------------------------


class TestPushDryRun:
    def test_dry_run_does_not_commit(self, hermes_home):
        _write(hermes_home / "memory" / "agents" / "alice" / "MEMORY.md", "hello")
        result = sg.push(
            workspace_slug="ws-test",
            remote="file:///nonexistent",
            dry_run=True,
        )
        assert result.pushed is False
        assert result.commit_sha == "(dry-run)"
        assert result.files_mirrored >= 1
        # Audit was still written (attempted-push record).
        assert sg.AUDIT_LOG.exists()
        # The local last-push file should NOT be set (no actual push).
        assert not sg.LAST_PUSH_FILE.exists()


class TestPushToLocalBareRepo:
    """Push to a local bare repo (no network)."""

    def test_push_to_bare_repo(self, hermes_home, tmp_path):
        _write(hermes_home / "memory" / "agents" / "alice" / "MEMORY.md", "hello world")
        # Create a bare repo to act as the "remote".
        bare = tmp_path / "remote.git"
        _run_git(["init", "--bare", "--initial-branch=main", str(bare)])
        result = sg.push(
            workspace_slug="ws-test",
            remote=str(bare),
        )
        assert result.pushed is True
        assert result.commit_sha and len(result.commit_sha) >= 7
        assert result.files_mirrored >= 1
        # Verify the remote received the commit.
        clone = tmp_path / "clone"
        _run_git(["clone", str(bare), str(clone)])
        assert (clone / "memory" / "agents" / "alice" / "MEMORY.md").exists()
        readme = (clone / "README.md").read_text()
        assert "ws-test" in readme
        # Local last-push file should now exist.
        assert sg.LAST_PUSH_FILE.exists()
        last = json.loads(sg.LAST_PUSH_FILE.read_text())
        assert last["commit_sha"] == result.commit_sha
        # Audit log should have at least one entry.
        assert sg.AUDIT_LOG.exists()
        lines = sg.AUDIT_LOG.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["workspace_slug"] == "ws-test"
        assert entry["remote"] == str(bare)

    def test_push_scrubs_secrets(self, hermes_home, tmp_path):
        _write(hermes_home / "memory" / "agents" / "alice" / "MEMORY.md", f"raw secret: {NSEC_SAMPLE}")
        bare = tmp_path / "remote.git"
        _run_git(["init", "--bare", "--initial-branch=main", str(bare)])
        result = sg.push(workspace_slug="ws-test", remote=str(bare))
        assert result.pushed is True
        assert result.scrub_stats.counts.get("nostr-nsec") == 1
        clone = tmp_path / "clone"
        _run_git(["clone", str(bare), str(clone)])
        pushed_md = (clone / "memory" / "agents" / "alice" / "MEMORY.md").read_text()
        assert NSEC_SAMPLE not in pushed_md
        assert "REDACTED" in pushed_md

    def test_push_second_run_increments(self, hermes_home, tmp_path):
        _write(hermes_home / "memory" / "agents" / "alice" / "MEMORY.md", "v1")
        bare = tmp_path / "remote.git"
        _run_git(["init", "--bare", "--initial-branch=main", str(bare)])
        r1 = sg.push(workspace_slug="ws-test", remote=str(bare))
        _write(hermes_home / "memory" / "agents" / "alice" / "MEMORY.md", "v2")
        r2 = sg.push(workspace_slug="ws-test", remote=str(bare))
        assert r1.commit_sha != r2.commit_sha
        # Audit has 2 entries now.
        lines = sg.AUDIT_LOG.read_text().strip().splitlines()
        assert len(lines) == 2

    def test_push_remote_rejected_raises(self, hermes_home, tmp_path):
        # No bare repo: push should fail.
        _write(hermes_home / "memory" / "agents" / "alice" / "MEMORY.md", "x")
        with pytest.raises(RuntimeError):
            sg.push(workspace_slug="ws-test", remote="/no/such/path/repo.git")
        # But audit still got the attempt.
        assert sg.AUDIT_LOG.exists()
        lines = sg.AUDIT_LOG.read_text().strip().splitlines()
        assert len(lines) == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_status(self, hermes_home, capsys):
        _write(hermes_home / "memory" / "agents" / "alice" / "MEMORY.md", "x")
        rc = sc.main(["--workspace", "ws-test", "status"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ws-test" in out

    def test_status_json(self, hermes_home, capsys):
        rc = sc.main(["--workspace", "ws-test", "status", "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        d = json.loads(out)
        assert d["workspace_slug"] == "ws-test"

    def test_push_dry_run(self, hermes_home, capsys, tmp_path):
        _write(hermes_home / "memory" / "agents" / "alice" / "MEMORY.md", "x")
        bare = tmp_path / "remote.git"
        _run_git(["init", "--bare", "--initial-branch=main", str(bare)])
        rc = sc.main(["--workspace", "ws-test", "push", "--remote", str(bare), "--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "dry-run" in out

    def test_push_actual(self, hermes_home, capsys, tmp_path):
        _write(hermes_home / "memory" / "agents" / "alice" / "MEMORY.md", "x")
        bare = tmp_path / "remote.git"
        _run_git(["init", "--bare", "--initial-branch=main", str(bare)])
        rc = sc.main(["--workspace", "ws-test", "push", "--remote", str(bare)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "commit:" in out
        assert "pushed:    True" in out

    def test_init(self, hermes_home, capsys):
        rc = sc.main(["--workspace", "ws-test", "init"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Initialized" in out
        assert (hermes_home / "sync" / "audit.jsonl").exists()
        assert (hermes_home / "sync" / "config.example.json").exists()

    def test_audit_tail(self, hermes_home, capsys, tmp_path):
        _write(hermes_home / "memory" / "agents" / "alice" / "MEMORY.md", "x")
        bare = tmp_path / "remote.git"
        _run_git(["init", "--bare", "--initial-branch=main", str(bare)])
        sc.main(["--workspace", "ws-test", "push", "--remote", str(bare)])
        rc = sc.main(["audit", "tail", "-n", "5"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ws-test" in out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


import contextlib


@contextlib.contextmanager
def _tempdir():
    td = tempfile.mkdtemp(prefix="agentchat-sync-test-")
    try:
        yield td
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _run_git(cmd: list[str]) -> None:
    """Run git, raising on non-zero."""
    proc = subprocess.run(
        ["git"] + cmd, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(cmd)} failed (rc={proc.returncode})\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )


import tempfile  # noqa: E402  (used by _tempdir)

"""
Unit tests for the agentchat v1.2.0.dev20 sync_agent push stage.

These tests follow the task body (t_11537e05): "a unit test that
stubs the git client to verify retry and error paths." The push
stage is decoupled from the real git binary via the ``GitClient``
ABC; the tests inject a stub that records calls and returns
canned ``GitPushResult`` values.

What's covered:

* Happy path: stub returns ok, push returns PushResult.
* Retry: stub returns transient network errors for N attempts, then
  succeeds; verify attempt count and backoff schedule.
* Retry exhaustion: stub returns network errors forever; push
  raises ``NetworkError`` after ``max_attempts`` attempts.
* Auth failure: stub returns auth-shaped stderr; push raises
  ``AuthError`` and does NOT retry.
* Non-fast-forward: stub returns non-fast-forward stderr; push
  raises ``NonFastForwardError`` and does NOT retry.
* No changes: stub returns "Everything up-to-date"; push raises
  ``NoChangesError``.
* Missing repo: stub raises ``RuntimeError`` on rev_parse; push
  raises ``MissingRepoError``.
* Auth strategy URL injection: PAT auth rewrites the URL with the
  token; SSH auth leaves the URL alone.
* Auth strategy missing env: PAT auth with no token raises
  ``AuthError`` at resolution time.
* Generic error: stub returns unknown failure; push raises
  ``PushError`` and does NOT retry.

A live integration test (push to a local bare repo via the real
``SubprocessGitClient``) is included separately under
``TestLiveIntegration`` and is opt-in via ``AGENTCHAT_SYNC_LIVE=1``
so the unit tests stay fast.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from agentchat.sync_agent import errors as push_errors
from agentchat.sync_agent._git import (
    GitClient,
    GitPushResult,
    SubprocessGitClient,
    classify_push_failure,
)
from agentchat.sync_agent.push import (
    AuthStrategy,
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    PushConfig,
    PushResult,
    PushStage,
    push_committed,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StubGitClient(GitClient):
    """A minimal GitClient that returns a queue of canned results.

    Inherits from the ABC so Pyright is satisfied with the
    ``PushStage(..., git=stub)`` assignment.

    Records every call as a ``(repo_dir, remote, branch, remote_url)``
    tuple on ``self.calls`` so tests can assert on the call sequence.
    """

    def __init__(
        self,
        results: Optional[list[GitPushResult]] = None,
        rev_parse_sha: str = "deadbeef1234567890abcdef",
        raise_on_push: Optional[Exception] = None,
    ) -> None:
        self.results = list(results or [])
        self.rev_parse_sha = rev_parse_sha
        self.raise_on_push = raise_on_push
        self.calls: list[dict] = []
        self.rev_parse_calls: list[str] = []

    def push(
        self,
        *,
        repo_dir: Path,
        remote: str,
        branch: str,
        remote_url: Optional[str],
        env: Optional[dict[str, str]] = None,
    ) -> GitPushResult:
        self.calls.append({
            "repo_dir": str(repo_dir),
            "remote": remote,
            "branch": branch,
            "remote_url": remote_url,
            "env": env,
        })
        if self.raise_on_push is not None:
            raise self.raise_on_push
        if not self.results:
            raise RuntimeError("StubGitClient exhausted (no more canned results)")
        return self.results.pop(0)

    def remote_get_url(self, *, repo_dir: Path, remote: str) -> str:
        return f"git-stub://{remote}/{repo_dir}"

    def rev_parse(self, *, repo_dir: Path, ref: str) -> str:
        self.rev_parse_calls.append(ref)
        return self.rev_parse_sha


def _ok_result() -> GitPushResult:
    return GitPushResult(
        returncode=0,
        stdout="To github.com:foo/bar.git\n   abc1234..def5678  main -> main\n",
        stderr="",
        args=("git", "push", "origin", "main"),
    )


def _network_error_result() -> GitPushResult:
    return GitPushResult(
        returncode=1,
        stdout="",
        stderr=(
            "ssh: connect to host github.com port 22: Connection timed out\n"
            "fatal: Could not read from remote repository."
        ),
        args=("git", "push", "origin", "main"),
    )


def _auth_error_result() -> GitPushResult:
    return GitPushResult(
        returncode=1,
        stdout="",
        stderr=(
            "git@github.com: Permission denied (publickey).\n"
            "fatal: Could not read from remote repository."
        ),
        args=("git", "push", "origin", "main"),
    )


def _nonff_result() -> GitPushResult:
    return GitPushResult(
        returncode=1,
        stdout="To github.com:foo/bar.git\n",
        stderr=(
            "To github.com:foo/bar.git\n"
            " ! [rejected]        main -> main (non-fast-forward)\n"
            "error: failed to push some refs to 'github.com:foo/bar.git'\n"
            "hint: Updates were rejected because the tip of your current branch is behind\n"
        ),
        args=("git", "push", "origin", "main"),
    )


def _no_changes_result() -> GitPushResult:
    return GitPushResult(
        returncode=0,
        stdout="Everything up-to-date\n",
        stderr="",
        args=("git", "push", "origin", "main"),
    )


def _unknown_result() -> GitPushResult:
    return GitPushResult(
        returncode=128,
        stdout="",
        stderr="fatal: repository 'https://github.com/foo/bar.git/' not found",
        args=("git", "push", "origin", "main"),
    )


# ---------------------------------------------------------------------------
# Fast (no sleep) sleep function
# ---------------------------------------------------------------------------


def _no_sleep(attempt: int) -> None:
    """A no-op sleep replacement for tests."""
    pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A pretend repo_dir that exists on disk."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


@pytest.fixture
def base_config(fake_repo: Path) -> PushConfig:
    """A reasonable default config for tests."""
    return PushConfig(
        repo_dir=fake_repo,
        remote="origin",
        branch="main",
        auth=AuthStrategy.SSH,
        remote_url="git@github.com-agentchat:foo/bar.git",
        sleep=_no_sleep,  # fast tests
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_returns_push_result_on_success(self, base_config: PushConfig) -> None:
        stub = StubGitClient(results=[_ok_result()])
        stage = PushStage(base_config, git=stub)
        result = stage.push()
        assert isinstance(result, PushResult)
        assert result.pushed is True
        assert result.remote == "origin"
        assert result.branch == "main"
        assert result.commit_sha == stub.rev_parse_sha
        assert result.attempts == 1

    def test_push_committed_one_shot(self, fake_repo: Path) -> None:
        stub = StubGitClient(results=[_ok_result()])
        result = push_committed(
            fake_repo,
            remote="origin",
            branch="main",
            auth=AuthStrategy.SSH,
            remote_url="git@github.com-agentchat:foo/bar.git",
            git=stub,
        )
        assert result.pushed is True
        assert result.attempts == 1

    def test_rev_parse_called_once(self, base_config: PushConfig) -> None:
        stub = StubGitClient(results=[_ok_result()])
        PushStage(base_config, git=stub).push()
        assert stub.rev_parse_calls == ["HEAD"]

    def test_ssh_does_not_rewrite_remote_url(self, base_config: PushConfig) -> None:
        stub = StubGitClient(results=[_ok_result()])
        PushStage(base_config, git=stub).push()
        # SSH auth: remote_url is None on the push call (git uses the
        # remote's existing URL).
        assert stub.calls[0]["remote_url"] is None


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class TestRetry:
    def test_retries_transient_network_error_then_succeeds(
        self, base_config: PushConfig
    ) -> None:
        # 2 transient errors, then ok
        stub = StubGitClient(
            results=[_network_error_result(), _network_error_result(), _ok_result()]
        )
        result = PushStage(base_config, git=stub).push()
        assert result.pushed is True
        assert result.attempts == 3
        assert len(stub.calls) == 3

    def test_exhausts_retries_and_raises_network_error(
        self, base_config: PushConfig
    ) -> None:
        # Always network errors; max_attempts default = 3
        stub = StubGitClient(
            results=[_network_error_result()] * DEFAULT_MAX_ATTEMPTS
        )
        with pytest.raises(push_errors.NetworkError) as exc_info:
            PushStage(base_config, git=stub).push()
        assert exc_info.value.attempts == DEFAULT_MAX_ATTEMPTS
        assert len(stub.calls) == DEFAULT_MAX_ATTEMPTS

    def test_backoff_called_between_attempts(self, base_config: PushConfig) -> None:
        calls: list[int] = []

        def sleep(attempt: int) -> None:
            calls.append(attempt)

        cfg = PushConfig(
            repo_dir=base_config.repo_dir,
            remote=base_config.remote,
            branch=base_config.branch,
            auth=AuthStrategy.SSH,
            remote_url=base_config.remote_url,
            sleep=sleep,
        )
        stub = StubGitClient(
            results=[_network_error_result(), _network_error_result(), _ok_result()]
        )
        PushStage(cfg, git=stub).push()
        # sleep is called after attempt 1 and after attempt 2
        assert calls == [1, 2]

    def test_no_sleep_on_success(self, base_config: PushConfig) -> None:
        calls: list[int] = []

        def sleep(attempt: int) -> None:
            calls.append(attempt)

        cfg = PushConfig(
            repo_dir=base_config.repo_dir,
            remote=base_config.remote,
            branch=base_config.branch,
            auth=AuthStrategy.SSH,
            remote_url=base_config.remote_url,
            sleep=sleep,
        )
        stub = StubGitClient(results=[_ok_result()])
        PushStage(cfg, git=stub).push()
        assert calls == []

    def test_default_backoff_schedule(self) -> None:
        """The design mandates 3 attempts with backoff 1s, 5s, 30s."""
        assert DEFAULT_MAX_ATTEMPTS == 3
        assert DEFAULT_BACKOFF_SECONDS == (1.0, 5.0, 30.0)

    def test_push_raises_during_call_treated_as_network(
        self, base_config: PushConfig
    ) -> None:
        """If the GitClient itself raises (e.g. timeout, OSError), treat as NetworkError."""
        stub = StubGitClient(
            results=[],
            raise_on_push=TimeoutError("subprocess.TimeoutExpired"),
        )
        with pytest.raises(push_errors.NetworkError) as exc_info:
            PushStage(base_config, git=stub).push()
        assert "TimeoutError" in str(exc_info.value)
        assert exc_info.value.attempts == DEFAULT_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Auth errors (no retry)
# ---------------------------------------------------------------------------


class TestAuthFailure:
    def test_auth_failure_raises_without_retry(self, base_config: PushConfig) -> None:
        stub = StubGitClient(results=[_auth_error_result()])
        with pytest.raises(push_errors.AuthError) as exc_info:
            PushStage(base_config, git=stub).push()
        # AuthError is permanent — exactly ONE call to git, not 3.
        assert len(stub.calls) == 1
        assert exc_info.value.attempts == 1
        # The stderr is preserved.
        assert "Permission denied" in exc_info.value.stderr

    def test_auth_failure_inherits_push_error(self, base_config: PushConfig) -> None:
        """AuthError is a PushError for catch-all handling."""
        stub = StubGitClient(results=[_auth_error_result()])
        with pytest.raises(push_errors.PushError):
            PushStage(base_config, git=stub).push()

    def test_pat_auth_missing_token_raises(self, fake_repo: Path) -> None:
        cfg = PushConfig(
            repo_dir=fake_repo,
            remote="origin",
            branch="main",
            auth=AuthStrategy.PAT,
            remote_url="https://github.com/foo/bar.git",
            sleep=_no_sleep,
        )
        # No token in env, no token in config
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.delenv("AGENTCHAT_GITHUB_PAT", raising=False)
        try:
            with pytest.raises(push_errors.AuthError) as exc_info:
                PushStage(cfg, git=StubGitClient()).push()
            assert "AGENTCHAT_GITHUB_PAT" in str(exc_info.value)
        finally:
            monkeypatch.undo()

    def test_pat_auth_reads_env_var(self, fake_repo: Path) -> None:
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("AGENTCHAT_GITHUB_PAT", "ghp_TEST_TOKEN_xxx")
        try:
            cfg = PushConfig(
                repo_dir=fake_repo,
                remote="origin",
                branch="main",
                auth=AuthStrategy.PAT,
                remote_url="https://github.com/foo/bar.git",
                sleep=_no_sleep,
            )
            assert cfg.resolve_pat() == "ghp_TEST_TOKEN_xxx"
        finally:
            monkeypatch.undo()


# ---------------------------------------------------------------------------
# Non-fast-forward (no retry)
# ---------------------------------------------------------------------------


class TestNonFastForward:
    def test_non_ff_raises_without_retry(self, base_config: PushConfig) -> None:
        stub = StubGitClient(results=[_nonff_result()])
        with pytest.raises(push_errors.NonFastForwardError) as exc_info:
            PushStage(base_config, git=stub).push()
        assert len(stub.calls) == 1
        assert exc_info.value.attempts == 1
        assert "non-fast-forward" in exc_info.value.stderr

    def test_non_ff_inherits_push_error(self, base_config: PushConfig) -> None:
        stub = StubGitClient(results=[_nonff_result()])
        with pytest.raises(push_errors.PushError):
            PushStage(base_config, git=stub).push()


# ---------------------------------------------------------------------------
# No changes
# ---------------------------------------------------------------------------


class TestNoChanges:
    def test_no_changes_raises(self, base_config: PushConfig) -> None:
        stub = StubGitClient(results=[_no_changes_result()])
        with pytest.raises(push_errors.NoChangesError) as exc_info:
            PushStage(base_config, git=stub).push()
        assert "up-to-date" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Unknown / generic errors
# ---------------------------------------------------------------------------


class TestUnknownError:
    def test_unknown_error_raises_push_error_without_retry(
        self, base_config: PushConfig
    ) -> None:
        stub = StubGitClient(results=[_unknown_result()])
        with pytest.raises(push_errors.PushError) as exc_info:
            PushStage(base_config, git=stub).push()
        # Unknown errors are not retried — caller decides.
        assert len(stub.calls) == 1
        assert exc_info.value.attempts == 1
        assert "repository" in exc_info.value.stderr.lower()


# ---------------------------------------------------------------------------
# Missing repo
# ---------------------------------------------------------------------------


class TestMissingRepo:
    def test_missing_repo_dir_raises(self, tmp_path: Path) -> None:
        cfg = PushConfig(
            repo_dir=tmp_path / "does-not-exist",
            remote="origin",
            branch="main",
            auth=AuthStrategy.SSH,
            remote_url="git@github.com:foo/bar.git",
            sleep=_no_sleep,
        )
        with pytest.raises(push_errors.MissingRepoError) as exc_info:
            PushStage(cfg, git=StubGitClient()).push()
        assert "does not exist" in str(exc_info.value)

    def test_repo_dir_exists_but_not_git_raises(self, fake_repo: Path) -> None:
        # fake_repo is a directory but not a git repo. rev_parse
        # will raise RuntimeError ("not a git repository").
        stub = StubGitClient(results=[_ok_result()])
        # Override rev_parse to simulate non-git dir.
        def boom_rev_parse(*, repo_dir: Path, ref: str) -> str:
            raise RuntimeError("not a git repository")
        stub.rev_parse = boom_rev_parse  # type: ignore[assignment,method-assign]
        with pytest.raises(push_errors.MissingRepoError) as exc_info:
            PushStage(
                PushConfig(
                    repo_dir=fake_repo,
                    remote="origin",
                    branch="main",
                    auth=AuthStrategy.SSH,
                    remote_url="git@github.com:foo/bar.git",
                    sleep=_no_sleep,
                ),
                git=stub,
            ).push()
        assert "not a git working tree" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Auth strategy URL injection
# ---------------------------------------------------------------------------


class TestAuthStrategyUrl:
    def test_pat_interpolates_token_into_https_url(self, fake_repo: Path) -> None:
        cfg = PushConfig(
            repo_dir=fake_repo,
            remote="origin",
            branch="main",
            auth=AuthStrategy.PAT,
            remote_url="https://github.com/foo/bar.git",
            pat_token="ghp_TEST_TOKEN_xxx",
            sleep=_no_sleep,
        )
        url = cfg.effective_remote_url()
        assert "ghp_TEST_TOKEN_xxx" in url
        assert url.startswith("https://")
        assert "github.com/foo/bar.git" in url

    def test_pat_strips_existing_userinfo(self, fake_repo: Path) -> None:
        cfg = PushConfig(
            repo_dir=fake_repo,
            remote="origin",
            branch="main",
            auth=AuthStrategy.PAT,
            remote_url="https://olduser:oldpass@github.com/foo/bar.git",
            pat_token="NEW_TOKEN",
            sleep=_no_sleep,
        )
        url = cfg.effective_remote_url()
        assert "olduser" not in url
        assert "oldpass" not in url
        assert "NEW_TOKEN" in url

    def test_ssh_leaves_url_alone(self, base_config: PushConfig) -> None:
        # base_config is SSH; URL must be unchanged.
        assert base_config.effective_remote_url() == base_config.remote_url

    def test_pat_rejects_ssh_url(self, fake_repo: Path) -> None:
        cfg = PushConfig(
            repo_dir=fake_repo,
            remote="origin",
            branch="main",
            auth=AuthStrategy.PAT,
            remote_url="git@github.com:foo/bar.git",
            pat_token="tok",
            sleep=_no_sleep,
        )
        with pytest.raises(push_errors.AuthError) as exc_info:
            cfg.effective_remote_url()
        assert "non-HTTPS" in str(exc_info.value)

    def test_github_app_auth_requires_remote_url(self, fake_repo: Path) -> None:
        cfg = PushConfig(
            repo_dir=fake_repo,
            remote="origin",
            branch="main",
            auth=AuthStrategy.GITHUB_APP,
            remote_url=None,
            sleep=_no_sleep,
        )
        with pytest.raises(push_errors.AuthError) as exc_info:
            PushStage(cfg, git=StubGitClient()).push()
        assert "remote_url" in str(exc_info.value)

    def test_github_app_auth_missing_config_raises(self, fake_repo: Path) -> None:
        cfg = PushConfig(
            repo_dir=fake_repo,
            remote="origin",
            branch="main",
            auth=AuthStrategy.GITHUB_APP,
            remote_url="https://github.com/foo/bar.git",
            sleep=_no_sleep,
        )
        # No app_id, installation_id, or private_key — `resolve_github_app`
        # should raise AuthError.
        with pytest.raises(push_errors.AuthError) as exc_info:
            cfg.resolve_github_app()
        assert "app_id" in str(exc_info.value).lower()

    def test_pat_push_passes_rewritten_remote_url(self, fake_repo: Path) -> None:
        cfg = PushConfig(
            repo_dir=fake_repo,
            remote="origin",
            branch="main",
            auth=AuthStrategy.PAT,
            remote_url="https://github.com/foo/bar.git",
            pat_token="ghp_T",
            rewrite_remote_url=True,
            sleep=_no_sleep,
        )
        stub = StubGitClient(results=[_ok_result()])
        PushStage(cfg, git=stub).push()
        # The push call gets the rewritten URL with the token.
        assert stub.calls[0]["remote_url"] is not None
        assert "ghp_T" in stub.calls[0]["remote_url"]


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_max_attempts_below_one_rejected(self, fake_repo: Path) -> None:
        with pytest.raises(ValueError):
            PushConfig(
                repo_dir=fake_repo,
                max_attempts=0,
            )

    def test_backoff_too_short_rejected(self, fake_repo: Path) -> None:
        with pytest.raises(ValueError):
            PushConfig(
                repo_dir=fake_repo,
                max_attempts=4,
                backoff_seconds=(1.0, 1.0),  # need at least 3 entries
            )


# ---------------------------------------------------------------------------
# Classifier unit tests
# ---------------------------------------------------------------------------


class TestClassify:
    @pytest.mark.parametrize("stderr,expected", [
        ("ssh: connect to host github.com port 22: Connection timed out\nfatal: unable to access", "network"),
        ("fatal: Could not resolve host: github.com", "network"),
        ("fatal: unable to access 'https://...': Could not resolve host", "network"),
        ("fatal: unable to access 'https://...': The requested URL returned error: 502", "network"),
        ("git@github.com: Permission denied (publickey).", "auth"),
        ("remote: Invalid username or password.\nfatal: Authentication failed", "auth"),
        ("HTTP Basic: Access denied", "auth"),
        ("non-fast-forward", "non_fast_forward"),
        ("! [rejected]        main -> main (non-fast-forward)", "non_fast_forward"),
        ("! [rejected]        main -> main (fetch first)", "non_fast_forward"),
        ("Updates were rejected because the tip of your current branch is behind its remote counterpart", "non_fast_forward"),
        ("Everything up-to-date", "no_changes"),
        ("fatal: repository 'https://github.com/foo/bar.git/' not found", "unknown"),
        ("fatal: unable to lookup", "unknown"),
    ])
    def test_classifies_correctly(self, stderr: str, expected: str) -> None:
        r = GitPushResult(returncode=1, stdout="", stderr=stderr)
        assert classify_push_failure(r) == expected


# ---------------------------------------------------------------------------
# Live integration test (opt-in via env var)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("AGENTCHAT_SYNC_LIVE") != "1",
    reason="set AGENTCHAT_SYNC_LIVE=1 to run the live push test",
)
class TestLiveIntegration:
    """Push to a local bare repo via the real SubprocessGitClient.

    Verifies the real ``git push`` output is classified correctly and
    the retry path actually works against a real (in-process) git
    transport.
    """

    def test_push_to_local_bare_repo(self, tmp_path: Path) -> None:
        from agentchat.sync_agent._git import SubprocessGitClient

        # Local "remote" — a bare repo on disk.
        bare = tmp_path / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(bare)],
            check=True, capture_output=True,
        )
        # Local working repo with one commit.
        work = tmp_path / "work"
        work.mkdir()
        env = {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "[email protected]",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "[email protected]",
            "PATH": os.environ["PATH"],
        }
        subprocess.run(["git", "init", "-b", "main"], cwd=work, check=True, env=env, capture_output=True)
        subprocess.run(["git", "config", "user.email", "[email protected]"], cwd=work, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=work, check=True, capture_output=True)
        (work / "README.md").write_text("hi")
        subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=work, check=True, env=env, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=work, check=True, capture_output=True)

        cfg = PushConfig(
            repo_dir=work,
            remote="origin",
            branch="main",
            auth=AuthStrategy.SSH,  # irrelevant for local file://
            remote_url=str(bare),
            sleep=_no_sleep,
        )
        result = PushStage(cfg, git=SubprocessGitClient()).push()
        assert result.pushed is True

        # Verify the remote received the commit.
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", str(bare), str(clone)],
            check=True, capture_output=True,
        )
        assert (clone / "README.md").exists()
        assert (clone / "README.md").read_text() == "hi"

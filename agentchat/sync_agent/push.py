"""
agentchat v1.2 — Sync agent push stage (v1.2.0.dev20).

This is the **second half** of the GitHub sync pipeline (see
``docs/design/dev20/sync-agent.md``). The change-detection + local
commit stage (sibling module: ``sync_agent.commit``) hands off a
working tree with HEAD on the branch we want to push. This module
takes it from there.

Responsibilities
================

1. **Auth.** Resolve the configured auth strategy (``ssh`` / ``pat`` /
   ``github_app``) and prepare the credentials for the ``git push``
   invocation. SSH is the default (per the design — no env-var
   secrets); PAT and GitHub App are supported for alternative hosts.
2. **Push.** Call ``git push <remote> <branch>`` via a pluggable
   ``GitClient`` (production: ``SubprocessGitClient``; tests can
   inject a stub).
3. **Retry.** Up to 3 attempts on transient network errors with
   exponential backoff (1s, 5s, 30s — chosen to match the design's
   failure-mode table).
4. **Classify.** Map ``git push`` failures to typed exceptions:
   ``AuthError`` (permanent), ``NetworkError`` (transient),
   ``NonFastForwardError`` (caller decides), ``NoChangesError``,
   ``PushError`` (catch-all).

What this module does NOT do
============================

* **Detection** — listening for file changes / commits. That's the
  sibling commit stage.
* **Daemon mode** — out of scope for dev20 (one-shot CLI / library
  function only).
* **Force-push** — explicitly forbidden by the design. A non-fast-forward
  failure surfaces to the caller; the caller can fetch + rebase if it
  wants.
* **The mirror-tree scratch flow** — the legacy ``sync_github.py``
  one-shot CLI builds a temp mirror tree and pushes from it. This
  module pushes from the working tree directly; the two flows are
  separate paths and the orchestrator chooses which to use.

Public API
==========

* :class:`PushConfig` — dataclass with auth strategy, remote, branch,
  retry policy.
* :class:`PushStage` — the orchestrator. ``PushStage(config).push()``
  runs the full flow.
* :func:`push_committed` — convenience wrapper for the one-shot case.
* :class:`AuthStrategy` — enum: ``SSH``, ``PAT``, ``GITHUB_APP``.

Example
=======

::

    from agentchat.sync_agent.push import PushConfig, PushStage, AuthStrategy
    from agentchat.sync_agent._git import SubprocessGitClient

    config = PushConfig(
        repo_dir=Path("/home/waynec/agentchat"),
        remote="origin",
        branch="main",
        auth=AuthStrategy.SSH,
        remote_url="git@github.com-agentchat:wayne-comerford/agentchat.git",
    )
    stage = PushStage(config, git=SubprocessGitClient())
    result = stage.push()  # raises typed errors on failure
    print(result.commit_sha, result.attempts)
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

from ._git import GitClient, GitPushResult, SubprocessGitClient, classify_push_failure
from .errors import (
    AuthError,
    MissingRepoError,
    NetworkError,
    NoChangesError,
    NonFastForwardError,
    PushError,
)

log = logging.getLogger("agentchat.sync_agent.push")


class AuthStrategy(str, enum.Enum):
    """How the push authenticates to the remote."""

    SSH = "ssh"
    PAT = "pat"
    GITHUB_APP = "github_app"


# Env vars read for credential material. Each is read **once** at
# PushConfig construction time and not retained on the dataclass
# (the resolved value is what gets passed to git). The push stage
# never logs these values.
_PAT_ENV_VAR = "AGENTCHAT_GITHUB_PAT"
_APP_ID_ENV_VAR = "AGENTCHAT_GITHUB_APP_ID"
_APP_INSTALLATION_ENV_VAR = "AGENTCHAT_GITHUB_APP_INSTALLATION_ID"
_APP_PRIVATE_KEY_ENV_VAR = "AGENTCHAT_GITHUB_APP_PRIVATE_KEY"  # PEM contents


# Default retry schedule: 3 attempts, backoff between them.
# Tuned so the worst-case total wait is ~36s — short enough to feel
# snappy for a polling-driven sync, long enough to ride out a 30s
# transient network outage.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 5.0, 30.0)


@dataclasses.dataclass(frozen=True)
class PushResult:
    """The successful outcome of a push stage run."""

    remote: str
    branch: str
    commit_sha: str
    attempts: int
    pushed: bool = True
    bytes_uploaded: int = 0  # best-effort; only filled by callers who parse it


# ---------------------------------------------------------------------------
# PushConfig
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PushConfig:
    """All the knobs the push stage needs.

    The ``auth`` strategy determines where credentials come from. The
    other fields (``remote``, ``branch``, ``remote_url``) are the
    resolved push target — the operator-side setup (deploy key
    install + ``~/.ssh/config`` alias) is documented in
    ``docs/design/dev20/sync-agent.md`` §3.

    ``max_attempts`` defaults to 3 — the design's failure-mode table
    caps retries at 3; going higher risks amplifying a long outage
    into a long backoff.
    """

    repo_dir: Path
    remote: str = "origin"
    branch: str = "main"
    auth: AuthStrategy = AuthStrategy.SSH
    # ``remote_url`` is the *resolved* URL the push will use. When
    # ``auth == PAT``, the token is interpolated into this URL just
    # before the call. When ``auth == SSH``, this is the SSH URL
    # (e.g. ``git@github.com-agentchat:owner/repo.git``) and the
    # ssh-agent provides the key.
    remote_url: Optional[str] = None
    # Overrides; if unset we pull from the appropriate env var.
    pat_token: Optional[str] = None
    app_id: Optional[str] = None
    app_installation_id: Optional[str] = None
    app_private_key: Optional[str] = None

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_seconds: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS
    # When True, the push stage mutates the remote's URL on the
    # local repo before the first push (PAT/GitHub App flows need
    # the token in the URL). When False, the URL is rewritten via
    # ``git -c url.<url>.pushInsteadOf=...`` instead. Default True
    # because it's the simpler path and the local repo is a one-off
    # for dev20 (no other remotes to worry about).
    rewrite_remote_url: bool = True

    # When not None, the push stage calls ``sleep(attempt)`` between
    # attempts instead of ``time.sleep(backoff_seconds[attempt-1])``.
    # Tests use this to keep the suite fast.
    sleep: Optional[Callable[[int], None]] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_dir", Path(self.repo_dir).expanduser().resolve())
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts}")
        if len(self.backoff_seconds) < self.max_attempts - 1:
            raise ValueError(
                f"backoff_seconds has {len(self.backoff_seconds)} entries; "
                f"need at least max_attempts-1 = {self.max_attempts - 1}"
            )

    # --- credential resolution -----------------------------------------

    def resolve_pat(self) -> str:
        """Return the PAT to use, or raise ``AuthError`` if missing."""
        token = self.pat_token or os.environ.get(_PAT_ENV_VAR)
        if not token:
            raise AuthError(
                "PAT auth requested but neither PushConfig.pat_token nor "
                f"${_PAT_ENV_VAR} is set",
                remote=self.remote,
                branch=self.branch,
            )
        return token

    def resolve_github_app(self) -> tuple[str, str, str]:
        """Return ``(app_id, installation_id, private_key_pem)`` for GitHub App auth.

        GitHub App auth in dev20 is **deferred** — this method returns
        the inputs the caller would need to mint an installation token,
        but the push stage doesn't implement the mint flow itself (that
        needs ``PyJWT`` / ``cryptography`` which would break the
        stdlib-only constraint). The orchestrator will handle it.
        """
        app_id = self.app_id or os.environ.get(_APP_ID_ENV_VAR)
        installation_id = self.app_installation_id or os.environ.get(_APP_INSTALLATION_ENV_VAR)
        key = self.app_private_key or os.environ.get(_APP_PRIVATE_KEY_ENV_VAR)
        if not (app_id and installation_id and key):
            raise AuthError(
                "GitHub App auth requested but app_id / installation_id / "
                "private_key are not all available (set PushConfig fields or "
                f"${_APP_ID_ENV_VAR} / ${_APP_INSTALLATION_ENV_VAR} / "
                f"${_APP_PRIVATE_KEY_ENV_VAR})",
                remote=self.remote,
                branch=self.branch,
            )
        return app_id, installation_id, key

    # --- URL composition -----------------------------------------------

    def effective_remote_url(self) -> str:
        """Return the URL the push will actually use.

        For SSH and GitHub App flows, returns ``remote_url`` as-is
        (the orchestrator rewrites the URL with the minted token for
        the GitHub App case).
        For PAT flows, interpolates the token into the HTTPS URL.
        """
        if not self.remote_url:
            raise AuthError(
                f"PushConfig.remote_url is required for auth={self.auth.value}",
                remote=self.remote,
                branch=self.branch,
            )
        if self.auth == AuthStrategy.PAT:
            token = self.resolve_pat()
            return _inject_pat(self.remote_url, token)
        return self.remote_url


def _inject_pat(url: str, token: str) -> str:
    """Inject a PAT into an HTTPS URL.

    Handles the three common shapes:

    * ``https://github.com/owner/repo.git``             -> HTTPS w/ token
    * ``https://[email protected]/owner/repo.git``    -> HTTPS w/ token (replaces existing userinfo)
    * ``git@...`` or ``ssh://...``                     -> unchanged (SSH)

    Raises ``AuthError`` if the URL is not HTTPS.
    """
    if url.startswith("git@") or url.startswith("ssh://"):
        raise AuthError(
            f"Cannot inject PAT into non-HTTPS URL: {url!r}",
            remote="<unknown>",
            branch="<unknown>",
        )
    if not url.startswith("https://"):
        raise AuthError(
            f"PAT auth requires an https:// URL, got: {url!r}",
            remote="<unknown>",
            branch="<unknown>",
        )
    # Strip any existing userinfo: "https://user:[email protected]/path" -> "/path"
    _, _, after_scheme = url.partition("https://")
    if "@" in after_scheme.split("/", 1)[0]:
        after_scheme = after_scheme.split("@", 1)[1]
    authed = "https:" + "//" + "x-access-token:" + token + "@" + after_scheme
    return authed


# ---------------------------------------------------------------------------
# PushStage
# ---------------------------------------------------------------------------


class PushStage:
    """The push stage. Construct with a config, call ``push()``.

    A minimal ``GitClient`` is injected for testability. The default
    ``SubprocessGitClient`` shells out to a real ``git`` binary.
    """

    def __init__(
        self,
        config: PushConfig,
        git: Optional[GitClient] = None,
    ) -> None:
        self.config = config
        self.git = git or SubprocessGitClient()

    # ------------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------------

    def push(self) -> PushResult:
        """Push the current HEAD to ``config.remote/config.branch``.

        Returns a :class:`PushResult` on success. Raises a typed
        exception on failure:

        * ``MissingRepoError`` — local repo doesn't exist.
        * ``AuthError`` — credential problem; not retried.
        * ``NonFastForwardError`` — remote has commits local doesn't;
          not retried; caller decides what to do (fetch + rebase).
        * ``NetworkError`` — transient; retried up to ``max_attempts``.
        * ``PushError`` — catch-all after retries are exhausted.
        """
        cfg = self.config
        if not cfg.repo_dir.exists():
            raise MissingRepoError(
                f"repo_dir does not exist: {cfg.repo_dir}",
                remote=cfg.remote,
                branch=cfg.branch,
            )
        # Sanity check: is this actually a git working tree?
        try:
            head_sha = self.git.rev_parse(repo_dir=cfg.repo_dir, ref="HEAD")
        except RuntimeError as e:
            raise MissingRepoError(
                f"repo_dir is not a git working tree: {cfg.repo_dir} ({e})",
                remote=cfg.remote,
                branch=cfg.branch,
            ) from e

        # Resolve the URL we'll use, validate the auth strategy.
        effective_url = self._prepare_url()

        # If the caller wants the URL rewritten on the local repo,
        # do it before the first push. For PAT, this is the only way
        # the token reaches the remote (HTTP basic auth in the URL).
        push_url = None
        if cfg.rewrite_remote_url and cfg.auth == AuthStrategy.PAT:
            push_url = effective_url

        last_exc: Optional[PushError] = None
        for attempt in range(1, cfg.max_attempts + 1):
            try:
                result = self.git.push(
                    repo_dir=cfg.repo_dir,
                    remote=cfg.remote,
                    branch=cfg.branch,
                    remote_url=push_url,
                )
            except Exception as e:
                # SubprocessGitClient can raise on transient infra
                # issues (timeout, OSError). Treat as a network error.
                log.warning(
                    "git push raised (attempt %d/%d): %r",
                    attempt, cfg.max_attempts, e,
                )
                last_exc = NetworkError(
                    f"git push raised {type(e).__name__}: {e}",
                    remote=cfg.remote,
                    branch=cfg.branch,
                    attempts=attempt,
                )
                if attempt < cfg.max_attempts:
                    self._sleep_between(attempt)
                    continue
                raise last_exc from e

            if result.ok:
                # A successful push that reported nothing to do is still
                # a successful no-op — but the caller wants to know it
                # so they can avoid appending to the audit log. Signal
                # it via NoChangesError (typed, distinct from success).
                if _looks_like_no_changes(result):
                    raise NoChangesError(
                        "git push reported everything up-to-date",
                        remote=cfg.remote,
                        branch=cfg.branch,
                        stdout=result.stdout,
                        stderr=result.stderr,
                        attempts=attempt,
                    )
                return PushResult(
                    remote=cfg.remote,
                    branch=cfg.branch,
                    commit_sha=head_sha,
                    attempts=attempt,
                )

            kind = classify_push_failure(result)
            last_stderr_line = (
                (result.stderr or "").strip().splitlines()[-1]
                if result.stderr else ""
            )
            log.info(
                "git push failed (attempt %d/%d, kind=%s, rc=%d, stderr=%r)",
                attempt, cfg.max_attempts, kind, result.returncode, last_stderr_line,
            )

            if kind == "auth":
                # Permanent — do not retry.
                raise AuthError(
                    f"git push auth failed: {result.stderr.strip() or 'unknown cause'}",
                    remote=cfg.remote,
                    branch=cfg.branch,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    attempts=attempt,
                )
            if kind == "non_fast_forward":
                raise NonFastForwardError(
                    f"git push rejected as non-fast-forward: {result.stderr.strip()}",
                    remote=cfg.remote,
                    branch=cfg.branch,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    attempts=attempt,
                )
            if kind == "no_changes":
                # Strange: we polled HEAD and decided there's something
                # to push, but git says nothing to do. Surface as a
                # typed error so the caller can log it without alerting.
                raise NoChangesError(
                    "git push reported everything up-to-date",
                    remote=cfg.remote,
                    branch=cfg.branch,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    attempts=attempt,
                )
            if kind == "network":
                # Transient — retry.
                last_exc = NetworkError(
                    f"git push network error: {result.stderr.strip() or 'unknown cause'}",
                    remote=cfg.remote,
                    branch=cfg.branch,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    attempts=attempt,
                )
                if attempt < cfg.max_attempts:
                    self._sleep_between(attempt)
                    continue
                raise last_exc

            # Unknown — treat as a generic push error. No retry: unknown
            # errors are usually operator-side (broken remote URL, bad
            # repo, etc.) and retrying just amplifies the noise.
            raise PushError(
                f"git push failed (rc={result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip() or 'unknown cause'}",
                remote=cfg.remote,
                branch=cfg.branch,
                stdout=result.stdout,
                stderr=result.stderr,
                attempts=attempt,
            )

        # Defensive: loop returns or raises; this is unreachable.
        raise last_exc or PushError(
            "git push failed after exhausting retries",
            remote=cfg.remote,
            branch=cfg.branch,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _prepare_url(self) -> str:
        """Validate the auth strategy and return the URL ``git push`` will use.

        For PAT, the URL has the token interpolated. For SSH, the URL
        is the ``remote_url`` as-is (ssh-agent handles auth). For
        GitHub App, the design defers the mint flow to the caller;
        we raise ``AuthError`` if the push is attempted with app auth
        but no minted token was supplied.
        """
        cfg = self.config
        if cfg.auth == AuthStrategy.GITHUB_APP:
            # Defer to the caller: the orchestrator (t_0105ff20) is
            # expected to mint an installation token and supply it
            # via a rewritten remote_url. We just sanity-check that
            # remote_url is set.
            if not cfg.remote_url:
                raise AuthError(
                    "GitHub App auth requires remote_url to be set "
                    "(the orchestrator should mint an installation "
                    "token and rewrite the remote URL before calling push)",
                    remote=cfg.remote,
                    branch=cfg.branch,
                )
        return cfg.effective_remote_url()

    def _sleep_between(self, attempt: int) -> None:
        """Sleep for the configured backoff between attempts."""
        cfg = self.config
        if cfg.sleep is not None:
            cfg.sleep(attempt)
            return
        # backoff_seconds[i] is the delay AFTER attempt i (1-indexed).
        idx = min(attempt - 1, len(cfg.backoff_seconds) - 1)
        delay = cfg.backoff_seconds[idx]
        time.sleep(delay)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_NO_CHANGES_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Everything up-to-date", re.IGNORECASE),
)


def _looks_like_no_changes(result: GitPushResult) -> bool:
    """Return True if a successful push result says nothing changed.

    Mirrors the classifier in ``_git.py`` but operates on a *successful*
    result. Used to raise :class:`NoChangesError` so the caller can
    distinguish a true no-op from a successful push.
    """
    haystack = (result.stdout or "") + "\n" + (result.stderr or "")
    for pat in _NO_CHANGES_PATTERNS:
        if pat.search(haystack):
            return True
    return False


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------


def push_committed(
    repo_dir: Path,
    *,
    remote: str = "origin",
    branch: str = "main",
    auth: AuthStrategy = AuthStrategy.SSH,
    remote_url: Optional[str] = None,
    git: Optional[GitClient] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> PushResult:
    """One-shot helper: push the current HEAD of ``repo_dir`` and return.

    Example::

        from agentchat.sync_agent.push import push_committed, AuthStrategy
        result = push_committed(
            repo_dir=Path("/home/waynec/agentchat"),
            remote="origin",
            branch="main",
            auth=AuthStrategy.SSH,
            remote_url="git@github.com-agentchat:wayne-comerford/agentchat.git",
        )
    """
    cfg = PushConfig(
        repo_dir=repo_dir,
        remote=remote,
        branch=branch,
        auth=auth,
        remote_url=remote_url,
        max_attempts=max_attempts,
    )
    return PushStage(cfg, git=git).push()

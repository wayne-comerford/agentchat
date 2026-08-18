"""
GitClient abstraction used by the push stage.

The push stage needs to shell out to ``git push``. The natural seam
is a thin wrapper around ``subprocess.run`` that returns a typed
result, so the unit tests can inject a stub (verifying retry / error
mapping) without monkeypatching subprocess.

Two implementations are provided:

* :class:`SubprocessGitClient` — production. Shells out to a real
  ``git`` binary; configurable binary path for tests.
* :class:`GitClient` (the Protocol) — the contract. Any object with
  a ``push(...)`` method returning a :class:`GitPushResult` fits.

The push stage classifies failures by inspecting the stderr text.
The patterns below are intentionally conservative: they match the
**canonical** git error messages on Linux/macOS (and the
`git-for-windows` mingw shim). A new git version that changes its
error text would surface as a generic non-zero exit and the push
stage would raise a :class:`PushError` (the safe fallback).
"""

from __future__ import annotations

import abc
import dataclasses
import os
import re
import subprocess
from pathlib import Path
from typing import Optional


@dataclasses.dataclass(frozen=True)
class GitPushResult:
    """The trimmed output of a single ``git push`` invocation."""

    returncode: int
    stdout: str
    stderr: str
    # Some git transports return non-zero with "Everything up-to-date"
    # on stderr. Stdout is the reliable signal. We capture both.
    args: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class GitClient(abc.ABC):
    """The contract the push stage depends on.

    Implemented as an ABC (rather than a Protocol) so unit tests can
    subclass it with a stubbed ``push()`` and Pyright is happy with the
    return types. The push stage only cares about the four methods
    below; anything else is implementation detail.
    """

    @abc.abstractmethod
    def push(
        self,
        *,
        repo_dir: Path,
        remote: str,
        branch: str,
        remote_url: Optional[str],
        env: Optional[dict[str, str]] = None,
    ) -> GitPushResult:
        """Push ``branch`` to ``remote``.

        If ``remote_url`` is provided, the remote's URL should be set
        to that value for this invocation (the push stage rewrites
        the URL when using a PAT to inject the token).
        """

    @abc.abstractmethod
    def remote_get_url(self, *, repo_dir: Path, remote: str) -> str:
        """Return the configured URL for *remote* (for logging/debug)."""

    @abc.abstractmethod
    def rev_parse(self, *, repo_dir: Path, ref: str) -> str:
        """Return the SHA that *ref* resolves to. Used for the audit log."""


# ---------------------------------------------------------------------------
# Classifiers — match the textual signal from git's stderr.
# ---------------------------------------------------------------------------
# Order matters: more specific patterns first. AuthError can sometimes be
# triggered by a network failure (e.g. "Connection closed by authenticating
# host"); we prefer to surface that as AuthError because the deploy key
# probe gets the same response regardless of the underlying cause.

_AUTH_PATTERNS: tuple[re.Pattern[str], ...] = (
    # SSH
    re.compile(r"Permission denied \(publickey\)", re.IGNORECASE),
    re.compile(r"no mutual signature supported", re.IGNORECASE),
    re.compile(r"host key verification failed", re.IGNORECASE),
    re.compile(r"REMOTE HOST IDENTIFICATION HAS CHANGED", re.IGNORECASE),
    re.compile(r"Error in authentication", re.IGNORECASE),
    # HTTPS / PAT
    re.compile(r"could not read (?:Username|Password) for", re.IGNORECASE),
    re.compile(r"authentication failed", re.IGNORECASE),
    re.compile(r"HTTP Basic: Access denied", re.IGNORECASE),
    re.compile(r"bad credentials", re.IGNORECASE),
    # Permission (deploy key without write access)
    re.compile(r"denied to .+", re.IGNORECASE),
    re.compile(r"refusing to update checked out branch", re.IGNORECASE),
    # Author identity rejected (often a side-effect of a misconfigured PAT)
    re.compile(r"invalid authentication", re.IGNORECASE),
)

_NETWORK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Could not resolve host", re.IGNORECASE),
    re.compile(r"connection (?:refused|reset|closed|timed out)", re.IGNORECASE),
    re.compile(r"network is unreachable", re.IGNORECASE),
    re.compile(r"Operation timed out", re.IGNORECASE),
    re.compile(r"ssh: connect to host .+ port .+: (?:Connection refused|Operation timed out)", re.IGNORECASE),
    re.compile(r"fatal: unable to access", re.IGNORECASE),
    re.compile(r"RPC failed", re.IGNORECASE),
    re.compile(r"The requested URL returned error: 5\d\d", re.IGNORECASE),
    # TLS / cert glitches — typically transient
    re.compile(r"SSL connection (?:error|timeout)", re.IGNORECASE),
    re.compile(r"gnutls_handshake", re.IGNORECASE),
)

_NONFASTFORWARD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"non-fast-forward", re.IGNORECASE),
    re.compile(r"rejected because the remote contains work that you do not have", re.IGNORECASE),
    re.compile(r"stale info"),  # can accompany non-fast-forward
    re.compile(r"fetch first", re.IGNORECASE),
    # "Updates were rejected because the tip of your current branch is behind"
    re.compile(r"Updates were rejected.*behind", re.IGNORECASE | re.DOTALL),
)

_NOCHANGES_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Everything up-to-date", re.IGNORECASE),
)


def classify_push_failure(result: GitPushResult) -> str:
    """Return one of: 'auth', 'network', 'non_fast_forward', 'no_changes', 'unknown'.

    Inspects stderr primarily (where git writes human-readable errors),
    falling back to stdout if stderr is empty.
    """
    haystack = (result.stderr or "") + "\n" + (result.stdout or "")
    if not haystack.strip():
        return "unknown"
    # Order: auth first (more specific), then network, then non-fast-forward,
    # then no-changes (this is actually a success signal but git returns 0
    # so we shouldn't end up here for it — defensive only).
    for pat in _AUTH_PATTERNS:
        if pat.search(haystack):
            return "auth"
    for pat in _NONFASTFORWARD_PATTERNS:
        if pat.search(haystack):
            return "non_fast_forward"
    for pat in _NETWORK_PATTERNS:
        if pat.search(haystack):
            return "network"
    for pat in _NOCHANGES_PATTERNS:
        if pat.search(haystack):
            return "no_changes"
    return "unknown"


# ---------------------------------------------------------------------------
# Real implementation
# ---------------------------------------------------------------------------


class SubprocessGitClient:
    """Production GitClient. Shells out to the configured ``git`` binary."""

    def __init__(self, git_binary: str = "git", timeout: float = 60.0) -> None:
        self.git_binary = git_binary
        self.timeout = timeout

    def push(
        self,
        *,
        repo_dir: Path,
        remote: str,
        branch: str,
        remote_url: Optional[str],
        env: Optional[dict[str, str]] = None,
    ) -> GitPushResult:
        # If remote_url is provided, override the remote's URL **for this
        # invocation only** via ``git -c url.<url>.insteadOf=...`` won't
        # work — we rewrite the URL on the remote. We prefer setting
        # the URL on the remote ahead of the call (see push.py).
        args = [self.git_binary, "-C", str(repo_dir), "push", remote, branch]
        merged_env: Optional[dict[str, str]] = None
        if env:
            # Inherit the parent env, layer caller-supplied on top.
            merged_env = os.environ.copy()
            merged_env.update(env)
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            env=merged_env,
            check=False,
        )
        return GitPushResult(
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            args=tuple(args),
        )

    def remote_get_url(self, *, repo_dir: Path, remote: str) -> str:
        proc = subprocess.run(
            [self.git_binary, "-C", str(repo_dir), "remote", "get-url", remote],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git remote get-url {remote} failed (rc={proc.returncode}): {proc.stderr}"
            )
        return proc.stdout.strip()

    def rev_parse(self, *, repo_dir: Path, ref: str) -> str:
        proc = subprocess.run(
            [self.git_binary, "-C", str(repo_dir), "rev-parse", ref],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git rev-parse {ref} failed (rc={proc.returncode}): {proc.stderr}"
            )
        return proc.stdout.strip()

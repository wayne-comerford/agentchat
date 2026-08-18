"""
Typed error hierarchy for the sync_agent push stage.

The push stage distinguishes between **transient** failures (worth retrying)
and **permanent** failures (operator action required). The caller (the
orchestrator in t_0105ff20) decides what to do for each:

* ``NetworkError`` — transient; retry with backoff is the default.
* ``AuthError`` — permanent; push stage should stop and alert.
* ``NonFastForwardError`` — permanent; the caller can choose to fetch +
  rebase, or surface to the operator.
* ``PushError`` — generic catch-all surfaced after retries are exhausted.

Each carries ``stdout`` and ``stderr`` from the underlying ``git push``
invocation so the caller can log them without re-running git.
"""

from __future__ import annotations

from pathlib import Path


class PushError(Exception):
    """Base class for all push stage errors."""

    def __init__(
        self,
        message: str,
        *,
        remote: str,
        branch: str,
        stdout: str = "",
        stderr: str = "",
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.remote = remote
        self.branch = branch
        self.stdout = stdout
        self.stderr = stderr
        self.attempts = attempts

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = super().__str__()
        ctx = f"remote={self.remote!r} branch={self.branch!r} attempts={self.attempts}"
        if self.stderr:
            # Trim to last ~400 chars to keep error messages readable.
            tail = self.stderr.strip().splitlines()[-6:]
            ctx += "\n--- git stderr (tail) ---\n" + "\n".join(tail)
        return f"{base} ({ctx})"


class NetworkError(PushError):
    """Transient network / transport failure.

    Raised when git push fails with a connection-level error (DNS,
    timeout, refused connection, broken pipe). The push stage retries
    these by default with exponential backoff.

    Note: SSH key exchange errors that look like "Permission denied
    (publickey)" are *not* a NetworkError — they are AuthError.
    """


class AuthError(PushError):
    """Permanent authentication / authorisation failure.

    Raised when git push fails with a credential problem the operator
    must fix (revoked deploy key, expired PAT, missing write access on
    the repo). The push stage does NOT retry — see the design's
    failure-mode table (t_08bd1def §5).
    """


class NonFastForwardError(PushError):
    """The remote has commits the local repo does not.

    The push stage does NOT retry — the caller can choose to fetch +
    rebase, or surface to the operator. The design explicitly forbids
    force-push.
    """


class NoChangesError(PushError):
    """There is nothing to push (HEAD already matches the remote).

    Distinct from a successful no-op so the caller can avoid appending
    to the audit log.
    """


class MissingRepoError(PushError):
    """The local repo_dir does not exist or is not a git working tree."""

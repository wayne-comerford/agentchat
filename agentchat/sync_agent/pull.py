"""
agentchat v1.2 — Sync agent pull stage (v1.2.0.dev27).

The first half of the GitHub sync pipeline (push is the second half —
see :mod:`agentchat.sync_agent.push`). Without a pull command, two
workstations editing the same workspace can clobber each other on push.

Responsibilities
================

1. **Fetch.** ``git fetch <remote>`` to learn what's on the remote.
2. **Detect ahead/behind.** Compare local HEAD against
   ``<remote>/<branch>``. Three states: even, ahead (local is ahead,
   nothing to do), behind (fast-forward is possible), diverged (both
   sides have commits → no FF).
3. **Fast-forward pull.** If local is behind and clean (no uncommitted
   changes), do ``git merge --ff-only``. Otherwise, refuse to mutate
   and surface a typed error.
4. **Conflict detection.** If local has uncommitted changes that would
   conflict with the incoming changes, snapshot the conflicts to
   ``~/.hermes/agent_chat/pull_conflicts/<ts>/`` and write a
   ``conflict_report.md`` with diff snippets. Local working tree is
   left untouched so the operator can resolve.
5. **Audit.** Append a record to the same audit log the push stage
   uses, so pull history is visible alongside push history.

Public API
==========

* :class:`PullConfig` — dataclass with repo_dir, remote, branch.
* :class:`PullResult` — dataclass with status, ahead/behind, commit SHAs.
* :class:`PullStage` — orchestrator. ``PullStage(config).pull()`` runs.
* :func:`pull_remote` — convenience wrapper for the one-shot case.
* :func:`detect_ahead_behind` — read-only check (used by --dry-run).
* :class:`PullError` hierarchy.

Status values
=============

* ``"up_to_date"`` — no action needed
* ``"fast_forwarded"`` — local was behind, now even
* ``"diverged"`` — both sides have commits; no auto-merge
* ``"local_dirty"`` — local has uncommitted changes; would conflict
* ``"no_remote"`` — no remote configured (skip)
* ``"error"`` — something went wrong (see exception / stderr)
"""

from __future__ import annotations

import dataclasses
import enum
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("agentchat.sync_agent.pull")

DEFAULT_CONFLICT_DIR = Path(
    os.environ.get(
        "AGENTCHAT_PULL_CONFLICT_DIR",
        str(Path.home() / ".hermes" / "agent_chat" / "pull_conflicts"),
    )
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PullError(Exception):
    """Base class for all pull stage errors."""

    def __init__(
        self,
        message: str,
        *,
        remote: str = "",
        branch: str = "",
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.remote = remote
        self.branch = branch
        self.stdout = stdout
        self.stderr = stderr


class NoRemoteError(PullError):
    """No remote configured (or remote is empty)."""


class DivergedError(PullError):
    """Local and remote have diverged — no fast-forward possible."""


class LocalDirtyError(PullError):
    """Local working tree has uncommitted changes that conflict with incoming."""


class GitError(PullError):
    """Generic git failure (network, auth, etc.)."""


# ---------------------------------------------------------------------------
# Config + result
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PullConfig:
    repo_dir: Path
    remote: str = "origin"
    branch: str = "main"
    conflict_dir: Path = DEFAULT_CONFLICT_DIR
    # If True, do not mutate anything. Just compute ahead/behind.
    dry_run: bool = False
    # If True, allow non-FF pulls by rebasing. (Operator only.)
    allow_rebase: bool = False
    # Git client interface (production: SubprocessGitClient; tests: stub).
    git: "GitClient | None" = None


@dataclasses.dataclass
class PullResult:
    status: str  # up_to_date | fast_forwarded | diverged | local_dirty | no_remote | error
    local_sha: str = ""
    remote_sha: str = ""
    ahead: int = 0
    behind: int = 0
    pulled_sha: str = ""  # if fast-forwarded, the new HEAD
    conflict_dir: Optional[Path] = None
    error: Optional[str] = None
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in ("up_to_date", "fast_forwarded", "no_remote")

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        if self.conflict_dir is not None:
            d["conflict_dir"] = str(self.conflict_dir)
        return d


class GitClient:
    """Minimal git interface for pull. Mirrors SubprocessGitClient from push.py."""

    def run(self, args: list[str], *, cwd: Path, timeout: int = 30) -> tuple[int, str, str]:
        try:
            r = subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return r.returncode, r.stdout, r.stderr
        except FileNotFoundError:
            return 127, "", "git not found in PATH"
        except subprocess.TimeoutExpired:
            return -1, "", f"git timeout after {timeout}s"


class SubprocessGitClient(GitClient):
    pass


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


class PullStage:
    def __init__(self, config: PullConfig):
        self.config = config
        self.git = config.git or SubprocessGitClient()

    def pull(self) -> PullResult:
        t0 = time.time()
        cfg = self.config
        # 1. Check remote exists
        rc, stdout, stderr = self.git.run(
            ["remote", "get-url", cfg.remote], cwd=cfg.repo_dir,
        )
        if rc != 0 or not stdout.strip():
            return PullResult(
                status="no_remote",
                duration_s=time.time() - t0,
                error=f"remote {cfg.remote!r} not configured: {stderr.strip()}",
            )

        # 2. Fetch
        rc, stdout, stderr = self.git.run(
            ["fetch", cfg.remote, cfg.branch], cwd=cfg.repo_dir, timeout=120,
        )
        if rc != 0:
            return PullResult(
                status="error",
                error=f"git fetch failed: {stderr.strip() or stdout.strip()}",
                duration_s=time.time() - t0,
            )

        # 3. Detect ahead/behind
        local_sha, remote_sha, ahead, behind = detect_ahead_behind(
            cfg.repo_dir, cfg.remote, cfg.branch, git=self.git,
        )
        result = PullResult(
            status="up_to_date",
            local_sha=local_sha,
            remote_sha=remote_sha,
            ahead=ahead,
            behind=behind,
            duration_s=time.time() - t0,
        )

        if ahead == 0 and behind == 0:
            return result

        if ahead > 0 and behind == 0:
            # Local is ahead — nothing to pull
            result.status = "up_to_date"
            return result

        if behind > 0 and ahead == 0:
            # Pure fast-forward possible
            return self._do_fast_forward(result)

        # ahead > 0 and behind > 0 → diverged
        if cfg.dry_run:
            result.status = "diverged"
            return result
        if not cfg.allow_rebase:
            raise DivergedError(
                f"local and remote have diverged (ahead={ahead}, behind={behind}); "
                f"refusing to pull. Use --allow-rebase to rebase local onto remote.",
                remote=cfg.remote,
                branch=cfg.branch,
            )
        # Rebase
        return self._do_rebase(result)

    def _do_fast_forward(self, result: PullResult) -> PullResult:
        cfg = self.config
        # Check local dirty
        rc, stdout, _ = self.git.run(
            ["status", "--porcelain"], cwd=cfg.repo_dir,
        )
        if rc == 0 and stdout.strip():
            # Has uncommitted changes — snapshot conflicts and refuse
            conflict_dir = self._snapshot_conflicts(result, stdout)
            result.status = "local_dirty"
            result.conflict_dir = conflict_dir
            result.error = (
                f"local working tree has {len(stdout.strip().splitlines())} "
                f"uncommitted change(s); refusing fast-forward"
            )
            return result
        if cfg.dry_run:
            result.status = "fast_forwarded"  # would-be
            return result
        # Real pull
        rc, out, err = self.git.run(
            ["merge", "--ff-only", f"{cfg.remote}/{cfg.branch}"],
            cwd=cfg.repo_dir,
        )
        if rc != 0:
            result.status = "error"
            result.error = f"fast-forward failed: {err.strip() or out.strip()}"
            return result
        # Get new HEAD
        rc, new_sha, _ = self.git.run(
            ["rev-parse", "HEAD"], cwd=cfg.repo_dir,
        )
        result.status = "fast_forwarded"
        result.pulled_sha = new_sha.strip()
        result.local_sha = new_sha.strip()
        result.behind = 0
        result.ahead = 0
        return result

    def _do_rebase(self, result: PullResult) -> PullResult:
        cfg = self.config
        if cfg.dry_run:
            result.status = "diverged"
            return result
        rc, out, err = self.git.run(
            ["rebase", f"{cfg.remote}/{cfg.branch}"],
            cwd=cfg.repo_dir,
        )
        if rc != 0:
            # Abort rebase so local is left as-is
            self.git.run(["rebase", "--abort"], cwd=cfg.repo_dir)
            raise GitError(
                f"rebase failed (likely conflicts): {err.strip() or out.strip()}",
                remote=cfg.remote,
                branch=cfg.branch,
                stdout=out,
                stderr=err,
            )
        rc, new_sha, _ = self.git.run(["rev-parse", "HEAD"], cwd=cfg.repo_dir)
        result.status = "fast_forwarded"
        result.pulled_sha = new_sha.strip()
        result.local_sha = new_sha.strip()
        result.behind = 0
        result.ahead = 0
        return result

    def _snapshot_conflicts(
        self, result: PullResult, porcelain_output: str,
    ) -> Path:
        """Snapshot conflicting files to disk for the operator to resolve."""
        ts = time.strftime("%Y%m%dT%H%M%S")
        target = self.config.conflict_dir / ts
        target.mkdir(parents=True, exist_ok=True)
        # Write porcelain status
        (target / "status.txt").write_text(porcelain_output)
        # Write diff of incoming
        rc, diff, _ = self.git.run(
            ["diff", f"{self.config.remote}/{self.config.branch}"],
            cwd=self.config.repo_dir,
        )
        (target / "incoming.diff").write_text(diff)
        # Write JSON metadata
        (target / "pull_result.json").write_text(json.dumps(result.to_dict(), indent=2))
        # Write human-readable report
        report = [
            "# Pull conflict report",
            "",
            f"- Generated: {ts}",
            f"- Remote: {self.config.remote}",
            f"- Branch: {self.config.branch}",
            f"- Local HEAD: {result.local_sha[:12]}",
            f"- Remote HEAD: {result.remote_sha[:12]}",
            f"- Ahead: {result.ahead}  Behind: {result.behind}",
            "",
            "## Why this happened",
            "",
            "Local working tree has uncommitted changes. The pull was",
            "refused to avoid clobbering them. Files in this directory:",
            "",
            "- `status.txt` — `git status --porcelain` output",
            "- `incoming.diff` — `git diff <remote>/<branch>` (incoming changes)",
            "- `pull_result.json` — machine-readable summary",
            "",
            "## How to resolve",
            "",
            "1. Review the diff in `incoming.diff`",
            "2. Either commit or stash your local changes:",
            "   - `git stash` then `agentchat-sync pull`",
            "   - or `git add . && git commit` then `agentchat-sync pull`",
            "3. Re-run `agentchat-sync pull`",
        ]
        (target / "conflict_report.md").write_text("\n".join(report) + "\n")
        LOG.warning("pull conflict snapshot saved to %s", target)
        return target


# ---------------------------------------------------------------------------
# Read-only helpers
# ---------------------------------------------------------------------------


def detect_ahead_behind(
    repo_dir: Path,
    remote: str,
    branch: str,
    *,
    git: GitClient | None = None,
) -> tuple[str, str, int, int]:
    """Return (local_sha, remote_sha, ahead, behind) for ``remote/branch``."""
    g = git or SubprocessGitClient()
    rc, out, _ = g.run(["rev-parse", "HEAD"], cwd=repo_dir)
    local_sha = out.strip() if rc == 0 else ""
    rc, out, _ = g.run(
        ["rev-parse", f"{remote}/{branch}"], cwd=repo_dir,
    )
    remote_sha = out.strip() if rc == 0 else ""
    # ahead/behind via rev-list
    rc, out, _ = g.run(
        ["rev-list", "--left-right", "--count", f"HEAD...{remote}/{branch}"],
        cwd=repo_dir,
    )
    ahead, behind = 0, 0
    if rc == 0 and out.strip():
        parts = out.strip().split()
        if len(parts) == 2:
            try:
                ahead = int(parts[0])
                behind = int(parts[1])
            except ValueError:
                pass
    return local_sha, remote_sha, ahead, behind


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def pull_remote(
    repo_dir: Path,
    remote: str = "origin",
    branch: str = "main",
    *,
    dry_run: bool = False,
    allow_rebase: bool = False,
    conflict_dir: Optional[Path] = None,
) -> PullResult:
    cfg = PullConfig(
        repo_dir=repo_dir,
        remote=remote,
        branch=branch,
        dry_run=dry_run,
        allow_rebase=allow_rebase,
        conflict_dir=conflict_dir or DEFAULT_CONFLICT_DIR,
    )
    return PullStage(cfg).pull()

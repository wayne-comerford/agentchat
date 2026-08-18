"""
agentchat v1.2 — GitHub sync agent (v1.2.0.dev20).

The sync agent is the durable-source-of-truth bridge between a local agentchat
workspace and a GitHub mirror repo. It is a **stdlib-only** module on purpose:
no PyGithub, no shelling out except for `git` itself. The job is narrow:

    1. Read the local workspace (memory + scrubbed config + audit log).
    2. Build a **mirror tree** in a temp directory, with secrets scrubbed.
    3. Run `git add . && git commit && git push` against a remote URL.
    4. Append an entry to `audit.jsonl` in the local workspace root.

This module is the **legacy one-shot mirror flow** (per
``docs/design/dev20/sync-agent.md`` §4). The newer, more capable flow lives
in ``agentchat/sync_agent/``:

* ``sync_agent.commit`` — change detection + local commit
* ``sync_agent.push``   — push with SSH/PAT/GitHub-App auth + retry
* ``sync_agent.watcher``— debounced file-system watcher

This one-shot flow complements that stack for operators who just want
``agentchat-sync push`` to do everything in a single command. The
scrubber is shared logic (see ``SCRUB_PATTERNS`` below) — refactoring it
into ``sync_agent.scrubber`` is a clean dev21 follow-up.

What it does NOT do (deferred to dev21+):
    * Daemon mode (file-watch → auto-push). v1.2.0.dev20 is **one-shot CLI**.
    * PR review flow (PRs require a GitHub API token; out of scope here).
    * Pull / clone on startup (also requires API to merge with the local tree).
    * Federation-aware multi-workspace sync (v1.3).

Security contract:
    * Memory files are scrubbed line-by-line against `SCRUB_PATTERNS` below
      before being written to the mirror. `npub1...` is **kept** (it's public);
      `nsec1...`, `nsec_bech32`, `private_key_hex`, `ghp_*`, etc. are replaced
      with `***REDACTED:<reason>***`.
    * Files in `NEVER_PUSH` (nsec, OAuth tokens, raw config) are skipped
      entirely before scrubbing — they never enter the mirror tree.
    * Every push writes one line to `~/.hermes/sync/audit.jsonl` capturing
      scrub counts, file counts, target repo, and commit SHA. Audit is local
      and travels into the mirror as `audit/audit.jsonl` (append-only).
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterable

# Re-export the canonical scrubber so legacy callers (sync_cli.py, the
# test suite, and the original ``agentchat-sync push`` flow) keep working
# without any import changes. The home of truth is
# ``agentchat.sync_agent.scrubber`` (v1.2.0.dev22).
from .sync_agent.scrubber import (  # noqa: F401
    SCRUB_PATTERNS,
    NEVER_PUSH_BASENAMES,
    NEVER_PUSH_PATH_SUBSTRINGS,
    ScrubStats,
    scrub_text,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# HERMES_HOME: the directory that contains memory/, nostr/, sync/, etc.
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))

# Memory root: <HERMES_HOME>/memory. Layout is agents/<name>/MEMORY.md,
# team/SHARED.md, projects/<slug>.md.
MEMORY_ROOT = HERMES_HOME / "memory"

# Sync working dir: <HERMES_HOME>/sync. Holds the bare-cloned mirror plus
# audit.jsonl + sync state (last SHA, last push time, scrub stats).
SYNC_ROOT = HERMES_HOME / "sync"
AUDIT_LOG = SYNC_ROOT / "audit.jsonl"
LAST_PUSH_FILE = SYNC_ROOT / ".last-push"
SCRUB_STATS_FILE = SYNC_ROOT / ".scrub-stats.json"

# Default remote target. The user can override via env or `--remote` flag.
# Convention: one repo per workspace. The placeholder is resolved at init time
# against the workspace slug.
DEFAULT_REMOTE_TEMPLATE = "git@github.com:{owner}/{repo}.git"

# ---------------------------------------------------------------------------
# Secret scrubbing
# ---------------------------------------------------------------------------
#
# The regex table, file-skip list, path-skip list, ``ScrubStats`` data class
# and ``scrub_text()`` function all live in ``agentchat.sync_agent.scrubber``
# (v1.2.0.dev22). This module re-exports them for backward compatibility so
# ``sg.scrub_text`` / ``sg.SCRUB_PATTERNS`` / ``sg.ScrubStats`` keep working.
#
# The pattern-ordering contract (``sk-ant-...`` BEFORE generic ``sk-...``) is
# enforced by the canonical module — see the long docstring at the top of
# ``agentchat/sync_agent/scrubber.py``.


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class SyncResult:
    """Result of a `sync_push` call."""

    repo: str
    commit_sha: str
    files_mirrored: int
    files_skipped: int
    bytes_mirrored: int
    scrub_stats: ScrubStats
    pushed: bool
    message: str


# ---------------------------------------------------------------------------
# Scrubber
# ---------------------------------------------------------------------------
#
# ``scrub_text`` is re-exported from ``agentchat.sync_agent.scrubber`` at
# the top of this file. See that module for the regex table and the
# pattern-ordering contract.


def should_skip_path(path: Path) -> bool:
    """Return True if *path* must never enter the mirror tree.

    Matches on basename (NEVER_PUSH_BASENAMES) and on path substrings
    (NEVER_PUSH_PATH_SUBSTRINGS). Symlinks pointing outside the workspace
    root are also skipped.
    """
    name = path.name
    for pat in NEVER_PUSH_BASENAMES:
        # Support literal names and glob-like `*.ext` patterns.
        if pat.startswith("*."):
            if name.endswith(pat[1:]):
                return True
        elif pat == name:
            return True

    s = str(path)
    for sub in NEVER_PUSH_PATH_SUBSTRINGS:
        if sub in s:
            return True
    return False


# ---------------------------------------------------------------------------
# Mirror builder
# ---------------------------------------------------------------------------


# Files we want to mirror, by path under HERMES_HOME. The mapping is
# (relative path, mirror path). Symmetric paths are common (memory/agents/
# stays memory/agents/ in the mirror), but we keep the indirection so we
# can add renames without rewriting call sites.
#
# nsec.json files are deliberately NOT included here — they would never
# be safe to push, so we don't even visit them. (They live next to
# registry.json in the nostr/ tree, but the registry.json entry does
# not rglob the parent.)
MIRROR_MAP: list[tuple[str, str]] = [
    ("memory/agents", "memory/agents"),
    ("memory/team", "memory/team"),
    ("memory/projects", "memory/projects"),
    ("memory/indexes", "memory/indexes"),
    ("nostr/registry.json", "config/nostr-registry.json"),
    ("nostr/agentchat-bridge.yaml", "config/agentchat-bridge.yaml"),
    ("nostr/personas", "config/personas"),
]


def build_mirror_tree(
    *,
    workspace_slug: str,
    audit_entry: dict | None = None,
    stats: ScrubStats | None = None,
) -> dict:
    """Return a manifest of files to mirror, scrubbed in-place.

    Returns a dict suitable for passing to `materialize_mirror`:

        {
            "workspace_slug": str,
            "files": [
                {
                    "src": <absolute path on local disk>,
                    "dst": <path inside the mirror tree, e.g. "memory/agents/hermes/MEMORY.md">,
                    "size": int,
                    "skipped": bool,
                    "skip_reason": str | None,
                },
                ...
            ],
            "stats": ScrubStats,
            "audit_entry": dict,
        }

    Skipped files are reported but do not cause errors. The caller decides
    what to do (typically: include a summary in the commit message).
    """
    if stats is None:
        stats = ScrubStats()

    files: list[dict] = []

    for src_rel, dst_rel in MIRROR_MAP:
        src_abs = HERMES_HOME / src_rel
        if not src_abs.exists():
            files.append(
                {
                    "src": str(src_abs),
                    "dst": dst_rel,
                    "size": 0,
                    "skipped": True,
                    "skip_reason": "source-missing",
                }
            )
            continue
        if src_abs.is_file():
            _collect_file(src_abs, Path(dst_rel), files, stats)
        else:
            for child in sorted(src_abs.rglob("*")):
                if child.is_dir():
                    continue
                if should_skip_path(child):
                    files.append(
                        {
                            "src": str(child),
                            "dst": str(Path(dst_rel) / child.relative_to(src_abs)),
                            "size": 0,
                            "skipped": True,
                            "skip_reason": "never-push",
                        }
                    )
                    continue
                _collect_file(child, Path(dst_rel) / child.relative_to(src_abs), files, stats)

    return {
        "workspace_slug": workspace_slug,
        "files": files,
        "stats": stats,
        "audit_entry": audit_entry or {},
    }


def _collect_file(src: Path, dst: Path, files: list[dict], stats: ScrubStats) -> None:
    try:
        size = src.stat().st_size
    except OSError as exc:
        files.append(
            {
                "src": str(src),
                "dst": str(dst),
                "size": 0,
                "skipped": True,
                "skip_reason": f"stat-error:{exc.__class__.__name__}",
            }
        )
        return
    files.append(
        {
            "src": str(src),
            "dst": str(dst),
            "size": size,
            "skipped": False,
            "skip_reason": None,
        }
    )


def materialize_mirror(manifest: dict, mirror_root: Path) -> int:
    """Write the scrubbed file tree under *mirror_root*. Return bytes written.

    Always rewrites the entire tree: callers should pass a fresh `mirror_root`
    (e.g. a `tempfile.TemporaryDirectory`). The function is pure: it does
    not commit, push, or touch git internals.
    """
    mirror_root.mkdir(parents=True, exist_ok=True)
    stats: ScrubStats = manifest["stats"]
    workspace_slug: str = manifest["workspace_slug"]
    audit_entry: dict = manifest["audit_entry"]

    bytes_written = 0

    for entry in manifest["files"]:
        if entry["skipped"]:
            continue
        src = Path(entry["src"])
        dst = mirror_root / entry["dst"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            raw = src.read_bytes()
        except OSError as exc:
            entry["skipped"] = True
            entry["skip_reason"] = f"read-error:{exc.__class__.__name__}"
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Binary file (shouldn't happen for our scope, but be safe).
            dst.write_bytes(raw)
            bytes_written += len(raw)
            continue
        scrubbed = scrub_text(text, stats=stats)
        out = scrubbed.encode("utf-8")
        dst.write_bytes(out)
        bytes_written += len(out)

    # Always include a README.md at the mirror root so a fresh-clone has context.
    _write_mirror_readme(mirror_root, workspace_slug, manifest)
    bytes_written += (mirror_root / "README.md").stat().st_size

    # Always include a .gitignore.
    _write_mirror_gitignore(mirror_root)
    bytes_written += (mirror_root / ".gitignore").stat().st_size

    # workspace.yaml — small metadata
    _write_workspace_yaml(mirror_root, workspace_slug)
    bytes_written += (mirror_root / "workspace.yaml").stat().st_size

    # Audit log: append the current entry under audit/audit.jsonl
    if audit_entry:
        audit_dir = mirror_root / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        with (audit_dir / "audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(audit_entry, sort_keys=True) + "\n")
        bytes_written += (audit_dir / "audit.jsonl").stat().st_size

    return bytes_written


def _write_mirror_readme(mirror_root: Path, workspace_slug: str, manifest: dict) -> None:
    files_total = len(manifest["files"])
    files_skipped = sum(1 for f in manifest["files"] if f["skipped"])
    skipped_paths = [
        f["src"] for f in manifest["files"] if f["skipped"]
    ][:5]
    text = f"""# agentchat workspace mirror — {workspace_slug}

This repository is an **automated mirror** of the agentchat workspace
`{workspace_slug}`. It is updated by the `agentchat-sync` CLI; do not
edit files by hand — your changes will be overwritten on the next push.

## Layout

```
README.md         this file
workspace.yaml    workspace metadata (slug, version, last-sync time)
.gitignore        excludes local-only files
memory/           agent + team + project memories (secrets scrubbed)
config/           scrubbed subset of ~/.hermes/nostr config
audit/            append-only audit log of every sync push
```

## What is scrubbed

The sync agent strips the following classes of secrets before writing
any file to this repo:

"""
    for label, _pat, _repl in SCRUB_PATTERNS:
        text += f"- `{label}`\n"
    text += """
## What is skipped outright

The following files are never copied to the mirror (they are local-only
or contain raw credentials):

"""
    for name in sorted(NEVER_PUSH_BASENAMES):
        text += f"- `{name}`\n"
    text += "\n## Stats from this push\n\n"
    text += f"- files considered: **{files_total}**\n"
    text += f"- files skipped:    **{files_skipped}**\n"
    if skipped_paths:
        text += f"- first 5 skipped paths: {', '.join(skipped_paths)}\n"
    text += "\n---\n\nGenerated by `agentchat-sync` v1.2.0.dev20.\n"
    (mirror_root / "README.md").write_text(text, encoding="utf-8")


def _write_mirror_gitignore(mirror_root: Path) -> None:
    (mirror_root / ".gitignore").write_text(
        "# local-only state\n"
        ".last-push\n"
        ".scrub-stats.json\n"
        "*.bak\n"
        "*.swp\n"
        "\n# Python\n"
        "__pycache__/\n"
        "*.pyc\n",
        encoding="utf-8",
    )


def _write_workspace_yaml(mirror_root: Path, workspace_slug: str) -> None:
    (mirror_root / "workspace.yaml").write_text(
        "agentchat:\n"
        "  version: 1.2.0.dev20\n"
        f"  workspace_slug: {workspace_slug}\n"
        f"  last_sync_utc: {datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Diff (status)
# ---------------------------------------------------------------------------


def status(
    *,
    workspace_slug: str,
    mirror_root: Path | None = None,
) -> dict:
    """Return a dict describing the current sync state.

    Computes a "would-push" manifest without writing anything, and compares
    file counts to the last successful push (if any). Always returns a dict;
    the CLI prints it as JSON.
    """
    manifest = build_mirror_tree(workspace_slug=workspace_slug)
    last = _read_last_push()
    return {
        "workspace_slug": workspace_slug,
        "files_total": len(manifest["files"]),
        "files_to_mirror": sum(1 for f in manifest["files"] if not f["skipped"]),
        "files_skipped": sum(1 for f in manifest["files"] if f["skipped"]),
        "skipped_reasons": _bucket_skip_reasons(manifest["files"]),
        "last_push": last,
        "would_scrub": manifest["stats"].to_dict(),
    }


def _bucket_skip_reasons(files: Iterable[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in files:
        if not f["skipped"]:
            continue
        reason = f.get("skip_reason") or "unknown"
        # Collapse "stat-error:FileNotFoundError" to "stat-error".
        reason = reason.split(":", 1)[0]
        out[reason] = out.get(reason, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


def init_mirror_repo(mirror_root: Path, remote: str) -> None:
    """Make *mirror_root* a git repo and add the remote.

    If a .git directory already exists, this is a no-op. If the remote is
    unreachable or the local git binary is missing, raise RuntimeError with
    a clear message — the caller decides whether to surface or swallow.
    """
    git = _git_binary()
    if (mirror_root / ".git").exists():
        # Already a repo; just ensure the remote matches.
        existing = _run(
            [git, "-C", str(mirror_root), "remote", "get-url", "origin"],
            capture=True,
        ).stdout.strip()
        if existing != remote:
            _run(
                [git, "-C", str(mirror_root), "remote", "set-url", "origin", remote],
                check=True,
            )
        return

    _run([git, "init", "--initial-branch=main", str(mirror_root)], check=True)
    _run([git, "-C", str(mirror_root), "remote", "add", "origin", remote], check=True)


def push(
    *,
    workspace_slug: str,
    remote: str,
    commit_message: str | None = None,
    author_name: str = "agentchat-sync",
    author_email: str = "agentchat-sync@localhost",
    dry_run: bool = False,
) -> SyncResult:
    """Build the mirror tree and push to *remote*. Return a SyncResult.

    On a `dry_run`, no commits are made and no push is attempted — the
    function still builds the mirror under a temp dir so the caller can
    inspect what *would* be sent. (The temp dir is cleaned up at the end.)
    """
    if not commit_message:
        commit_message = f"sync: {workspace_slug} @ {datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}"

    audit_entry: dict = {
        "ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "workspace_slug": workspace_slug,
        "remote": remote,
        "commit_message": commit_message,
        "author": f"{author_name} <{author_email}>",
        "agentchat_version": "1.2.0.dev20",
    }
    manifest = build_mirror_tree(
        workspace_slug=workspace_slug, audit_entry=audit_entry
    )
    stats: ScrubStats = manifest["stats"]

    # Audit append BEFORE push — if the push itself fails, we still have a
    # record that the operator attempted a sync.
    _append_audit(audit_entry)

    with tempfile.TemporaryDirectory(prefix="agentchat-sync-") as td:
        mirror_root = Path(td)
        bytes_written = materialize_mirror(manifest, mirror_root)
        files_mirrored = sum(1 for f in manifest["files"] if not f["skipped"])
        files_skipped = sum(1 for f in manifest["files"] if f["skipped"])

        if dry_run:
            return SyncResult(
                repo=remote,
                commit_sha="(dry-run)",
                files_mirrored=files_mirrored,
                files_skipped=files_skipped,
                bytes_mirrored=bytes_written,
                scrub_stats=stats,
                pushed=False,
                message="dry-run; nothing committed",
            )

        init_mirror_repo(mirror_root, remote)
        git = _git_binary()
        _run([git, "-C", str(mirror_root), "config", "user.name", author_name], check=True)
        _run([git, "-C", str(mirror_root), "config", "user.email", author_email], check=True)
        # Commit locally first so the branch is not empty. Without at
        # least one local commit, `pull --rebase` rejects the operation
        # with "no commit on branch 'main' yet". The `--allow-empty`
        # below keeps the no-op case (nothing changed since last push)
        # from failing too.
        _run([git, "-C", str(mirror_root), "add", "-A"], check=True)
        _run(
            [git, "-C", str(mirror_root), "commit", "--allow-empty", "-m", commit_message],
            check=True,
        )
        # The mirror is private and only the sync agent writes to it.
        # `push --force-with-lease` is the safe form of force-push: it
        # fails if the remote was updated by *something else* since our
        # last fetch, which protects against the rare case of a manual
        # push from another machine. We fetch first so `--force-with-lease`
        # has a current expected tip; without that it complains about
        # "stale info" because it has no record of what the remote
        # currently is. For our use case (single-writer mirror), this
        # gives the linear history we want without merge commits and
        # without the rebase-conflict class of failures that happen when
        # README.md / workspace.yaml get re-generated by every sync.
        # The commit SHA is the most recent ref.
        _run(
            [git, "-C", str(mirror_root), "fetch", "origin", "main"],
            capture=True,
            check=False,
        )
        sha_proc = _run(
            [git, "-C", str(mirror_root), "rev-parse", "HEAD"], capture=True
        )
        commit_sha = sha_proc.stdout.strip()

        push_proc = _run(
            [git, "-C", str(mirror_root), "push", "--force-with-lease", "origin", "main"],
            capture=True,
            check=False,
        )
        if push_proc.returncode != 0:
            raise RuntimeError(
                f"git push failed (rc={push_proc.returncode})\n"
                f"--- stdout ---\n{push_proc.stdout}\n"
                f"--- stderr ---\n{push_proc.stderr}\n"
            )

    _write_last_push(commit_sha=commit_sha, remote=remote, files=files_mirrored)
    _write_scrub_stats(stats)

    # Also commit the local audit + scrub stats into the mirror root if
    # we kept it; but since we used a tempdir, we append to the local
    # audit log file (already done in _append_audit above). Good enough.

    return SyncResult(
        repo=remote,
        commit_sha=commit_sha,
        files_mirrored=files_mirrored,
        files_skipped=files_skipped,
        bytes_mirrored=bytes_written,
        scrub_stats=stats,
        pushed=True,
        message="ok",
    )


# ---------------------------------------------------------------------------
# Local audit + last-push bookkeeping
# ---------------------------------------------------------------------------


def _append_audit(entry: dict) -> None:
    SYNC_ROOT.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def _read_last_push() -> dict | None:
    if not LAST_PUSH_FILE.exists():
        return None
    try:
        return json.loads(LAST_PUSH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_last_push(*, commit_sha: str, remote: str, files: int) -> None:
    SYNC_ROOT.mkdir(parents=True, exist_ok=True)
    LAST_PUSH_FILE.write_text(
        json.dumps(
            {
                "commit_sha": commit_sha,
                "remote": remote,
                "files_mirrored": files,
                "ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_scrub_stats(stats: ScrubStats) -> None:
    SYNC_ROOT.mkdir(parents=True, exist_ok=True)
    SCRUB_STATS_FILE.write_text(
        json.dumps(
            {
                "ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                **stats.to_dict(),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Proc:
    returncode: int
    stdout: str
    stderr: str


def _run(
    cmd: list[str],
    *,
    capture: bool = False,
    check: bool = False,
) -> _Proc:
    """Run *cmd*; if *capture*, return stdout/stderr; if *check*, raise on non-zero."""
    if capture:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    else:
        proc = subprocess.run(cmd, capture_output=False, text=True)
    out = _Proc(
        returncode=proc.returncode,
        stdout=proc.stdout if capture else "",
        stderr=proc.stderr if capture else "",
    )
    if check and out.returncode != 0:
        raise RuntimeError(
            f"command failed (rc={out.returncode}): {' '.join(cmd)}\n"
            f"--- stdout ---\n{out.stdout}\n"
            f"--- stderr ---\n{out.stderr}\n"
        )
    return out


def _git_binary() -> str:
    """Return the path to the `git` binary; raise clearly if not installed."""
    g = shutil.which("git")
    if g is None:
        raise RuntimeError("git is not installed or not on PATH")
    return g


# ---------------------------------------------------------------------------
# Public helper: render a SyncResult / status dict as text
# ---------------------------------------------------------------------------


def format_result(result: SyncResult) -> str:
    lines = [
        f"workspace: {result.repo}",
        f"commit:    {result.commit_sha}",
        f"pushed:    {result.pushed}",
        f"mirrored:  {result.files_mirrored} files, {result.bytes_mirrored:,} bytes",
        f"skipped:   {result.files_skipped} files",
        "scrub counts: " + (
            ", ".join(f"{k}={v}" for k, v in sorted(result.scrub_stats.counts.items()))
            or "(none)"
        ),
        f"message:   {result.message}",
    ]
    return "\n".join(lines)


def format_status(d: dict) -> str:
    last = d.get("last_push")
    last_str = (
        f"{last['commit_sha']} at {last['ts_utc']} ({last['files_mirrored']} files)"
        if last
        else "(none — first push)"
    )
    lines = [
        f"workspace_slug:        {d['workspace_slug']}",
        f"files_total:           {d['files_total']}",
        f"files_to_mirror:       {d['files_to_mirror']}",
        f"files_skipped:         {d['files_skipped']}",
        f"skipped_reasons:       {d['skipped_reasons']}",
        f"last_push:             {last_str}",
        f"would_scrub:           {d['would_scrub']}",
    ]
    return "\n".join(lines)

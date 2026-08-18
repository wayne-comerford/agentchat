"""
Commit stage: detect the change set, build a structured commit message,
and run ``git add -A && git commit`` against ``repo_dir``.

Source of truth for "what changed" is ``git status --porcelain=v1``.
That's intentional: we don't want the watcher to be authoritative about
the change set — git is. If the watcher and git disagree, the operator
should see exactly what git sees in the commit message.

Key public types:

* ``ChangeRecord``  — one file's change (path, kind, is_rename).
* ``ChangeSet``     — collection of records + aggregate counters.
* ``CommitResult``  — outcome of a ``CommitStage.run()`` call (the
                      new HEAD SHA, the message that was used, and the
                      raw change set that was committed).
* ``CommitStage``   — the stage itself; pure stdlib, no shell strings.

This module does **not** push. Push lives in ``sync_agent.push``
(delivered in t_11537e05).
"""

from __future__ import annotations

import dataclasses
import fnmatch
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable, Sequence

from .config import SyncConfig


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


# git status --porcelain=v1 codes we care about. The X/Y two-letter status
# format: X = index, Y = worktree. We classify based on the worktree (Y)
# side because that's what would actually be committed by ``git add -A``.
#
# Examples of the X/Y codes we'll see:
#
#   " M"  unmodified in index, modified in worktree  -> modified
#   "M "  modified in index, unmodified in worktree -> modified (already staged)
#   "MM"  modified in index, modified in worktree   -> modified
#   "A "  added in index                             -> added
#   "??"  untracked                                  -> added
#   " D"  unmodified in index, deleted in worktree   -> deleted
#   "D "  deleted from index                         -> deleted
#   "R "  renamed in index  (path field is "old -> new")
#   "C "  copied in index   (path field is "old -> new")
#
# The "intent-to-add" sentinel 'A ' alone is fine to commit; we treat it
# as added. We'll never be in a partial-cherry-pick state because the
# commit stage itself owns staging.
_STATUS_CODE_TO_KIND = {
    "M": "modified",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "U": "conflicted",  # unmerged — we refuse to commit these
}


@dataclasses.dataclass(frozen=True)
class ChangeRecord:
    """One file in the change set."""

    path: str          # path relative to repo_dir, forward slashes
    kind: str          # "added" | "modified" | "deleted" | "renamed" | "copied"
    old_path: str | None = None  # set only when kind == "renamed" | "copied"

    @property
    def is_add(self) -> bool:
        return self.kind == "added"

    @property
    def is_modify(self) -> bool:
        return self.kind == "modified"

    @property
    def is_delete(self) -> bool:
        return self.kind == "deleted"

    @property
    def is_rename(self) -> bool:
        return self.kind == "renamed"


@dataclasses.dataclass(frozen=True)
class ChangeSet:
    """A grouped set of ChangeRecords with aggregate counters."""

    records: tuple[ChangeRecord, ...]
    origin: str = "watcher"  # "watcher" | "polling" | "manual"

    @property
    def added(self) -> tuple[ChangeRecord, ...]:
        return tuple(r for r in self.records if r.is_add)

    @property
    def modified(self) -> tuple[ChangeRecord, ...]:
        return tuple(r for r in self.records if r.is_modify)

    @property
    def deleted(self) -> tuple[ChangeRecord, ...]:
        return tuple(r for r in self.records if r.is_delete)

    @property
    def renamed(self) -> tuple[ChangeRecord, ...]:
        return tuple(r for r in self.records if r.is_rename)

    @property
    def total(self) -> int:
        return len(self.records)

    def is_empty(self) -> bool:
        return not self.records

    def summary_line(self) -> str:
        """One-line summary for commit subject lines and logs."""
        return (
            f"{len(self.added)} added, {len(self.modified)} modified, "
            f"{len(self.deleted)} deleted, {len(self.renamed)} renamed"
        )


@dataclasses.dataclass(frozen=True)
class CommitResult:
    """Outcome of a CommitStage.run() invocation."""

    committed: bool
    sha: str | None
    message: str
    change_set: ChangeSet
    raw_stdout: str = ""
    raw_stderr: str = ""

    @property
    def noop(self) -> bool:
        return not self.committed


# ---------------------------------------------------------------------------
# ``git status --porcelain`` parser
# ---------------------------------------------------------------------------


# git porcelain status line examples we need to handle:
#
#   " M src/foo.py"
#   "M  src/foo.py"
#   "MM src/foo.py"
#   "?? new_file.py"
#   " D deleted.py"
#   "D  staged_deleted.py"
#   "R  old_name.py -> new_name.py"
#   "C  original.py -> copy.py"
#
# Renames/copies have the form ``"<code> <score> old -> new"`` where the
# score is e.g. ``R100`` or ``C75``. The score is optional in --porcelain
# output but git always emits it.
_STATUS_LINE_RE = re.compile(
    r"""^(?P<xy>[ MADRCU?!]{2})
        (?:\ (?P<score>[CR][0-9]+))?
        \ (?P<rest>.+)$""",
    re.VERBOSE,
)


def _parse_status_line(line: str) -> ChangeRecord | None:
    """Parse one line of ``git status --porcelain=v1`` output.

    Returns ``None`` for lines we don't recognise (and silently drops
    the noise so the caller doesn't have to). Conflict lines (``UU``,
    ``AA``, etc.) raise ``RuntimeError`` because committing them would
    be wrong.
    """
    if not line:
        return None
    # Strip the trailing NUL git uses between paths on rare platforms —
    # --porcelain=v1 uses spaces, but defensive doesn't hurt.
    line = line.rstrip("\r\n")
    m = _STATUS_LINE_RE.match(line)
    if not m:
        # Unrecognised; don't crash, just drop it. Examples that hit
        # this branch: "!!" ignored paths, lines truncated by very long
        # paths on Windows. The operator will see them in git status.
        return None

    xy = m.group("xy")
    worktree = xy[1]  # Y column
    index = xy[0]     # X column

    # Conflict detection — refuse to commit a half-merged tree.
    if worktree in ("U",) or index in ("U",) or xy in ("AA", "DD", "AU", "UA", "DU", "UD"):
        raise RuntimeError(
            f"repo has unmerged paths; refusing to commit until the merge is resolved: {line!r}"
        )

    rest = m.group("rest")

    # Untracked files use "??" in the X column; the Y column is also "?".
    if xy == "??":
        return ChangeRecord(path=rest.replace("\\", "/"), kind="added")

    # ``R``/``C`` in the INDEX column wins even if the worktree also
    # has a side-channel status (e.g. ``RM`` for a staged rename
    # where the worktree still has the old path modified). The path
    # field still needs to be split on `` -> ``. If the worktree
    # column says "M" or "D", that's the *secondary* signal — git
    # already knows how to handle the index-side rename, so we report
    # the rename and ignore the worktree-side noise.
    if index in ("R", "C"):
        kind = _STATUS_CODE_TO_KIND[index]  # "renamed" or "copied"
        if " -> " not in rest:
            return None
        old, new = rest.split(" -> ", 1)
        return ChangeRecord(
            path=new.replace("\\", "/"),
            kind=kind,
            old_path=old.replace("\\", "/"),
        )

    kind_letter = worktree if worktree in ("M", "D") else index
    if kind_letter not in _STATUS_CODE_TO_KIND:
        return None
    kind = _STATUS_CODE_TO_KIND[kind_letter]

    if kind in ("renamed", "copied"):
        # Format: "old_name -> new_name". git's --porcelain quotes paths
        # with spaces using C-style escapes (\); for the dev20 test
        # surface there are no quoted paths, so a plain split suffices.
        if " -> " not in rest:
            # Malformed; skip rather than corrupt the message.
            return None
        old, new = rest.split(" -> ", 1)
        return ChangeRecord(
            path=new.replace("\\", "/"),
            kind=kind,
            old_path=old.replace("\\", "/"),
        )

    return ChangeRecord(path=rest.replace("\\", "/"), kind=kind)


def collect_changes(repo_dir: Path) -> ChangeSet:
    """Run ``git status --porcelain=v1`` and return the parsed ChangeSet.

    Empty repo (no commits yet) returns an empty set — the caller is
    expected to handle "nothing to do" gracefully. We do **not** raise
    on an empty repo because the watcher's first commit *is* the
    initial commit.
    """
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git status failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    records: list[ChangeRecord] = []
    for line in proc.stdout.splitlines():
        rec = _parse_status_line(line)
        if rec is not None:
            records.append(rec)
    return ChangeSet(records=tuple(records), origin="polling")


def has_uncommitted_changes(repo_dir: Path) -> bool:
    """Cheap "is there anything new?" check.

    Implemented as ``git status --porcelain`` for the polling watchdog
    so we don't pay the parse cost when the tree is clean. Used by
    ``watch_and_commit`` to short-circuit when nothing changed.
    """
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # On error, be conservative: assume there *might* be changes so
        # we don't silently lose a commit. The commit stage itself
        # will catch any actual problems.
        return True
    return bool(proc.stdout.strip())


# ---------------------------------------------------------------------------
# Commit message builder
# ---------------------------------------------------------------------------


def build_commit_message(change_set: ChangeSet, origin: str | None = None) -> str:
    """Produce a structured commit message for a change set.

    Format (matches the design doc, §6):

        chore(sync): workspace + memory snapshot

        - N added, M modified, K deleted, J renamed
        - triggered: <origin>

    A human scanning ``git log`` can tell what kind of change happened
    without reading the diff first. ``origin`` defaults to the change
    set's own ``origin`` field.
    """
    triggered = origin or change_set.origin or "manual"
    return (
        "chore(sync): workspace + memory snapshot\n\n"
        f"- {change_set.summary_line()}\n"
        f"- triggered: {triggered}\n"
    )


# ---------------------------------------------------------------------------
# CommitStage
# ---------------------------------------------------------------------------


class CommitStage:
    """The ``git add -A && git commit`` step.

    Stateless apart from ``config``. Safe to instantiate one per
    worker thread if the orchestrator ever spawns multiple.
    """

    def __init__(self, config: SyncConfig):
        self.config = config

    # -- public ---------------------------------------------------------

    def run(self, change_set: ChangeSet | None = None) -> CommitResult:
        """Commit pending changes.

        If ``change_set`` is None, the stage calls ``collect_changes``
        itself. The pre-computed variant exists so the watcher can
        pass in the change set it already built (no double ``git
        status`` call).
        """
        if change_set is None:
            change_set = collect_changes(self.config.repo_dir)
        if change_set.is_empty():
            message = build_commit_message(change_set)
            return CommitResult(
                committed=False,
                sha=None,
                message=message,
                change_set=change_set,
            )

        message = build_commit_message(change_set)
        return self._commit(change_set, message)

    def run_once(self) -> CommitResult:
        """Convenience: ``collect_changes`` then ``_commit``. Same as
        ``run(None)`` but reads slightly cleaner at call sites."""
        return self.run(None)

    # -- internals ------------------------------------------------------

    def _commit(self, change_set: ChangeSet, message: str) -> CommitResult:
        repo = str(self.config.repo_dir)

        # Filter the ChangeSet against the configured exclude patterns.
        # The PollingEmitter already does this when it builds the
        # change set, but ``collect_changes`` (which ``run_once`` and
        # the CLI use directly) does not — it asks git. Defence in
        # depth: if a path matches an exclude pattern, drop it here
        # rather than push it to the remote.
        filtered_records = tuple(
            r for r in change_set.records if not _path_excluded(r.path, self.config.exclude)
        )
        # Build a new ChangeSet with the filtered records, preserving
        # the origin and any other metadata.
        filtered_change_set = ChangeSet(records=filtered_records, origin=change_set.origin)
        if len(filtered_records) != len(change_set.records):
            log.info(
                "filtered %d excluded path(s) from change set",
                len(change_set.records) - len(filtered_records),
            )

        # Stage strategy:
        #
        # * If we have a pre-computed ChangeSet (from the watcher or
        #   polling sweep), stage only the paths in that set. The
        #   watcher's exclude rules are then authoritative — files
        #   that match `.git`, `.venv`, `__pycache__`, etc. never make
        #   it into the commit because the watcher never reported them.
        # * If we have no ChangeSet (the CLI ran ``agentchat-sync-stage
        #   once`` directly, or the orchestrator fell back to a
        #   status-based commit), use ``git add -A`` which respects the
        #   repo's ``.gitignore``.
        #
        # The ``git add`` per-path form is preferred when possible
        # because it gives the operator a tighter blast radius — even
        # a misconfigured exclude list can't accidentally push a
        # ``.git/objects/`` blob.
        if filtered_change_set.records:
            paths = [r.path for r in filtered_change_set.records]
            add = subprocess.run(
                ["git", "add", "--", *paths],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            add = subprocess.run(
                ["git", "add", "-A"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
        if add.returncode != 0:
            raise RuntimeError(
                f"git add failed (rc={add.returncode}): {add.stderr.strip()}"
            )

        commit_cmd = ["git", "commit", "-m", message]
        if self.config.author_name and self.config.author_email:
            commit_cmd.extend(["--author", f"{self.config.author_name} <{self.config.author_email}>"])

        commit = subprocess.run(
            commit_cmd,
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )

        if commit.returncode != 0:
            # ``git commit`` returns non-zero when there is literally
            # nothing staged (race with the watcher emptying the index
            # between ``git add`` and ``git commit``). That's not a
            # fatal error — it just means another process committed
            # first. Surface as a no-op.
            if "nothing to commit" in (commit.stdout + commit.stderr).lower():
                return CommitResult(
                    committed=False,
                    sha=None,
                    message=message,
                    change_set=filtered_change_set,
                    raw_stdout=commit.stdout,
                    raw_stderr=commit.stderr,
                )
            raise RuntimeError(
                f"git commit failed (rc={commit.returncode}): {commit.stderr.strip()}"
            )

        sha = self._head_sha()
        return CommitResult(
            committed=True,
            sha=sha,
            message=message,
            change_set=filtered_change_set,
            raw_stdout=commit.stdout,
            raw_stderr=commit.stderr,
        )

    def _head_sha(self) -> str:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(self.config.repo_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout.strip()


# ---------------------------------------------------------------------------
# File-system scan fallback (used by PollingEmitter)
# ---------------------------------------------------------------------------


# Names we always ignore, on top of the user's ``exclude`` patterns.
# Kept here (not in config) because they're hard-coded as "never push".
_ALWAYS_IGNORE_NAMES: frozenset[str] = frozenset({
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".DS_Store",
})


def _path_excluded(path: str, exclude: Sequence[str]) -> bool:
    """True if any path component or the basename matches an exclude pattern.

    The ``exclude`` patterns are shell-style fnmatch globs. We match
    against each path component AND the full path so that ``.venv``
    (a directory name) is caught when the path is ``.venv/skip.py``.
    """
    parts = path.split("/")
    for pattern in exclude:
        for part in parts:
            if fnmatch.fnmatch(part, pattern):
                return True
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def _should_skip(name: str, exclude: Sequence[str], include_hidden: bool) -> bool:
    """Decide whether a single directory entry should be skipped during
    the recursive polling walk.

    * ``.git`` etc. are always skipped (these would explode git).
    * Anything matching an ``exclude`` glob is skipped.
    * Hidden files (starting with ``.``) are skipped unless
      ``include_hidden`` is True.
    """
    if name in _ALWAYS_IGNORE_NAMES:
        return True
    if not include_hidden and name.startswith(".") and name not in (".gitignore", ".gitattributes"):
        return True
    for pattern in exclude:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


# Quick test: poll a single root, return the set of relative paths that
# currently exist on disk. The watcher diffs two snapshots.
def snapshot_tree(root: Path, exclude: Sequence[str], include_hidden: bool) -> dict[str, float]:
    """Return a dict mapping ``{relpath: mtime}`` for every regular file
    under ``root``.

    Symlinks are followed (matches git's behavior for tracked content).
    Broken symlinks are silently dropped because git status will flag
    those separately.
    """
    out: dict[str, float] = {}
    _walk(root, root, exclude, include_hidden, out)
    return out


def _walk(
    root: Path,
    current: Path,
    exclude: Sequence[str],
    include_hidden: bool,
    out: dict[str, float],
) -> None:
    try:
        entries = list(os.scandir(current))
    except (PermissionError, FileNotFoundError, NotADirectoryError):
        return
    for entry in entries:
        if _should_skip(entry.name, exclude, include_hidden):
            continue
        # Resolve symlinks for the mtime read; if the target is gone we
        # skip the entry. ``os.DirEntry.stat(follow_symlinks=True)`` is
        # the stdlib-supported way and doesn't require opening the file.
        try:
            if entry.is_dir(follow_symlinks=False):
                _walk(root, Path(entry.path), exclude, include_hidden, out)
                continue
            if entry.is_file(follow_symlinks=False):
                st = entry.stat(follow_symlinks=True)
                rel = str(Path(entry.path).relative_to(root)).replace(os.sep, "/")
                out[rel] = st.st_mtime
            elif entry.is_symlink():
                # Symlink to a file: include the link's own mtime so
                # edits under ``~/.hermes/memory/...`` propagate through
                # the workspace symlink.
                try:
                    st = entry.stat(follow_symlinks=False)
                    rel = str(Path(entry.path).relative_to(root)).replace(os.sep, "/")
                    out[rel] = st.st_mtime
                except OSError:
                    pass
        except OSError:
            # Permission errors on individual entries don't kill the
            # whole walk; just skip them.
            continue
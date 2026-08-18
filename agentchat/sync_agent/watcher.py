"""
Watcher layer for the sync agent change-detection pipeline.

This module is the stdlib-only polling watcher that backs the dev20
sync agent. It deliberately does **not** depend on ``watchdog`` (or
``inotify_simple``, or ``fsevents``) — agentchat's runtime dependency
contract is stdlib-only. The design doc (t_08bd1def §2) called the
polling approach the "watchdog" because that's also what recovers a
dead inotify watcher; here it is the primary mechanism.

The abstraction is ``ChangeEmitter``: anything that can produce a
``ChangeSet`` (or report "no changes") when asked. ``PollingEmitter``
is the stdlib implementation; future work can add a
``WatchdogEmitter`` behind the same interface without touching the
rest of the pipeline.

The orchestrator glue lives at the bottom of this file:
``watch_and_commit(config, once=True)`` is the public entry point used
by the CLI and the unit tests. ``once=True`` means "do one sweep, then
exit" — that is the variant the acceptance test asserts on.

Threading
---------
``DebouncedEmitter`` schedules a callback after ``debounce_seconds`` of
quiet. The implementation uses ``threading.Timer`` which is the stdlib
idiom for one-shot delayed callbacks. Callers that want to drive the
loop themselves (the daemon mode that t_0105ff20 will own) should
hold a reference to the timer so they can ``.cancel()`` it on shutdown.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Protocol

from .commit import (
    ChangeSet,
    CommitResult,
    CommitStage,
    collect_changes,
    has_uncommitted_changes,
    snapshot_tree,
)
from .config import SyncConfig


log = logging.getLogger("agentchat.sync_agent.watcher")


# ---------------------------------------------------------------------------
# ``ChangeEmitter`` Protocol
# ---------------------------------------------------------------------------


class ChangeEmitter(Protocol):
    """Anything that can produce a ChangeSet (or None for "no changes").

    Implementations may be push (watchdog-style event hooks) or pull
    (polling). The orchestrator calls ``poll()`` on a fixed cadence;
    push-style emitters may simply ignore the call and rely on
    ``emit_change_set`` being invoked from an external callback.
    """

    def poll(self) -> ChangeSet | None:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# PollingEmitter — stdlib implementation
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PollingEmitter:
    """Poll the watched roots, diff against the previous snapshot,
    return a ChangeSet if anything differs.

    The emitter holds two snapshots:

    * ``_baseline`` — the FS state at the last successful commit (or
      at construction, before the first commit ever lands). This is
      the comparison anchor.
    * ``_current``   — the FS state observed at the most recent poll.

    On each ``poll()`` we build the diff from ``_baseline`` to
    ``_current``. That diff is what the commit stage commits. After
    the commit stage reports success, the orchestrator calls
    ``reset_baseline()`` so the next poll diffs against the new HEAD.

    Without the ``_baseline`` vs ``_current`` distinction the emitter
    would only ever report "what changed since the last poll" — and
    the debouncer would coalesce those into a single-file diff. The
    commit stage would commit one file per debounce window. That's
    wrong: an agent that writes 20 memory files in a burst should
    produce one commit, and the commit's ChangeSet must contain all
    20 files.

    The first call returns ``None`` (no baseline yet) so the watcher
    doesn't immediately commit the entire tree on startup.

    Heuristic for change detection (good enough for the polling
    watchdog, exact for the watcher when paired with the debouncer):

        * added    — path in current, not in baseline
        * deleted  — path in baseline, not in current
        * modified — path in both, mtime differs
        * renamed  — not detected here; relies on git's own rename
                     detection at ``git add`` time. Polling alone
                     cannot tell a rename from a delete+add pair, and
                     guessing wrong is worse than letting git handle it.
    """

    config: SyncConfig
    _baseline: dict[str, float] = dataclasses.field(
        default_factory=dict, init=False, repr=False
    )
    _current: dict[str, float] = dataclasses.field(
        default_factory=dict, init=False, repr=False
    )
    _baseline_taken: bool = dataclasses.field(default=False, init=False, repr=False)

    def poll(self) -> ChangeSet | None:
        """Return a ChangeSet if anything differs from the baseline,
        else None. Returns None on the very first call (baseline is
        being established, not diffed)."""
        current = self._scan()

        if not self._baseline_taken:
            self._baseline = current
            self._current = current
            self._baseline_taken = True
            log.debug("polling baseline established (%d files)", len(current))
            return None

        self._current = current
        return self._diff()

    def reset_baseline(self) -> None:
        """Reset the baseline to the most recent ``_current`` snapshot.

        Call this after a successful commit so the next poll diffs
        against the new HEAD rather than the original tree state.
        """
        self._baseline = dict(self._current)
        log.debug("polling baseline reset (%d files)", len(self._baseline))

    def _scan(self) -> dict[str, float]:
        current: dict[str, float] = {}
        for root in self.config.iter_watched():
            try:
                snap = snapshot_tree(
                    root,
                    self.config.exclude,
                    self.config.include_hidden,
                )
            except OSError as exc:
                log.warning("snapshot_tree failed for %s: %s", root, exc)
                continue
            for relpath, mtime in snap.items():
                # Prefix by the root so multiple watched roots with
                # same-named files don't collide.
                current[f"{root}{relpath}"] = mtime
        return current

    def _diff(self) -> ChangeSet | None:
        prev = self._baseline
        curr = self._current
        if prev == curr:
            return None

        added_keys = set(curr) - set(prev)
        deleted_keys = set(prev) - set(curr)
        modified_keys = {
            k for k in set(curr) & set(prev) if curr[k] != prev[k]
        }

        records = []
        for k in sorted(added_keys):
            records.append(_to_change_record(self._strip_root(k), "added"))
        for k in sorted(modified_keys):
            records.append(_to_change_record(self._strip_root(k), "modified"))
        for k in sorted(deleted_keys):
            records.append(_to_change_record(self._strip_root(k), "deleted"))

        if not records:
            return None
        return ChangeSet(records=tuple(records), origin="polling")

    def _strip_root(self, key: str) -> str:
        """Remove the absolute-root prefix so the returned paths are
        repo-relative (or, for memory trees, relative to their own root)."""
        for root in self.config.watched_roots:
            if key.startswith(str(root)):
                return key[len(str(root)):]
        return key


def _to_change_record(path: str, kind: str):
    from .commit import ChangeRecord

    # Strip leading slash if any (relative-to-repo paths should be
    # slash-free at the front).
    return ChangeRecord(path=path.lstrip("/"), kind=kind)


# ---------------------------------------------------------------------------
# DebouncedEmitter
# ---------------------------------------------------------------------------


class DebouncedEmitter:
    """Wrap any ``ChangeEmitter`` with debounce semantics.

    On each ``poll()`` that detects a change, restart a
    ``threading.Timer``; only when ``debounce_seconds`` elapse without
    a fresh change does the timer fire and invoke the supplied
    callback with the latest ChangeSet.

    This is what implements the design doc's "one commit per debounce
    window" rule: a burst of writes from the agent coalesces into a
    single commit rather than twenty.
    """

    def __init__(
        self,
        inner: ChangeEmitter,
        debounce_seconds: float,
        callback: Callable[[ChangeSet], None] | None = None,
    ):
        self.inner = inner
        self.debounce_seconds = debounce_seconds
        self.callback = callback
        self._latest: ChangeSet | None = None
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._cancelled = False

    def poll(self) -> ChangeSet | None:
        change = self.inner.poll()
        if change is None or change.is_empty():
            return None
        with self._lock:
            self._latest = change
            if self._timer is not None:
                self._timer.cancel()
            if self._cancelled:
                return None
            self._timer = threading.Timer(self.debounce_seconds, self._fire)
            self._timer.daemon = True
            self._timer.start()
        return None  # the actual ChangeSet delivery happens via the timer

    def _fire(self) -> None:
        with self._lock:
            change = self._latest
            self._latest = None
            self._timer = None
        if change is not None and self.callback is not None:
            try:
                self.callback(change)
            except Exception:  # pragma: no cover - callback error
                log.exception("debounced callback raised")

    def cancel(self) -> None:
        """Cancel any pending timer. Safe to call multiple times."""
        with self._lock:
            self._cancelled = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


# ---------------------------------------------------------------------------
# ``watch_and_commit`` — the public orchestrator entry point
# ---------------------------------------------------------------------------


def watch_and_commit(
    config: SyncConfig,
    *,
    once: bool = False,
    commit_stage: CommitStage | None = None,
    emitter: ChangeEmitter | None = None,
    poll_interval_seconds: float = 1.0,
    on_commit: Callable[[CommitResult], None] | None = None,
) -> CommitResult | None:
    """Run the watch-and-commit loop.

    * ``once=True``  → one poll, one commit (or one no-op), then
      return. This is the variant the CLI uses, and the unit test
      asserts on.
    * ``once=False`` → loop forever, polling every
      ``poll_interval_seconds``. Each detected change triggers a
      debounced commit.

    ``on_commit`` (optional) — invoked after each successful commit
    (whether from ``once=True`` or the daemon loop). Receives the
    ``CommitResult``. The dev21 ``agentchat-sync watch`` CLI uses
    this hook to push the just-landed commit to GitHub. Exceptions
    raised by ``on_commit`` are logged but do not stop the loop;
    callers that want hard failure semantics should raise inside
    ``on_commit`` and handle the consequence in their own daemon
    lifecycle (PID file removal, exit code, etc.).

    Returns the ``CommitResult`` of the final commit (whether from
    ``once=True`` or the last loop iteration in daemon mode). In
    daemon mode this returns ``None`` because the loop never ends.
    """
    stage = commit_stage or CommitStage(config)
    emitter = emitter or PollingEmitter(config)

    if once:
        change = emitter.poll()
        if change is None:
            # No new changes detected by the watcher. Fall back to a
            # git-status check so ``agentchat-sync-stage`` can still
            # be useful as a "commit any straggler changes" command.
            if has_uncommitted_changes(config.repo_dir):
                result = stage.run_once()
                _maybe_reset_baseline(emitter, result)
                if on_commit is not None and result.committed:
                    _safe_invoke_on_commit(on_commit, result)
                return result
            return None
        result = stage.run(change)
        _maybe_reset_baseline(emitter, result)
        if on_commit is not None and result.committed:
            _safe_invoke_on_commit(on_commit, result)
        return result

    # Daemon mode — the dev21 watch CLI owns the lockfile, push stage,
    # and signal handling on top of this loop. This function provides
    # the polling+debounce+commit primitives; the caller decides what
    # to do with the resulting CommitResult.
    log.info("watch_and_commit daemon mode starting (poll=%.1fs)", poll_interval_seconds)
    last_commit: CommitResult | None = None

    def _on_change(change: ChangeSet) -> None:
        nonlocal last_commit
        last_commit = _safe_commit(stage, emitter, change)
        if on_commit is not None and last_commit.committed:
            _safe_invoke_on_commit(on_commit, last_commit)

    debouncer = DebouncedEmitter(
        emitter,
        debounce_seconds=config.debounce_seconds,
        callback=_on_change,
    )
    try:
        while True:
            debouncer.poll()
            time.sleep(poll_interval_seconds)
    except KeyboardInterrupt:
        log.info("watch_and_commit interrupted; shutting down")
        debouncer.cancel()
        return last_commit


def _maybe_reset_baseline(emitter: ChangeEmitter, result: CommitResult) -> None:
    """After a successful commit, advance the polling emitter's baseline
    so the next poll diffs against the new HEAD rather than reporting
    the same changes again."""
    if not result.committed:
        return
    reset = getattr(emitter, "reset_baseline", None)
    if callable(reset):
        reset()


def _safe_commit(
    stage: CommitStage,
    emitter: ChangeEmitter,
    change: ChangeSet,
) -> CommitResult:
    try:
        result = stage.run(change)
    except Exception:
        log.exception("CommitStage raised; change set dropped: %s", change.summary_line())
        return CommitResult(
            committed=False,
            sha=None,
            message="",
            change_set=change,
            raw_stderr="exception during commit; see logs",
        )
    if result.committed:
        log.info("committed %s: %s", (result.sha or "?")[:8], change.summary_line())
        _maybe_reset_baseline(emitter, result)
    return result


def _safe_invoke_on_commit(
    on_commit: Callable[[CommitResult], None],
    result: CommitResult,
) -> None:
    """Invoke the user-supplied ``on_commit`` callback with a guard.

    Exceptions raised by ``on_commit`` are logged but do not propagate.
    The rationale: the watch loop is responsible for keeping the
    daemon alive across long stretches of quiet, and a downstream
    push failure must not be allowed to kill the polling+commit
    pipeline. Callers that need to react hard to ``on_commit``
    failure should signal that through their own state (a metric,
    an audit log entry, a callback that sends a Telegram message,
    etc.) rather than by raising — raising here would terminate
    the daemon.
    """
    try:
        on_commit(result)
    except Exception:
        log.exception(
            "on_commit callback raised after commit %s; daemon continues",
            (result.sha or "?")[:8],
        )
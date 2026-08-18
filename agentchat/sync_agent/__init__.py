"""
agentchat v1.2 — Sync agent change detection + local commit (v1.2.0.dev20).

This is the **first half** of the GitHub sync pipeline (see
``docs/design/dev20/sync-agent.md``). It watches a workspace + memory
tree, debounces events, and produces a local git commit summarising
the change set. **It does NOT push** — push is the responsibility of
``sync_agent.push`` (delivered in t_11537e05).

Design constraints:

* stdlib only at runtime (matches the rest of agentchat; ``pyproject.toml``
  declares ``dependencies = []``).
* Cross-platform. The detection loop uses ``os.scandir`` polling rather
  than inotify/FSEvents, so it works on Linux, macOS, and Windows without
  any extra wheels. Polling is the primary path; the design calls this
  the "watchdog" fallback because it is also what recovers a dead
  inotify watcher. The ``ChangeEmitter`` abstraction below is the seam
  where a future ``watchdog``-backed emitter can plug in.
* Idempotent. Running the commit stage back-to-back without new changes
  produces no new commits. ``git status --porcelain`` is the source of
  truth for "is there anything new?".
* No subprocess shell strings. Every ``git`` invocation is a list passed
  to ``subprocess.run`` with ``check=False`` so we can classify errors.

Public surface:

* ``SyncConfig`` — dataclass with ``repo_dir``, ``debounce_seconds``,
  ``exclude`` (patterns), and optional ``author_name``/``author_email``
  overrides.
* ``ChangeSet`` — typed change record (added/modified/deleted/renamed).
* ``ChangeEmitter`` — abstraction; default impl is ``PollingEmitter``.
* ``CommitStage`` — ``git add -A`` + ``git commit`` with a structured
  message.
* ``watch_and_commit(config, on_change, once=False)`` — orchestrator
  helper that ties polling + debounce + commit together. ``once=True``
  is the one-shot variant used by the CLI and the unit tests.

Module surface kept deliberately small. ``push.py`` and
``orchestrator.py`` will plug into the same primitives when
t_11537e05 and t_0105ff20 land.
"""

from .config import SyncConfig, DEFAULT_EXCLUDE
from .commit import (
    ChangeSet,
    ChangeRecord,
    CommitResult,
    build_commit_message,
    collect_changes,
    has_uncommitted_changes,
    CommitStage,
)
from .watcher import (
    ChangeEmitter,
    PollingEmitter,
    DebouncedEmitter,
    watch_and_commit,
)

__all__ = [
    "SyncConfig",
    "DEFAULT_EXCLUDE",
    "ChangeSet",
    "ChangeRecord",
    "CommitResult",
    "build_commit_message",
    "collect_changes",
    "has_uncommitted_changes",
    "CommitStage",
    "ChangeEmitter",
    "PollingEmitter",
    "DebouncedEmitter",
    "watch_and_commit",
]
"""
Sync agent configuration.

Kept as a plain dataclass (no Pydantic) so the module stays
stdlib-only. The orchestrator in t_0105ff20 will likely load this from
a TOML/JSON file; for now the unit tests construct it inline.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Iterable


# Default ignore patterns. The design (t_08bd1def §2) called these out
# explicitly. Keep the list conservative — better to skip a little too
# much than to push secrets or generated noise.
DEFAULT_EXCLUDE: tuple[str, ...] = (
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".DS_Store",
    "*.pyc",
    "*.pyo",
    "*.swp",
    "*.swo",
    "*.tmp",
    "*.temp",
    "*.log",
    # The mirror-tree that sync_github.py builds in tmp dirs (one-shot
    # CLI flow). If the operator runs both flows side by side we don't
    # want one to push the other's scratch directory.
    ".sync-mirror",
    ".agentchat-sync-mirror",
)


@dataclasses.dataclass(frozen=True)
class SyncConfig:
    """All the knobs the change-detection + commit stage needs.

    ``watched_roots`` is the list of filesystem trees to scan. In the
    dev20 layout that's typically::

        watched_roots = ("/home/waynec/agentchat", "~/.hermes/memory/agents", "~/.hermes/memory/team")

    tilde-expansion is honoured at construction time.
    """

    repo_dir: Path
    watched_roots: tuple[Path, ...] = ()
    debounce_seconds: float = 5.0
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE
    author_name: str | None = None
    author_email: str | None = None
    # When True, the polling emitter also includes hidden files
    # (dotfiles). Default False because dotfiles are usually config
    # noise. The ``.gitignore`` itself is always picked up.
    include_hidden: bool = False

    def __post_init__(self) -> None:
        # Path-ify + expanduser, in-place is fine because the dataclass
        # is frozen (we replace the field via object.__setattr__).
        object.__setattr__(self, "repo_dir", Path(self.repo_dir).expanduser().resolve())
        roots = tuple(Path(r).expanduser().resolve() for r in self.watched_roots)
        object.__setattr__(self, "watched_roots", roots)
        # ``exclude`` stays a tuple of strings; they're matched as
        # glob-style fnmatch patterns against each path component.

    def iter_watched(self) -> Iterable[Path]:
        """Yield each watched root that actually exists on disk."""
        for root in self.watched_roots:
            if root.exists():
                yield root
"""
agentchat v1.2 — single-file memory import for the Add Agent flow.

This complements :func:`agentchat.memory.import_memory` (which expects
a snapshot *directory*) by handling the common case of a single markdown
file pasted into the UI or uploaded as part of "Add agent with existing
memory".

Public surface:

  * :class:`ParseResult` — return type of :func:`parse_memory_md`.
  * :class:`ImportResult` — return type of :func:`import_text` /
    :func:`import_file`.
  * :func:`parse_memory_md` — pure parse, no I/O, used by the preview
    endpoint and the test suite.
  * :func:`import_text` — atomic write to
    ``~/.hermes/memory/{agent}.md`` with a ``.bak`` if the file exists.
  * :func:`import_file` — opens a file on disk and delegates to
    :func:`import_text`.

Size policy:

  * Inline paste (request body): 64 KiB (matches ``MAX_BODY_BYTES``).
  * Multipart upload: 256 KiB (covers the rare chat-history-as-memory
    case without bloating a normal API call).
  * Both are enforced at the handler layer; this module just rejects
    oversize input defensively.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import memory as memory_store


# --------------------------------------------------------------------------- #
# Size policy
# --------------------------------------------------------------------------- #

MAX_INLINE_BYTES = 64 * 1024       # matches MAX_BODY_BYTES on the bridge
MAX_UPLOAD_BYTES = 256 * 1024      # generous for chat-history-as-memory


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class MemoryImportError(Exception):
    """Base class for all import failures."""


class OversizeInput(MemoryImportError):
    """The incoming text exceeds the configured cap."""

    def __init__(self, size: int, cap: int) -> None:
        super().__init__(
            f"input is {size} bytes, cap is {cap} bytes"
        )
        self.size = size
        self.cap = cap


class InvalidAgentName(MemoryImportError):
    """The target agent name failed validation."""


# --------------------------------------------------------------------------- #
# Parse result
# --------------------------------------------------------------------------- #

@dataclass
class ParsedSection:
    title: Optional[str]      # None = intro / H1 preamble
    lines: list[str] = field(default_factory=list)
    line_count: int = 0       # matches len(lines); for quick UI display

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "lines": list(self.lines),
            "line_count": self.line_count,
        }


@dataclass
class ParseResult:
    sections: list[ParsedSection] = field(default_factory=list)
    section_titles: list[str] = field(default_factory=list)  # non-None, ordered
    intro_line_count: int = 0
    total_line_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sections": [s.to_dict() for s in self.sections],
            "section_titles": list(self.section_titles),
            "intro_line_count": self.intro_line_count,
            "total_line_count": self.total_line_count,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- #
# Import result
# --------------------------------------------------------------------------- #

@dataclass
class ImportResult:
    agent: str
    sections_imported: int
    lines_imported: int
    backup_path: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    bytes_written: int = 0

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "sections_imported": self.sections_imported,
            "lines_imported": self.lines_imported,
            "backup_path": self.backup_path,
            "warnings": list(self.warnings),
            "bytes_written": self.bytes_written,
        }


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _validate_agent_name(name: str) -> None:
    """Mirror of :func:`agentchat.memory._validate_agent` rules.

    Letters, digits, ``-``, ``_``; non-empty; no leading dot/dash.
    """
    if not name:
        raise InvalidAgentName("agent name is empty")
    if not all(c.isalnum() or c in "-_" for c in name):
        raise InvalidAgentName(
            f"agent name {name!r} must be alphanumeric (with - or _)"
        )
    if name.startswith((".", "-", "_")):
        raise InvalidAgentName(
            f"agent name {name!r} must not start with '.', '-', or '_'"
        )


def parse_memory_md(text: str) -> ParseResult:
    """Parse a memory.md body into sections + counts + warnings.

    Pure function — no I/O, safe to call from the preview endpoint.
    """
    result = ParseResult()
    if not text or not text.strip():
        result.warnings.append("input is empty")
        return result

    pairs = memory_store._split_sections(text)
    if not pairs:
        result.warnings.append("no sections detected")
        return result

    for title, body in pairs:
        # Skip wholly-empty sections but count their heading for the
        # title list — that's a feature, not a bug.
        lines = [
            stripped for stripped in (ln.strip() for ln in body.splitlines())
            if stripped
        ]
        sec = ParsedSection(
            title=title,
            lines=lines,
            line_count=len(lines),
        )
        # Detect blank lines inside a section (between non-empty lines)
        # — that's a parser warning, not a hard error.
        if lines:
            raw_stripped = [ln.strip() for ln in body.splitlines()]
            non_empty_idxs = [i for i, ln in enumerate(raw_stripped) if ln]
            for i in range(len(non_empty_idxs) - 1):
                gap = non_empty_idxs[i + 1] - non_empty_idxs[i]
                if gap > 1:
                    result.warnings.append(
                        f"section {title!r}: blank line(s) inside section "
                        f"(will be collapsed on save)"
                    )
                    break

        result.sections.append(sec)
        result.total_line_count += sec.line_count
        if title is None:
            result.intro_line_count = sec.line_count
        else:
            result.section_titles.append(title)

    # Detect duplicate section names — deterministic last-wins on save.
    seen: dict[str, int] = {}
    for t in result.section_titles:
        seen[t.lower()] = seen.get(t.lower(), 0) + 1
    dups = sorted(t for t, c in seen.items() if c > 1)
    for d in dups:
        result.warnings.append(
            f"duplicate section title (case-insensitive): {d!r} — "
            f"last occurrence will win on save"
        )

    return result


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #

def _make_backup(target: Path) -> Optional[Path]:
    """Snapshot the existing target to ``<target>.<UTC>.bak``. Returns None
    if target doesn't exist (no-op)."""
    if not target.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = target.with_name(f"{target.name}.{stamp}.bak")
    # Avoid collisions if two imports happen in the same second.
    counter = 1
    while bak.exists():
        bak = target.with_name(f"{target.name}.{stamp}-{counter}.bak")
        counter += 1
    shutil.copy2(target, bak)
    return bak


def import_text(
    agent_name: str,
    text: str,
    *,
    max_bytes: int = MAX_INLINE_BYTES,
) -> ImportResult:
    """Write ``text`` to ``~/.hermes/memory/{agent_name}.md`` atomically.

    Backs up any existing file. Returns counts. Validates size + name.
    """
    _validate_agent_name(agent_name)
    if not isinstance(text, str):
        raise MemoryImportError(f"text must be str, got {type(text).__name__}")
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        raise OversizeInput(size=len(encoded), cap=max_bytes)

    parsed = parse_memory_md(text)

    target = memory_store.agent_memory_path(agent_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = _make_backup(target)

    # Use the existing write_agent helper which already does
    # fcntl.flock + atomic write.
    memory_store.write_agent(agent_name, text if text.endswith("\n") else text + "\n")

    return ImportResult(
        agent=agent_name,
        sections_imported=len(parsed.section_titles),
        lines_imported=parsed.total_line_count,
        backup_path=str(backup) if backup else None,
        warnings=parsed.warnings,
        bytes_written=len(encoded),
    )


def import_file(
    agent_name: str,
    source: Path,
    *,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> ImportResult:
    """Read a file on disk and delegate to :func:`import_text`.

    ``source`` must be a regular file under ``max_bytes``. Symlinks are
    resolved before the size check to avoid TOCTOU on the size cap.
    """
    _validate_agent_name(agent_name)
    src = Path(source).resolve()
    if not src.is_file():
        raise MemoryImportError(f"source not a file: {source}")
    size = src.stat().st_size
    if size > max_bytes:
        raise OversizeInput(size=size, cap=max_bytes)
    text = src.read_text(encoding="utf-8")
    return import_text(agent_name, text, max_bytes=max_bytes)

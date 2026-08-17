"""
agentchat v1.2 — Memory store (per-agent + shared team + project tiers).

Tiered filesystem layout under ``~/.hermes/memory/`` (overridable via
``AGENTCHAT_MEMORY_DIR``):

    memory/
      agents/<name>/MEMORY.md        # private facts only this agent reads
      team/SHARED.md                 # shared team knowledge (all agents r/w)
      team/focus.json                # structured: each agent's active focus
      projects/<slug>/NOTES.md       # per-project notes (r/w by relevant agents)
      archive/<date>/                # snapshots for hydration / import

Each tier has the same primitive ops: ``read``, ``write`` (atomic), ``append``,
and ``snapshot``. Tier-specific helpers (``set_focus``, ``team_append``) live
at the bottom.

Atomic writes use ``Path.replace()`` after a write to a sibling tmp file, so a
crash mid-write never produces a torn file. Appends use ``Path.open("a")`` and
are protected by a single-process ``FileLock`` (``fcntl``) — we don't expect
multiple writers within the same process, but cross-process safety is
mandatory because Chappy writes from a separate process.

The store does NOT enforce a content schema. Markdown is the lingua franca
because agents and humans both read/write it, and ``session_search`` /
ripgrep already index it. ``focus.json`` is the only structured file, because
agents need machine-readable live state.

Stdlib only.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def memory_root() -> Path:
    """Root of the memory store. Override via ``AGENTCHAT_MEMORY_DIR``."""
    override = os.environ.get("AGENTCHAT_MEMORY_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes" / "memory"


def agents_dir() -> Path:
    return memory_root() / "agents"


def team_dir() -> Path:
    return memory_root() / "team"


def projects_dir() -> Path:
    return memory_root() / "projects"


def archive_dir() -> Path:
    return memory_root() / "archive"


AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,63}$")
PROJECT_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,63}$")


# --------------------------------------------------------------------------- #
# Low-level atomic IO
# --------------------------------------------------------------------------- #

@contextmanager
def _file_lock(path: Path, exclusive: bool = True) -> Iterator[None]:
    """Cross-process advisory lock via ``fcntl.flock``. Always blocks briefly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = open(lock_path, "w")
    try:
        # Lock; 5s timeout to avoid wedging on stuck writers.
        for _ in range(50):
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                break
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise
                time.sleep(0.1)
        else:
            raise TimeoutError(f"could not acquire lock on {lock_path}")
        yield
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()


def atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (via tmp + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------------------- #
# Per-agent private memory
# --------------------------------------------------------------------------- #

def _validate_agent(name: str) -> None:
    if not AGENT_NAME_RE.match(name):
        raise ValueError(f"invalid agent name: {name!r}")


def agent_memory_path(name: str) -> Path:
    _validate_agent(name)
    return agents_dir() / name / "MEMORY.md"


def read_agent(name: str) -> str:
    return read_text(agent_memory_path(name))


def write_agent(name: str, content: str) -> None:
    p = agent_memory_path(name)
    with _file_lock(p):
        atomic_write_text(p, content)


def list_agent_sections(name: str) -> list[dict[str, Any]]:
    """Return the agent's memory as a list of sections for the memory-UX UI.

    Each section is ``{"title": str, "lines": [str, ...], "index": int}``.
    Lines are stripped; blank lines are dropped.  Section ``index`` is the
    stable order in the source markdown (used as ``:key`` for live editing).
    The first H1 preamble (if any) is returned under title ``None`` so the
    UI can render the agent display name + intro separately.
    """
    raw = read_agent(name)
    parsed = _split_sections(raw)
    sections: list[dict[str, Any]] = []
    for idx, (title, body) in enumerate(parsed):
        lines = [
            stripped for stripped in (ln.strip() for ln in body.splitlines())
            if stripped
        ]
        sections.append({"title": title, "lines": lines, "index": idx})
    return sections


def replace_agent_section(name: str, section_title: str, new_lines: list[str]) -> None:
    """Replace the body of a single ``## section`` with ``new_lines``.

    Lines are written verbatim (no extra escaping) so the UI controls
    display/escape.  Creates the section if it doesn't already exist.
    """
    p = agent_memory_path(name)
    with _file_lock(p):
        existing = read_text(p)
        # Reuse the merge helper: build a tiny incoming doc with just this
        # section, then merge into the existing body.  That gives us
        # "create-if-missing" behaviour for free and keeps the rest of the
        # document intact.
        incoming = f"## {section_title}\n" + "\n".join(new_lines) + "\n"
        # Replace this section's body by removing existing section, then
        # appending under-section.  Simpler: use _append_under_section for
        # empty body, then write_agent the result.  But append creates
        # duplicate lines on repeated calls — instead, build fresh content.
        new_md = _replace_section_in_md(existing, section_title, new_lines)
        atomic_write_text(p, new_md)


def _replace_section_in_md(md: str, section_title: str, new_lines: list[str]) -> str:
    """Return ``md`` with ``## section_title`` body replaced by ``new_lines``.

    Creates the section if absent.  Preserves H1 preamble and other
    sections untouched.  Lines are joined with single newlines; trailing
    newline added so the file ends cleanly.
    """
    if not md.strip():
        return f"## {section_title}\n" + "\n".join(new_lines) + "\n"

    # Find the H2 for this title.
    h2_pat = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
    matches = list(h2_pat.finditer(md))
    target_idx = None
    target_match = None
    for i, m in enumerate(matches):
        if m.group(1).strip().lower() == section_title.strip().lower():
            target_idx = i
            target_match = m
            break

    if target_match is None:
        # Section doesn't exist — append at end.
        new_block = f"\n## {section_title}\n\n" + "\n".join(new_lines) + "\n"
        if not md.endswith("\n"):
            md = md + "\n"
        return md + new_block

    # Replace body of target section: from end of heading line to start of next H2 (or EOF).
    body_start = target_match.end()
    if target_match is None:
        return md
    if target_idx is None:
        return md
    if target_idx + 1 < len(matches):
        body_end = matches[target_idx + 1].start()
    else:
        body_end = len(md)

    new_body = "\n" + "\n".join(new_lines) + "\n\n" if new_lines else "\n"
    return md[:body_start] + new_body + md[body_end:]


def remove_agent_line(name: str, section_title: str, line_index: int) -> bool:
    """Delete one line (by index) from ``## section_title``.

    Returns True if a line was removed, False if the section or line
    didn't exist.  Indexes are 0-based and refer to the order in
    ``list_agent_sections(name)``.
    """
    sections = list_agent_sections(name)
    section = next(
        (s for s in sections
         if s["title"] is not None and s["title"].strip().lower() == section_title.strip().lower()),
        None,
    )
    if section is None or line_index >= len(section["lines"]):
        return False
    new_lines = [ln for i, ln in enumerate(section["lines"]) if i != line_index]
    replace_agent_section(name, section_title, new_lines)
    return True


def append_agent(name: str, section: str, line: str) -> None:
    """Append ``line`` under a Markdown ``## section`` heading.

    Creates the heading if missing. Useful for incremental notes like:
    ``append_agent("hermes", "Today", "Sent PSP pack to Chappy")``.
    """
    p = agent_memory_path(name)
    with _file_lock(p):
        existing = read_text(p)
        updated = _append_under_section(existing, section, line)
        atomic_write_text(p, updated)


def list_agents() -> list[str]:
    d = agents_dir()
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir())


# --------------------------------------------------------------------------- #
# Team shared memory — section-aware helpers (used by the HTTP bridge)
# --------------------------------------------------------------------------- #


def list_team_sections() -> list[dict[str, Any]]:
    """Return the team shared memory as a list of sections.

    Same shape as :func:`list_agent_sections` (title / lines / index) so the
    UI can render shared memory using the same drawer. The first H1
    preamble (if any) is returned under title ``None`` so the UI can
    render the team display name + intro separately.
    """
    raw = read_team()
    parsed = _split_sections(raw)
    sections: list[dict[str, Any]] = []
    for idx, (title, body) in enumerate(parsed):
        lines = [
            stripped for stripped in (ln.strip() for ln in body.splitlines())
            if stripped
        ]
        sections.append({"title": title, "lines": lines, "index": idx})
    return sections


def replace_team_section(section_title: str, new_lines: list[str]) -> None:
    """Replace the body of ``## section_title`` in the team shared memory.

    Creates the section if it doesn't already exist. Uses ``_file_lock``
    so concurrent writers from different agents don't stomp each other.
    """
    p = team_shared_path()
    with _file_lock(p):
        existing = read_text(p)
        new_md = _replace_section_in_md(existing, section_title, new_lines)
        atomic_write_text(p, new_md)


def append_team_line(section: str, line: str, *, author: str) -> None:
    """Append ``line`` under ``## section`` in the team shared memory.

    Uses the same attribution-trail pattern as ``append_team``: each line
    gets a ``— author @ ISO`` sub-tail so multi-agent writes are
    traceable.
    """
    p = team_shared_path()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    attribution = f"  \n<sub>— {author} @ {ts}</sub>" if line else ""
    with _file_lock(p):
        existing = read_text(p)
        updated = _append_under_section(existing, section, line + attribution)
        atomic_write_text(p, updated)


def remove_team_line(section: str, line_index: int) -> bool:
    """Delete one line (by index) from a shared section.

    Returns True if a line was removed, False if the section or line
    was not found. Lock-protected for concurrent writers.
    """
    p = team_shared_path()
    with _file_lock(p):
        existing = read_text(p)
        updated = _remove_line_in_section(existing, section, line_index)
        if updated == existing:
            return False
        atomic_write_text(p, updated)
        return True


def _remove_line_in_section(md: str, section_title: str, line_index: int) -> str:
    """Return ``md`` with one line removed from ``## section_title``.

    ``line_index`` is 0-based after the same stripping rules used by
    ``list_team_sections`` (blank lines dropped, whitespace stripped).
    Returns ``md`` unchanged when the section or line isn't found.
    """
    h2_pat = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
    matches = list(h2_pat.finditer(md))
    target = None
    for m in matches:
        if m.group(1).strip().lower() == section_title.strip().lower():
            target = m
            break
    if target is None:
        return md
    body_start = target.end()
    target_idx = matches.index(target)
    body_end = matches[target_idx + 1].start() if target_idx + 1 < len(matches) else len(md)
    body = md[body_start:body_end]
    lines = [
        ln for ln in (
            stripped for stripped in (l.strip() for l in body.splitlines())
        ) if ln
    ]
    if line_index < 0 or line_index >= len(lines):
        return md
    del lines[line_index]
    new_body = "\n" + "\n".join(lines) + "\n\n" if lines else "\n"
    return md[:body_start] + new_body + md[body_end:]


# --------------------------------------------------------------------------- #
# Team shared memory
# --------------------------------------------------------------------------- #

def team_shared_path() -> Path:
    """Path to team/SHARED.md (resolved fresh each call so AGENTCHAT_MEMORY_DIR
    changes are honored)."""
    return team_dir() / "SHARED.md"


def team_focus_path() -> Path:
    """Path to team/focus.json (resolved fresh each call)."""
    return team_dir() / "focus.json"


# Backwards-compatible module-level constants. Deprecated; use the
# team_shared_path() / team_focus_path() functions instead so changes to
# AGENTCHAT_MEMORY_DIR are picked up. These are kept so legacy callers
# (memory_bridge.py) don't break, but they resolve at import time using
# whatever AGENTCHAT_MEMORY_DIR was set then.
TEAM_SHARED_PATH = team_shared_path()
TEAM_FOCUS_PATH = team_focus_path()


def read_team() -> str:
    return read_text(team_shared_path())


def write_team(content: str) -> None:
    p = team_shared_path()
    with _file_lock(p):
        atomic_write_text(p, content)


def append_team(section: str, line: str, *, author: str) -> None:
    """Append ``line`` under ``## section`` with an ``— author @ ISO`` tail."""
    p = team_shared_path()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    attribution = f"  \n<sub>— {author} @ {ts}</sub>" if line else ""
    with _file_lock(p):
        existing = read_text(p)
        updated = _append_under_section(existing, section, line + attribution)
        atomic_write_text(p, updated)


# --------------------------------------------------------------------------- #
# Agent focus (structured live state)
# --------------------------------------------------------------------------- #

@dataclass
class AgentFocus:
    name: str
    focus: str = ""
    status: str = "idle"  # "active" | "idle" | "blocked"
    updated_at: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "focus": self.focus,
            "status": self.status,
            "updated_at": self.updated_at,
            "notes": self.notes,
        }


@dataclass
class FocusState:
    agents: dict[str, AgentFocus] = field(default_factory=dict)
    wayne_priorities: list[str] = field(default_factory=list)
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agents": {n: a.to_dict() for n, a in self.agents.items()},
            "wayne_priorities": list(self.wayne_priorities),
            "updated_at": self.updated_at,
        }


def _empty_focus() -> FocusState:
    return FocusState(updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def read_focus() -> FocusState:
    p = team_focus_path()
    if not p.exists():
        return _empty_focus()
    try:
        raw = json.loads(read_text(p))
    except Exception:
        return _empty_focus()
    state = _empty_focus()
    for name, info in (raw.get("agents") or {}).items():
        state.agents[name] = AgentFocus(
            name=name,
            focus=info.get("focus", ""),
            status=info.get("status", "idle"),
            updated_at=info.get("updated_at", ""),
            notes=info.get("notes", ""),
        )
    state.wayne_priorities = list(raw.get("wayne_priorities") or [])
    state.updated_at = raw.get("updated_at", state.updated_at)
    return state


def write_focus(state: FocusState) -> None:
    state.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    p = team_focus_path()
    with _file_lock(p):
        atomic_write_text(
            p,
            json.dumps(state.to_dict(), indent=2, ensure_ascii=False) + "\n",
        )


def set_focus(
    agent: str,
    *,
    focus: str,
    status: str = "active",
    notes: str = "",
) -> FocusState:
    """Update this agent's focus entry. Returns the new state."""
    _validate_agent(agent)
    state = read_focus()
    state.agents[agent] = AgentFocus(
        name=agent,
        focus=focus,
        status=status,
        updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        notes=notes,
    )
    write_focus(state)
    return state


def set_wayne_priorities(priorities: list[str]) -> FocusState:
    """Update Wayne's current priorities (visible to all agents)."""
    state = read_focus()
    state.wayne_priorities = list(priorities)
    write_focus(state)
    return state


# --------------------------------------------------------------------------- #
# Project memory
# --------------------------------------------------------------------------- #

def _validate_project(slug: str) -> None:
    if not PROJECT_SLUG_RE.match(slug):
        raise ValueError(f"invalid project slug: {slug!r}")


def project_notes_path(slug: str) -> Path:
    _validate_project(slug)
    return projects_dir() / slug / "NOTES.md"


def read_project(slug: str) -> str:
    return read_text(project_notes_path(slug))


def write_project(slug: str, content: str) -> None:
    p = project_notes_path(slug)
    with _file_lock(p):
        atomic_write_text(p, content)


def append_project(slug: str, section: str, line: str) -> None:
    p = project_notes_path(slug)
    with _file_lock(p):
        existing = read_text(p)
        updated = _append_under_section(existing, section, line)
        atomic_write_text(p, updated)


def list_projects() -> list[str]:
    d = projects_dir()
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir())


# --------------------------------------------------------------------------- #
# Archive / snapshot / import
# --------------------------------------------------------------------------- #

def snapshot(label: Optional[str] = None) -> Path:
    """Take a snapshot of all tiers into ``archive/<timestamp>-<label>/``.

    Returns the snapshot directory path.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = f"-{label}" if label else ""
    dest = archive_dir() / f"{stamp}{suffix}"
    dest.mkdir(parents=True, exist_ok=True)

    src_map = [
        ("agents", agents_dir()),
        ("team", team_dir()),
        ("projects", projects_dir()),
    ]
    for name, src in src_map:
        if not src.exists():
            continue
        target = dest / name
        target.mkdir(parents=True, exist_ok=True)
        # Copy files preserving relative paths. Don't follow symlinks.
        import shutil
        for root, _dirs, files in os.walk(src):
            rel = Path(root).relative_to(src)
            (target / rel).mkdir(parents=True, exist_ok=True)
            for f in files:
                if f.endswith(".lock") or f.endswith(".tmp"):
                    continue
                shutil.copy2(Path(root) / f, target / rel / f)

    return dest


def list_snapshots() -> list[Path]:
    d = archive_dir()
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.is_dir())


def import_memory(
    source: Path,
    *,
    target_agent: Optional[str] = None,
    target_project: Optional[str] = None,
    mode: str = "merge",
) -> dict[str, Any]:
    """Import a prior memory bundle into the live store.

    Args:
      source: directory produced by ``snapshot()``, or a single-agent export
              bundle (i.e. ``agents/<name>/`` from a snapshot).
      target_agent: if set, treat ``source`` as an agent memory bundle and
                    merge it into ``agents/<target_agent>/``.
      target_project: if set, treat ``source`` as a project notes bundle and
                      merge it into ``projects/<target_project>/``.
      mode: ``"merge"`` (append under the same headings) or ``"replace"``
            (overwrite the target tier wholesale).

    Use cases:
      - New agent joining agentchat: snapshot from another host, point
        ``target_agent`` at the new agent name, ``mode="merge"`` to bring
        their prior knowledge along.
      - Project handoff: snapshot a project from one agent's perspective,
        point ``target_project`` at the canonical slug, ``mode="replace"``
        when the old owner is decommissioned.
    """
    if not source.exists() or not source.is_dir():
        raise FileNotFoundError(f"source not found: {source}")
    if target_agent is None and target_project is None:
        raise ValueError("must specify target_agent or target_project")
    if target_agent is not None and target_project is not None:
        raise ValueError("specify only one of target_agent / target_project")

    summary = {"source": str(source), "mode": mode, "files_imported": []}

    if target_agent is not None:
        # Source should be a snapshot of one agent: source/agents/<name>/...
        candidates = list(source.glob("agents/*/MEMORY.md"))
        if not candidates:
            # Or directly an agent dir
            direct = source / "MEMORY.md"
            if direct.exists():
                candidates = [direct]
        if not candidates:
            raise FileNotFoundError(
                f"no MEMORY.md found under {source} (looked for agents/*/MEMORY.md)"
            )
        for c in candidates:
            body = c.read_text(encoding="utf-8")
            if mode == "replace":
                write_agent(target_agent, body)
            else:
                existing = read_agent(target_agent)
                merged = _merge_memory(existing, body)
                write_agent(target_agent, merged)
            summary["files_imported"].append(str(c))
        return summary

    if target_project is not None:
        candidates = list(source.glob("projects/*/NOTES.md"))
        if not candidates:
            direct = source / "NOTES.md"
            if direct.exists():
                candidates = [direct]
        if not candidates:
            raise FileNotFoundError(
                f"no NOTES.md found under {source}"
            )
        for c in candidates:
            body = c.read_text(encoding="utf-8")
            if mode == "replace":
                write_project(target_project, body)
            else:
                existing = read_project(target_project)
                merged = _merge_memory(existing, body)
                write_project(target_project, merged)
            summary["files_imported"].append(str(c))
        return summary

    return summary  # unreachable


def export_agent(name: str, dest: Optional[Path] = None) -> Path:
    """Bundle one agent's memory into a portable directory.

    The bundle contains ``agents/<name>/MEMORY.md`` so it can be re-imported
    with ``import_memory(source, target_agent=...)``.
    """
    _validate_agent(name)
    dest = dest or (Path.cwd() / f"agent-memory-{name}")
    bundle = dest / "agents" / name
    bundle.mkdir(parents=True, exist_ok=True)
    src = agent_memory_path(name)
    if src.exists():
        import shutil
        shutil.copy2(src, bundle / "MEMORY.md")
    return dest


# --------------------------------------------------------------------------- #
# Markdown helpers
# --------------------------------------------------------------------------- #

def _append_under_section(existing: str, section: str, line: str) -> str:
    """Append ``line`` under a Markdown ``## section`` heading. Create the
    section if missing. Preserves a leading H1 if present.
    """
    if not existing.strip():
        return f"## {section}\n\n{line}\n"

    # Find the section heading (case-insensitive, exact match).
    pattern = re.compile(
        rf"^(##\s+{re.escape(section)}\s*$)", re.IGNORECASE | re.MULTILINE
    )
    m = pattern.search(existing)
    if not m:
        # Append at the end under a new heading.
        sep = "" if existing.endswith("\n") else "\n"
        return existing + sep + f"\n## {section}\n\n{line}\n"

    # Find next heading of equal or higher level to know where the section ends.
    start = m.end()
    next_h = re.search(r"^(##\s|^\#\s)", existing[start:], re.MULTILINE)
    end = start + next_h.start() if next_h else len(existing)

    # Insert the new line before the next heading.
    head = existing[:end].rstrip() + "\n"
    tail = existing[end:]
    if not head.endswith("\n\n"):
        head += "\n"
    return head + line + "\n\n" + tail.lstrip("\n")


def _merge_memory(existing: str, incoming: str) -> str:
    """Merge two markdown bodies section-by-section. Incoming sections that
    don't exist are appended; existing sections keep their content; incoming
    sections that match are appended to the existing section body.
    """
    if not existing.strip():
        return incoming
    if not incoming.strip():
        return existing

    existing_sections = _split_sections(existing)
    incoming_sections = _split_sections(incoming)

    merged: list[tuple[Optional[str], str]] = []
    matched: set[str] = set()

    # First pass: walk existing in order. Append any incoming section that
    # has the same heading under the existing heading.
    for title, body in existing_sections:
        merged.append((title, body))
        if title is None:
            continue
        for inc_title, inc_body in incoming_sections:
            if inc_title is None:
                continue
            if inc_title.lower() == title.lower() and inc_body.strip():
                merged.append((None, inc_body.rstrip() + "\n"))
                matched.add(inc_title.lower())

    # Second pass: append incoming sections whose heading didn't exist.
    for inc_title, inc_body in incoming_sections:
        if inc_title is None:
            continue
        if inc_title.lower() in matched:
            continue
        merged.append((inc_title, inc_body))

    leading_h1 = None
    if existing_sections:
        first_title = existing_sections[0][0]
        if first_title and first_title.startswith("# ") and not first_title.startswith("## "):
            leading_h1 = first_title
    return _join_sections(merged, leading_h1=leading_h1)


_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _split_sections(md: str) -> list[tuple[Optional[str], str]]:
    """Split markdown into (heading-or-None, body) pairs.

    The first H1 (if any) is returned as the leading ``(title, body)`` pair
    where ``body`` is the intro text up to the first H2 (or the rest of
    the document if there are no H2 sections).
    """
    if not md.strip():
        return []

    # Find all H2 boundaries.
    h2_matches = list(_H2_RE.finditer(md))
    if not h2_matches:
        # No H2 — extract the H1 if present, otherwise the whole thing.
        h1_match = _H1_RE.search(md)
        if h1_match:
            return [(h1_match.group(1), md[h1_match.end():].lstrip("\n").rstrip("\n"))]
        return [(None, md.rstrip() + "\n")]

    sections = []
    # Preamble (H1 + intro before first H2).
    first = h2_matches[0]
    preamble = md[: first.start()]
    h1_match = _H1_RE.search(preamble)
    if h1_match:
        sections.append((h1_match.group(1), preamble[h1_match.end():].lstrip("\n")))
    else:
        sections.append((None, preamble.rstrip("\n")))

    for i, m in enumerate(h2_matches):
        title = m.group(1)
        body_start = m.end()
        body_end = h2_matches[i + 1].start() if i + 1 < len(h2_matches) else len(md)
        body = md[body_start:body_end].rstrip("\n")
        sections.append((title, body))

    return sections


def _join_sections(sections: list[tuple[Optional[str], str]], *, leading_h1: Optional[str]) -> str:
    out = []
    if leading_h1:
        out.append(f"# {leading_h1}\n")
    for title, body in sections:
        if title is None and not out:
            out.append(body.rstrip() + "\n")
        elif title is None:
            out.append("\n" + body.rstrip() + "\n")
        else:
            out.append(f"\n## {title}\n\n{body.rstrip()}\n")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# CLI shim — invoked by `python -m agentchat.memory ...` from package __main__
# --------------------------------------------------------------------------- #

def _cli(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="agentchat.memory", description="Memory store CLI.")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_ls = sub.add_parser("ls", help="list known agents / projects / snapshots")
    p_ls.add_argument("tier", choices=["agents", "projects", "snapshots"])
    p_ls.set_defaults(func=lambda a: _do_ls(a))

    p_cat = sub.add_parser("cat", help="print an agent's / project's / team's memory")
    p_cat.add_argument("tier", choices=["agent", "project", "team"])
    p_cat.add_argument("name", nargs="?")
    p_cat.set_defaults(func=lambda a: _do_cat(a))

    p_add = sub.add_parser("append", help="append a line under a section heading")
    p_add.add_argument("tier", choices=["agent", "team", "project"])
    p_add.add_argument("name")
    p_add.add_argument("section")
    p_add.add_argument("line")
    p_add.add_argument("--author", default="anon")
    p_add.set_defaults(func=lambda a: _do_append(a))

    p_focus = sub.add_parser("focus", help="set or read agent focus")
    p_focus.add_argument("agent")
    p_focus.add_argument("text", nargs="?", default=None)
    p_focus.add_argument("--status", default="active", choices=["active", "idle", "blocked"])
    p_focus.add_argument("--notes", default="")
    p_focus.set_defaults(func=lambda a: _do_focus(a))

    p_priorities = sub.add_parser("priorities", help="set Wayne's current priorities")
    p_priorities.add_argument("text", nargs="*", help="priority lines")
    p_priorities.set_defaults(func=lambda a: _do_priorities(a))

    p_snap = sub.add_parser("snapshot", help="take a snapshot of all tiers")
    p_snap.add_argument("--label", default=None)
    p_snap.set_defaults(func=lambda a: _do_snapshot(a))

    p_export = sub.add_parser("export-agent", help="bundle an agent's memory for transport")
    p_export.add_argument("agent")
    p_export.add_argument("--dest", default=None)
    p_export.set_defaults(func=lambda a: _do_export(a))

    p_import = sub.add_parser("import", help="import a snapshot bundle into an agent or project")
    p_import.add_argument("source")
    p_import.add_argument("--as-agent", dest="agent", default=None)
    p_import.add_argument("--as-project", dest="project", default=None)
    p_import.add_argument("--mode", choices=["merge", "replace"], default="merge")
    p_import.set_defaults(func=lambda a: _do_import(a))

    # ----- `init` -----------------------------------------------------------
    # Bring a new agent online with prior memories in one shot.  Takes a
    # snapshot bundle (or single-agent export dir) and merges it under the
    # agent's MEMORY.md.  Designed for the bootstrap flow that ships with
    # t_fe4deb6d — used by both the bridge /v1/ui/memory/import endpoint
    # and the "Import memories" button on /settings.
    p_init = sub.add_parser(
        "init",
        help="bootstrap a new agent by importing prior memories from a bundle",
    )
    p_init.add_argument("--agent", required=True, help="target agent name")
    p_init.add_argument(
        "--import-from",
        dest="source",
        required=True,
        help="path to a snapshot dir OR a single-agent export bundle",
    )
    p_init.add_argument(
        "--mode",
        choices=["merge", "replace"],
        default="merge",
        help="merge (default, append under existing sections) or replace (overwrite target tier)",
    )
    p_init.add_argument(
        "--no-archive",
        action="store_true",
        help="skip the pre-import archive snapshot (default: snapshot live state first for safety)",
    )
    p_init.add_argument(
        "--create-if-missing",
        action="store_true",
        help="create an empty MEMORY.md for the target agent before importing (default: fail if missing)",
    )
    p_init.set_defaults(func=lambda a: _do_init(a))

    args = p.parse_args(argv)
    return args.func(args)


def _do_ls(args: argparse.Namespace) -> int:
    if args.tier == "agents":
        for n in list_agents():
            print(n)
    elif args.tier == "projects":
        for n in list_projects():
            print(n)
    elif args.tier == "snapshots":
        for p in list_snapshots():
            print(p)
    return 0


def _do_cat(args: argparse.Namespace) -> int:
    if args.tier == "team":
        print(read_team())
    elif args.tier == "agent":
        if not args.name:
            print("agent name required", file=__import__("sys").stderr)
            return 2
        print(read_agent(args.name))
    elif args.tier == "project":
        if not args.name:
            print("project slug required", file=__import__("sys").stderr)
            return 2
        print(read_project(args.name))
    return 0


def _do_append(args: argparse.Namespace) -> int:
    if args.tier == "agent":
        append_agent(args.name, args.section, args.line)
    elif args.tier == "team":
        append_team(args.section, args.line, author=args.author)
    elif args.tier == "project":
        append_project(args.name, args.section, args.line)
    return 0


def _do_focus(args: argparse.Namespace) -> int:
    if args.text is None:
        state = read_focus()
        a = state.agents.get(args.agent)
        if not a:
            print(f"(no focus for {args.agent})")
            return 0
        print(f"{args.agent} [{a.status}]: {a.focus}")
        if a.notes:
            print(f"  notes: {a.notes}")
        return 0
    set_focus(args.agent, focus=args.text, status=args.status, notes=args.notes)
    print(f"focus updated for {args.agent}")
    return 0


def _do_priorities(args: argparse.Namespace) -> int:
    set_wayne_priorities(list(args.text))
    print(f"priorities updated ({len(args.text)} items)")
    return 0


def _do_snapshot(args: argparse.Namespace) -> int:
    dest = snapshot(args.label)
    print(f"snapshot: {dest}")
    return 0


def _do_export(args: argparse.Namespace) -> int:
    dest = Path(args.dest) if args.dest else None
    out = export_agent(args.agent, dest)
    print(f"exported: {out}")
    return 0


def _do_import(args: argparse.Namespace) -> int:
    summary = import_memory(
        Path(args.source),
        target_agent=args.agent,
        target_project=args.project,
        mode=args.mode,
    )
    print(json.dumps(summary, indent=2))
    return 0


def _do_init(args: argparse.Namespace) -> int:
    """Bootstrap a new agent by importing prior memories.

    Flow:
      1. Validate target agent name + source path.
      2. Optionally take an archive snapshot of live state first (safety net).
      3. Create an empty MEMORY.md for the target agent if requested.
      4. Delegate to ``import_memory(source, target_agent=..., mode=...)``.
      5. Print a JSON summary with files imported and any archive path.

    Exits 0 on success, 2 on validation error, 3 on import failure.
    """
    import sys

    target_agent: str = args.agent
    source: Path = Path(args.source)
    mode: str = args.mode

    # 1. validate target agent
    try:
        _validate_agent(target_agent)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # 2. validate source
    if not source.exists() or not source.is_dir():
        print(f"error: source not found or not a directory: {source}", file=sys.stderr)
        return 2

    # 3. create-if-missing
    target_path = agent_memory_path(target_agent)
    if not target_path.exists():
        if not args.create_if_missing:
            print(
                f"error: target agent '{target_agent}' has no MEMORY.md. "
                f"Pass --create-if-missing to bootstrap from empty, or "
                f"use 'append' first to give the agent some context.",
                file=sys.stderr,
            )
            return 2
        write_agent(target_agent, f"# {target_agent} — Agent Memory\n")

    # 4. optional archive
    archive = None
    if not args.no_archive:
        archive = snapshot(label=f"pre-init-{target_agent}-{datetime.now(timezone.utc).strftime('%H%M%S')}")

    # 5. delegate
    try:
        summary = import_memory(source, target_agent=target_agent, mode=mode)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"error: import failed: {e}", file=sys.stderr)
        return 3

    summary["target_agent"] = target_agent
    summary["mode"] = mode
    if archive is not None:
        summary["archive"] = str(archive)
    print(json.dumps(summary, indent=2))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    import sys
    if argv is None:
        argv = sys.argv[1:]
    return _cli(argv)


if __name__ == "__main__":
    import sys
    sys.exit(main())
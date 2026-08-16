"""
agentchat.bot.memory_bridge — thin wrapper around :mod:`agentchat.memory`.

The bot speaks "Telegram-shaped" data on the boundary and
"agentchat-shaped" markdown on disk. This module owns that translation:

- Auto-key generation for ``/remember`` and ``/share`` per §3.1 of the
  slash-command contract.
- Envelope construction for private notes per §3.2.
- Per-user private bucket resolution: ``agents/telegram:<user_id>/MEMORY.md``.
- Project bucket auto-creation for first ``/remember_for`` write.
- Listing / searching helpers for ``/notes`` and ``/recall``.

The bot **does not** route through the HTTP API or the record-oriented
``memory_store.py`` — that's a v1.1 add-on. v1 is markdown layer only.

All functions are synchronous because :mod:`agentchat.memory` is stdlib
I/O and the python-telegram-bot handlers run them via ``asyncio.to_thread``
in :mod:`agentchat.bot.commands`. Keeping the bridge synchronous makes it
trivial to unit-test against a temp ``AGENTCHAT_MEMORY_DIR``.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from agentchat import memory
from agentchat.bot import auth

log = logging.getLogger("agentchat.bot.memory_bridge")

# --------------------------------------------------------------------------- #
# Auto-key format (contract §3.1)
# --------------------------------------------------------------------------- #
# Lexicographic == chronological because ISO-8601 in UTC sorts as a string.
# 8 hex chars of SHA-256 = 2^32 ≈ 4B; per-second collision probability is
# negligible and `metadata.created_at` disambiguates any residual.
_AUTO_KEY_RE = re.compile(r"^note-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z-[0-9a-f]{8}$")
_MAX_TEXT_CHARS = 4096  # default; see TELEGRAM_MAX_TEXT_CHARS
_MAX_KEY_CHARS = 64


@dataclass(frozen=True)
class WriteResult:
    """A successful /remember or /share write."""

    key: str
    bucket_path: Path
    text_length: int
    created_at: str  # ISO-8601 UTC


@dataclass(frozen=True)
class ProjectWriteResult:
    """A successful /remember_for write."""

    key: str
    bucket_path: Path
    text_length: int
    created_bucket: bool  # True iff this is the first write to that project


@dataclass(frozen=True)
class ForgetResult:
    """Result of a /forget call."""

    key: str
    target_path: Path
    deleted: bool  # True iff something was actually removed


@dataclass(frozen=True)
class NoteEntry:
    """A parsed line from a private bucket MEMORY.md (for /notes + /recall)."""

    key: str
    created_at: str
    text: str
    source_path: Path


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    """UTC ISO-8601 with second precision and trailing Z (no microseconds)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_key_stamp() -> str:
    """UTC stamp formatted for the auto-key (``-`` instead of ``:`` so it's path-safe)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _auto_key(text: str, *, stamp: Optional[str] = None) -> str:
    """Generate ``note-<stamp>-<8hex>`` per contract §3.1."""
    stamp = stamp or _now_key_stamp()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"note-{stamp}-{digest}"


def _envelope(auto_key: str, created_at: str, agent_name: str, telegram_user_id: int, telegram_username: Optional[str], text: str) -> str:
    """Markdown entry shape from contract §3.2 (private + shared notes).

    Two-space-indented text body so Markdown's line-break rule keeps multi-line
    notes readable.
    """
    username_line = f"telegram_username: {telegram_username}\n" if telegram_username else "telegram_username: null\n"
    # Indent each line of the text body by two spaces.
    indented = "\n".join("  " + line for line in text.split("\n"))
    return (
        f"## {auto_key}\n"
        f"created_at: {created_at}\n"
        f"created_by: {agent_name}\n"
        f"telegram_user_id: {telegram_user_id}\n"
        f"{username_line}"
        f"text: |\n"
        f"{indented}\n"
    )


def _section_for_key(key: str) -> str:
    """The H2 heading a private note lives under.

    We use ``"Notes"`` as the catch-all section heading so private bucket
    entries appear under one chronological log rather than fragmenting by
    auto-key. ``/notes`` sorts by ``created_at`` regardless of section.
    """
    return "Notes"


def _private_bucket_path(agent_name: str) -> Path:
    """Path to a Telegram user's private MEMORY.md.

    The canonical agent_name may contain a colon (``telegram:<uid>``)
    which :func:`agentchat.memory.agent_memory_path` rejects via its
    strict name regex. We replace the colon with a hyphen so the bucket
    resolves to a real directory; the canonical name is preserved in
    the envelope's ``created_by`` field for display / audit.
    """
    fs_safe = agent_name.replace(":", "-")
    return memory.agent_memory_path(fs_safe)


def _fs_safe_agent(agent_name: str) -> str:
    """Return the on-disk-safe form of ``agent_name`` (colons → dashes)."""
    return agent_name.replace(":", "-")


def _project_path(slug: str) -> Path:
    """Path to a project's NOTES.md."""
    return memory.project_notes_path(slug)


# --------------------------------------------------------------------------- #
# Write API — called by /remember, /share, /remember_for
# --------------------------------------------------------------------------- #

def remember_private(
    agent_name: str,
    text: str,
    *,
    telegram_user_id: int,
    telegram_username: Optional[str],
    max_chars: int = _MAX_TEXT_CHARS,
) -> WriteResult:
    """Append a note to ``agents/<agent_name>/MEMORY.md``.

    Raises ``ValueError`` on length issues (caller maps to Telegram error
    text). The on-disk layout is one section ``## Notes`` with each
    entry as a sub-block. ``auto_key`` is generated here; we do **not**
    expose it as an arg because Telegram users never supply it.
    """
    text = text.strip()
    if not text:
        raise ValueError("empty text")
    if len(text) > max_chars:
        raise ValueError(f"text too long ({len(text)} > {max_chars})")

    created_at = _now_iso()
    auto_key = _auto_key(text)
    envelope = _envelope(
        auto_key=auto_key,
        created_at=created_at,
        agent_name=agent_name,
        telegram_user_id=telegram_user_id,
        telegram_username=telegram_username,
        text=text,
    )
    fs_name = _fs_safe_agent(agent_name)
    path = _private_bucket_path(fs_name)
    memory.append_agent(fs_name, _section_for_key(auto_key), envelope)

    log.info(
        "remember_private agent=%s uid=%s key=%s len=%d",
        agent_name, telegram_user_id, auto_key, len(text),
    )
    return WriteResult(
        key=auto_key,
        bucket_path=path,
        text_length=len(text),
        created_at=created_at,
    )


def share_team(
    text: str,
    *,
    agent_name: str,
    telegram_user_id: int,
    telegram_username: Optional[str],
    max_chars: int = _MAX_TEXT_CHARS,
) -> WriteResult:
    """Append a note to ``team/SHARED.md`` (writer-role only).

    Same envelope as ``remember_private``; the section is fixed to
    ``## Notes`` so the team file stays a single chronological log.
    """
    text = text.strip()
    if not text:
        raise ValueError("empty text")
    if len(text) > max_chars:
        raise ValueError(f"text too long ({len(text)} > {max_chars})")

    created_at = _now_iso()
    auto_key = _auto_key(text)
    envelope = _envelope(
        auto_key=auto_key,
        created_at=created_at,
        agent_name=agent_name,
        telegram_user_id=telegram_user_id,
        telegram_username=telegram_username,
        text=text,
    )
    path = memory.team_shared_path()
    memory.append_team("Notes", envelope, author=agent_name)

    log.info(
        "share_team agent=%s uid=%s key=%s len=%d",
        agent_name, telegram_user_id, auto_key, len(text),
    )
    return WriteResult(
        key=auto_key,
        bucket_path=path,
        text_length=len(text),
        created_at=created_at,
    )


def remember_for_project(
    slug: str,
    text: str,
    *,
    agent_name: str,
    max_chars: int = _MAX_TEXT_CHARS,
) -> ProjectWriteResult:
    """Whole-file replace of ``projects/<slug>/NOTES.md``.

    The first call auto-creates the project bucket directory; the caller
    surfaces this in the reply as ``Created new project bucket for key '<slug>'``.

    Validates the slug against ``PROJECT_SLUG_RE`` and the reserved-key
    list (per contract §3.5). Bad slugs raise ``ValueError`` before any
    filesystem touch — callers map the message to a clean Telegram
    ``Invalid key '<key>'.`` or ``Reserved key '<key>'.`` reply.
    """
    slug = slug.lower()
    if not auth.is_valid_project_slug(slug):
        raise ValueError(f"invalid project slug: {slug!r}")
    if auth.is_reserved_key(slug):
        raise ValueError(f"reserved key: {slug!r}")

    text = text.strip()
    if not text:
        raise ValueError("empty text")
    if len(text) > max_chars:
        raise ValueError(f"text too long ({len(text)} > {max_chars})")

    path = _project_path(slug)
    created_bucket = not path.exists()
    if created_bucket:
        # Bootstrap the project directory by writing first; write_project
        # calls _validate_project + mkdir(parents=True).
        memory.write_project(slug, text)
        # Also stamp meta sidecar with attribution (best-effort).
        try:
            _write_project_meta(slug, agent_name=agent_name)
        except Exception:  # noqa: BLE001
            log.warning("could not write .meta.json for %s", slug, exc_info=True)
    else:
        memory.write_project(slug, text)
        try:
            _write_project_meta(slug, agent_name=agent_name)
        except Exception:  # noqa: BLE001
            log.warning("could not refresh .meta.json for %s", slug, exc_info=True)

    log.info(
        "remember_for_project slug=%s agent=%s created=%s len=%d",
        slug, agent_name, created_bucket, len(text),
    )
    return ProjectWriteResult(
        key=slug,
        bucket_path=path,
        text_length=len(text),
        created_bucket=created_bucket,
    )


def _write_project_meta(slug: str, *, agent_name: str) -> None:
    """Sidecar ``.meta.json`` carrying last-modified-by attribution."""
    import json as _json
    meta_path = memory.projects_dir() / slug / ".meta.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_modified_by": agent_name,
        "last_modified_at": _now_iso(),
    }
    memory.atomic_write_text(meta_path, _json.dumps(payload, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# Forget API — called by /forget
# --------------------------------------------------------------------------- #

def forget_project(slug: str) -> ForgetResult:
    """Delete ``projects/<slug>/NOTES.md``; idempotent (no-op if absent).

    Reserved keys (per contract §3.5) are rejected before any fs touch
    so a malicious user can't poke the record-store namespaces.
    """
    slug = slug.lower()
    if not auth.is_valid_project_slug(slug):
        raise ValueError(f"invalid project slug: {slug!r}")
    if auth.is_reserved_key(slug):
        raise ValueError(f"reserved key: {slug!r}")
    path = _project_path(slug)
    deleted = path.exists()
    if deleted:
        path.unlink()
    log.info("forget_project slug=%s deleted=%s", slug, deleted)
    return ForgetResult(key=slug, target_path=path, deleted=deleted)


def forget_private_entry(agent_name: str, auto_key: str) -> ForgetResult:
    """Delete one envelope block from a private bucket, keyed by auto-key.

    No-op (returns ``deleted=False``) if the entry isn't present.
    The bucket file is rewritten atomically under the same lock pattern
    the rest of the store uses.
    """
    fs_name = _fs_safe_agent(agent_name)
    path = _private_bucket_path(fs_name)
    if not path.exists():
        return ForgetResult(key=auto_key, target_path=path, deleted=False)

    body = memory.read_text(path)
    # The envelope begins with ``## <auto_key>\n`` and runs until the next
    # ``## `` heading or end of file.
    pattern = re.compile(
        rf"^## {re.escape(auto_key)}\s*$.*?(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    new_body, n = pattern.subn("", body)
    deleted = n > 0
    if deleted:
        with memory._file_lock(path):  # noqa: SLF001 — internal but stable
            memory.atomic_write_text(path, new_body)
    log.info("forget_private agent=%s key=%s deleted=%s", agent_name, auto_key, deleted)
    return ForgetResult(key=auto_key, target_path=path, deleted=deleted)


# --------------------------------------------------------------------------- #
# Read API — called by /notes, /recall, /whoami
# --------------------------------------------------------------------------- #

def list_private_entries(agent_name: str, *, limit: int = 20) -> List[NoteEntry]:
    """Recent entries from the actor's private bucket, newest first."""
    fs_name = _fs_safe_agent(agent_name)
    path = _private_bucket_path(fs_name)
    if not path.exists():
        return []
    return _parse_entries(memory.read_text(path), source_path=path)[:limit]


def list_team_entries(*, limit: int = 20) -> List[NoteEntry]:
    """Recent entries from the team-shared bucket, newest first."""
    path = memory.team_shared_path()
    if not path.exists():
        return []
    return _parse_entries(memory.read_text(path), source_path=path)[:limit]


def list_projects() -> List[Tuple[str, str, Optional[str]]]:
    """``[(slug, body_preview, last_modified_by)]`` for /notes --project.

    ``body_preview`` is the first 80 chars of the project NOTES.md,
    sanitised. ``last_modified_by`` is read from ``.meta.json`` if
    present (else ``None``).
    """
    out: List[Tuple[str, str, Optional[str]]] = []
    for slug in memory.list_projects():
        notes = memory.read_project(slug)
        preview = notes.replace("\n", " ").strip()[:80]
        meta_path = memory.projects_dir() / slug / ".meta.json"
        author: Optional[str] = None
        if meta_path.exists():
            try:
                import json
                author = json.loads(memory.read_text(meta_path)).get("last_modified_by")
            except Exception:  # noqa: BLE001
                author = None
        out.append((slug, preview, author))
    return out


def search(query: str, *, agent_names: Iterable[str], limit_per_bucket: int = 5) -> List[NoteEntry]:
    """Substring search across the actor's private bucket + team + all projects.

    Returns the matched entries newest-first per bucket, deduped by
    ``(key, source_path)``. Search is case-insensitive and operates on
    the raw markdown body (so it matches across envelope fields and
    text body).
    """
    q = query.strip().lower()
    if not q:
        return []

    results: List[NoteEntry] = []

    for name in agent_names:
        path = _private_bucket_path(name)
        if path.exists():
            for entry in _parse_entries(memory.read_text(path), source_path=path):
                if q in entry.text.lower() or q in entry.key.lower():
                    results.append(entry)

    team_path = memory.team_shared_path()
    if team_path.exists():
        for entry in _parse_entries(memory.read_text(team_path), source_path=team_path):
            if q in entry.text.lower() or q in entry.key.lower():
                results.append(entry)

    for slug in memory.list_projects():
        path = memory.projects_dir() / slug / "NOTES.md"
        if not path.exists():
            continue
        body = memory.read_text(path).strip()
        if q in body.lower():
            # Synthesise a single "entry" so the /recall reply can show the slug.
            results.append(
                NoteEntry(
                    key=slug,
                    created_at="",  # project bucket has no envelope timestamp
                    text=body,
                    source_path=path,
                )
            )

    # Sort newest-first where we have a timestamp; projects go to the end.
    results.sort(key=lambda e: (e.created_at == "", e.created_at), reverse=True)
    return results


# --------------------------------------------------------------------------- #
# Markdown parser — extracts ## <auto_key> envelopes from a bucket body
# --------------------------------------------------------------------------- #

_ENVELOPE_HEAD_RE = re.compile(r"^##\s+(note-[0-9TZ:z-]+-[0-9a-f]+)\s*$", re.MULTILINE)


def _parse_entries(body: str, *, source_path: Path) -> List[NoteEntry]:
    """Parse envelope blocks from a private/team bucket body.

    Returns the entries newest-first (lexicographic on the auto-key,
    which == chronological because the key is ISO-prefixed).
    """
    matches = list(_ENVELOPE_HEAD_RE.finditer(body))
    entries: List[NoteEntry] = []
    for i, m in enumerate(matches):
        key = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        block = body[start:end]
        created_at = ""
        text_lines: List[str] = []
        in_text = False
        for line in block.split("\n"):
            if line.startswith("created_at:"):
                created_at = line.split(":", 1)[1].strip()
            elif line.startswith("text:") or line.startswith("text: |"):
                in_text = True
                continue
            elif in_text:
                # Drop the two-space indent we put in.
                if line.startswith("  "):
                    text_lines.append(line[2:])
                else:
                    text_lines.append(line)
        entries.append(
            NoteEntry(
                key=key,
                created_at=created_at,
                text="\n".join(text_lines).rstrip(),
                source_path=source_path,
            )
        )
    entries.sort(key=lambda e: e.key, reverse=True)
    return entries


# --------------------------------------------------------------------------- #
# Module-level helpers
# --------------------------------------------------------------------------- #

def is_valid_auto_key(s: str) -> bool:
    """True iff ``s`` matches the auto-key shape from contract §3.1."""
    return bool(_AUTO_KEY_RE.match(s))


__all__ = [
    "WriteResult",
    "ProjectWriteResult",
    "ForgetResult",
    "NoteEntry",
    "remember_private",
    "share_team",
    "remember_for_project",
    "forget_project",
    "forget_private_entry",
    "list_private_entries",
    "list_team_entries",
    "list_projects",
    "search",
    "is_valid_auto_key",
]

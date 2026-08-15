"""
agentchat v1.2 — record-oriented memory store (shared tier focus).

This is the implementation of the design in
``memory-store-design-v1.md`` for the **shared** tier (cross-agent
visibility within a workspace). It exposes the unified access API
(``put`` / ``get`` / ``list`` / ``delete`` / ``search``) over records
stored at::

    <memory_root>/shared/<workspace_id>/kv/<record_key>.json

The store is parameterised so per-agent and project tiers can share
storage code (one ``MemoryRecord`` envelope, one filesystem layout,
one lock primitive); this file's focus is the shared tier's ACL rules.

ACL (shared tier):

  * Read: any workspace member.
  * Write: any workspace member (``updated_by`` is stamped, ``created_by``
    is preserved on update).
  * Delete: original writer (``created_by == actor``) OR workspace role
    ``admin`` / ``owner``.

Every successful ``put`` / ``delete`` emits one ``audit_log`` row
(action ``memory_put`` / ``memory_delete``). Audit is best-effort —
failures are swallowed so the store never blocks a successful write
on an audit hiccup.

Concurrency:

  * Per-record ``fcntl.flock`` for cross-process safety on the same key.
  * ``put(..., if_version=N)`` provides CAS: mismatch raises
    :class:`VersionConflict` with the current version.
  * Default writes are last-writer-wins; the loser can detect via the
    incremented ``version`` on the returned record.

Stdlib only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from .memory_models import (
    MemoryRecord,
    ScopeError,
    TierError,
    validate_record_key,
    validate_scope,
    validate_tier,
)
from .memory_quota import QuotaRegistry
from .memory_acl import ActorResolver


# --------------------------------------------------------------------------- #
# Storage backend
# --------------------------------------------------------------------------- #

# Where shared-tier records live. Overridable for tests.
DEFAULT_SHARED_ROOT = Path.home() / ".hermes" / "memory" / "shared"


def _shared_root(root: Optional[Path]) -> Path:
    return Path(root) if root is not None else DEFAULT_SHARED_ROOT


def _record_path(root: Path, *, tier: str, scope: str, key: str) -> Path:
    """On-disk path: ``<root>/<tier>/<scope>/kv/<key>.json``."""
    return root / tier / scope / "kv" / f"{key}.json"


def _kv_dir(root: Path, *, tier: str, scope: str) -> Path:
    return root / tier / scope / "kv"


# --------------------------------------------------------------------------- #
# Exception hierarchy — store-specific subclasses of the model-layer errors
# --------------------------------------------------------------------------- #

from .memory_acl import MemoryStoreError  # noqa: E402, F401  (re-exported)


class KeyNotFound(LookupError, MemoryStoreError):
    """``get()`` / ``delete()`` could not find the record."""


class StorageError(MemoryStoreError):
    """IO / corruption talking to the underlying store."""


class VersionConflict(MemoryStoreError):
    """``put(..., if_version=N)`` mismatch on existing record."""

    def __init__(self, message: str, *, current_version: int) -> None:
        super().__init__(message)
        self.current_version = current_version


class MemoryPermissionError(PermissionError, MemoryStoreError):
    """The actor is not authorised to perform this operation."""


# Use the model layer's validation error verbatim so callers can
# ``except KeyFormatError`` regardless of which module they imported it
# from. (The earlier design created a parallel class; merging it here
# keeps the public surface uniform across tiers.)
from .memory_models import KeyError_ as KeyFormatError  # noqa: E402


# ``QuotaExceeded`` is the single class defined in :mod:`memory_quota`.
# Re-export it here so callers can ``except memory_store.QuotaExceeded``
# without an extra import line.
from .memory_quota import QuotaExceeded  # noqa: E402, F401


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #

def _audit(action: str, *, actor: str, tier: str, scope: str, key: str,
           extra: Optional[dict] = None) -> None:
    """Best-effort audit-log insert. Lazy-imports the agentchat audit hook
    so this module stays decoupled from the Flask app (per the design §6
    "memory_store.py is the single entry point"). Failures are silent."""
    try:
        from . import audit_log  # type: ignore
    except Exception:
        return
    try:
        metadata = {"tier": tier, "scope": scope, "key": key}
        if extra:
            metadata.update(extra)
        audit_log(
            action=action,
            actor=actor,
            target_type=tier,
            target_id=f"{scope}:{key}",
            metadata=metadata,
        )
    except Exception:
        # Audit must never break a successful store op.
        pass


# --------------------------------------------------------------------------- #
# MemoryStore
# --------------------------------------------------------------------------- #

class MemoryStore:
    """Single-class record-oriented store.

    Args:
      root: filesystem root for shared-tier records. Defaults to
        ``~/.hermes/memory/shared``. Tests pass a tmp dir.
      actor_resolver: pluggable ACL backend. Required.
      quota_registry: optional quota policy; defaults to
        :class:`QuotaRegistry` with no per-scope overrides.
    """

    def __init__(
        self,
        *,
        root: Optional[Path] = None,
        actor_resolver: ActorResolver,
        quota_registry: Optional[QuotaRegistry] = None,
    ) -> None:
        self._root = _shared_root(root)
        self._acl = actor_resolver
        self._quota = quota_registry or QuotaRegistry()

    @property
    def root(self) -> Path:
        return self._root

    # -- put --------------------------------------------------------------- #

    def put(
        self,
        *,
        tier: str,
        scope: str,
        key: str,
        value: Any = None,
        value_bytes: Optional[bytes] = None,
        metadata: Optional[dict] = None,
        actor: str,
        ttl_seconds: Optional[int] = None,
        if_version: Optional[int] = None,
    ) -> MemoryRecord:
        """Write a record. Creates it if absent, updates it if present.

        Returns the resulting :class:`MemoryRecord` (with new ``version``).
        """
        tier = validate_tier(tier)
        scope = validate_scope(tier, scope)
        key = validate_record_key(key)
        if not actor:
            raise MemoryPermissionError("actor is required")
        if not self._acl.is_member(tier=tier, scope=scope, actor=actor):
            raise MemoryPermissionError(
                f"actor {actor!r} is not a member of {tier}:{scope}"
            )

        path = _record_path(self._root, tier=tier, scope=scope, key=key)
        path.parent.mkdir(parents=True, exist_ok=True)

        from .memory import _file_lock, atomic_write_text  # type: ignore

        # Build the new envelope under a per-record advisory lock so
        # concurrent writers on the same key serialise.
        with _file_lock(path, exclusive=True):
            existing: Optional[MemoryRecord] = None
            if path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    existing = MemoryRecord.from_envelope(raw)
                except (json.JSONDecodeError, OSError, ValueError) as e:
                    raise StorageError(f"failed to read existing record: {e}")

                if existing.is_expired():
                    existing = None  # treat expired as absent

                if if_version is not None and existing is not None:
                    if existing.version != if_version:
                        raise VersionConflict(
                            f"if_version mismatch: caller expected "
                            f"{if_version}, record is at {existing.version}",
                            current_version=existing.version,
                        )

            new_record_count = 1 if existing is None else 0
            self._enforce_quota(
                tier=tier,
                scope=scope,
                new_record_count=new_record_count,
            )

            if existing is None:
                new = existing_or_new(
                    tier=tier,
                    scope=scope,
                    key=key,
                    value=value,
                    value_bytes=value_bytes,
                    metadata=metadata,
                    actor=actor,
                    ttl_seconds=ttl_seconds,
                    version=1,
                )
            else:
                new = MemoryRecord(
                    tier=existing.tier,
                    scope=existing.scope,
                    key=existing.key,
                    value=value,
                    value_bytes=value_bytes,
                    metadata=metadata if metadata is not None else dict(existing.metadata),
                    created_by=existing.created_by,
                    created_at=existing.created_at,
                    updated_by=actor,
                    updated_at=_iso_now(),
                    version=existing.version + 1,
                    ttl_seconds=ttl_seconds,
                )

            payload = json.dumps(new.to_envelope(), ensure_ascii=False, indent=2)
            try:
                atomic_write_text(path, payload + "\n")
            except OSError as e:
                raise StorageError(f"failed to write record: {e}")

        _audit(
            "memory_put",
            actor=actor,
            tier=tier,
            scope=scope,
            key=key,
            extra={"version": new.version, "created": existing is None},
        )
        return new

    # -- get --------------------------------------------------------------- #

    def get(self, *, tier: str, scope: str, key: str, actor: str) -> MemoryRecord:
        """Read a record by key. Raises :class:`KeyNotFound` if missing
        or expired (expired is treated as absent)."""
        tier = validate_tier(tier)
        scope = validate_scope(tier, scope)
        key = validate_record_key(key)
        if not actor:
            raise MemoryPermissionError("actor is required")
        if not self._acl.is_member(tier=tier, scope=scope, actor=actor):
            raise MemoryPermissionError(
                f"actor {actor!r} is not a member of {tier}:{scope}"
            )

        path = _record_path(self._root, tier=tier, scope=scope, key=key)
        if not path.exists():
            raise KeyNotFound(f"no record at {tier}:{scope}:{key}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            rec = MemoryRecord.from_envelope(raw)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            raise StorageError(f"failed to read record: {e}")
        if rec.is_expired():
            raise KeyNotFound(f"no record at {tier}:{scope}:{key} (expired)")
        return rec

    # -- list -------------------------------------------------------------- #

    def list(
        self,
        *,
        tier: str,
        scope: str,
        actor: str,
        prefix: str = "",
        limit: int = 100,
        cursor: Optional[str] = None,
        include_expired: bool = False,
    ) -> list[MemoryRecord]:
        """List all records in ``scope``.

        ``prefix`` filters on record_key (case-sensitive, exact prefix).
        ``cursor`` resumes after a given record_key (opaque token).
        """
        tier = validate_tier(tier)
        scope = validate_scope(tier, scope)
        if not actor:
            raise MemoryPermissionError("actor is required")
        if not self._acl.is_member(tier=tier, scope=scope, actor=actor):
            raise MemoryPermissionError(
                f"actor {actor!r} is not a member of {tier}:{scope}"
            )
        if limit < 1:
            raise ValueError("limit must be >= 1")

        kv_dir = _kv_dir(self._root, tier=tier, scope=scope)
        if not kv_dir.exists():
            return []
        out: list[MemoryRecord] = []
        for p in sorted(kv_dir.glob("*.json")):
            rk = p.stem
            if prefix and not rk.startswith(prefix):
                continue
            if cursor and rk <= cursor:
                continue
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                rec = MemoryRecord.from_envelope(raw)
            except (json.JSONDecodeError, OSError, ValueError):
                continue
            if rec.is_expired() and not include_expired:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    # -- delete ------------------------------------------------------------ #

    def delete(self, *, tier: str, scope: str, key: str, actor: str) -> bool:
        """Remove a record. Returns True if deleted, False if already absent.

        Delete permission rules (shared tier):
          * actor must be a workspace member, AND
          * (actor is the original writer, OR carries role admin/owner).
        """
        tier = validate_tier(tier)
        scope = validate_scope(tier, scope)
        key = validate_record_key(key)
        if not actor:
            raise MemoryPermissionError("actor is required")
        role = self._acl.role_in_scope(tier=tier, scope=scope, actor=actor)
        if role is None:
            raise MemoryPermissionError(
                f"actor {actor!r} is not a member of {tier}:{scope}"
            )

        path = _record_path(self._root, tier=tier, scope=scope, key=key)
        from .memory import _file_lock  # type: ignore
        with _file_lock(path, exclusive=True):
            if not path.exists():
                return False
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                rec = MemoryRecord.from_envelope(raw)
            except (json.JSONDecodeError, OSError, ValueError):
                rec = None

            if tier == "shared":
                is_admin = role in ("admin", "owner")
                is_writer = rec is not None and rec.created_by == actor
                if not (is_admin or is_writer):
                    raise MemoryPermissionError(
                        f"actor {actor!r} cannot delete record created by "
                        f"{rec.created_by if rec else '?'}; need writer or admin"
                    )
            # For agent/project tiers, members are sufficient.
            # Tier-specific rules are fleshed out by the per-agent and
            # project child tasks; this branch is permissive for now.

            try:
                os.remove(path)
            except OSError as e:
                raise StorageError(f"failed to delete record: {e}")
            lock_path = path.with_suffix(path.suffix + ".lock")
            try:
                if lock_path.exists():
                    os.remove(lock_path)
            except OSError:
                pass

        _audit(
            "memory_delete",
            actor=actor,
            tier=tier,
            scope=scope,
            key=key,
            extra={"role": role},
        )
        return True

    # -- search ------------------------------------------------------------ #

    def search(
        self,
        *,
        tier: str,
        scope: str,
        actor: str,
        query: str,
        metadata_filter: Optional[dict] = None,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        """Substring search over record keys + string repr of ``value``
        + ``metadata`` values. ``metadata_filter`` is an equality-match
        dict."""
        tier = validate_tier(tier)
        scope = validate_scope(tier, scope)
        if not actor:
            raise MemoryPermissionError("actor is required")
        if not self._acl.is_member(tier=tier, scope=scope, actor=actor):
            raise MemoryPermissionError(
                f"actor {actor!r} is not a member of {tier}:{scope}"
            )
        if not isinstance(query, str):
            raise ValueError("query must be a str")

        kv_dir = _kv_dir(self._root, tier=tier, scope=scope)
        if not kv_dir.exists():
            return []
        needle = query.lower()
        out: list[MemoryRecord] = []
        for p in sorted(kv_dir.glob("*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                rec = MemoryRecord.from_envelope(raw)
            except (json.JSONDecodeError, OSError, ValueError):
                continue
            if rec.is_expired():
                continue
            if metadata_filter:
                if not all(rec.metadata.get(k) == v for k, v in metadata_filter.items()):
                    continue
            haystack = rec.key.lower() + "\n" + json.dumps(
                rec.value, default=str, ensure_ascii=False
            ).lower()
            if needle not in haystack:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    # -- quota enforcement -------------------------------------------------- #

    def _enforce_quota(
        self,
        *,
        tier: str,
        scope: str,
        new_record_count: int,
    ) -> None:
        max_records = self._quota.max_records(tier=tier, scope=scope)
        max_bytes = self._quota.max_bytes(tier=tier, scope=scope)

        kv_dir = _kv_dir(self._root, tier=tier, scope=scope)
        existing_count = 0
        existing_bytes = 0
        if kv_dir.exists():
            for p in kv_dir.glob("*.json"):
                existing_count += 1
                try:
                    existing_bytes += p.stat().st_size
                except OSError:
                    pass

        projected_count = existing_count + new_record_count
        usage = {
            "tier": tier,
            "scope": scope,
            "records": projected_count,
            "bytes": existing_bytes,
            "cap_records": max_records,
            "cap_bytes": max_bytes,
        }
        if max_records is not None and projected_count > max_records:
            raise QuotaExceeded(
                f"record count {projected_count} exceeds quota {max_records} "
                f"for {tier}:{scope}",
                usage=usage,
            )
        if max_bytes is not None and existing_bytes > max_bytes:
            raise QuotaExceeded(
                f"byte usage {existing_bytes} exceeds quota {max_bytes} "
                f"for {tier}:{scope}",
                usage=usage,
            )

    # -- diagnostics -------------------------------------------------------- #

    def count(self, *, tier: str, scope: str, actor: str) -> int:
        """Number of records in ``scope``. ACL-gated like ``list``."""
        tier = validate_tier(tier)
        scope = validate_scope(tier, scope)
        if not self._acl.is_member(tier=tier, scope=scope, actor=actor):
            raise MemoryPermissionError(
                f"actor {actor!r} is not a member of {tier}:{scope}"
            )
        kv_dir = _kv_dir(self._root, tier=tier, scope=scope)
        if not kv_dir.exists():
            return 0
        return sum(1 for _ in kv_dir.glob("*.json"))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _iso_now() -> str:
    """Local copy of the model's iso helper so we don't depend on a
    private symbol."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def existing_or_new(
    *,
    tier: str,
    scope: str,
    key: str,
    value: Any,
    value_bytes: Optional[bytes],
    metadata: Optional[dict],
    actor: str,
    ttl_seconds: Optional[int],
    version: int,
) -> MemoryRecord:
    """Build a fresh :class:`MemoryRecord` (used for first-write path)."""
    now = _iso_now()
    return MemoryRecord(
        tier=tier,
        scope=scope,
        key=key,
        value=value,
        value_bytes=value_bytes,
        metadata=dict(metadata or {}),
        created_by=actor,
        created_at=now,
        updated_by=actor,
        updated_at=now,
        version=version,
        ttl_seconds=ttl_seconds,
    )


# --------------------------------------------------------------------------- #
# Convenience constructors
# --------------------------------------------------------------------------- #

def default_store(root: Optional[Path] = None) -> MemoryStore:
    """Build a :class:`MemoryStore` with the no-auth ACL resolver. For
    local scripts and demos. Not for production."""
    from .memory_acl import NoAuthActorResolver
    return MemoryStore(root=root, actor_resolver=NoAuthActorResolver())
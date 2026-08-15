"""
agentchat v1.2 — Memory store models.

Defines the envelope schema and the single ``MemoryRecord`` dataclass that is
shared across all three tiers (per-agent, shared, project). Tiers differ only
in path and ACL, not in row shape.

This module is intentionally stdlib-only and decoupled from the rest of
agentchat so it can be imported by ACL / quota / store modules without dragging
in Flask or the SQLite daemon.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

# --------------------------------------------------------------------------- #
# Tier
# --------------------------------------------------------------------------- #

Tier = Literal["agent", "shared", "project"]
ALLOWED_TIERS: tuple[Tier, ...] = ("agent", "shared", "project")


# --------------------------------------------------------------------------- #
# Key / scope validation
# --------------------------------------------------------------------------- #

# Match the design §1.1: 1..128 chars, lowercase, digits, _.-, no leading dash.
RECORD_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_\-.]{0,127}$")

AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-.]{0,63}$")
PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_\-.]{0,63}$")
# workspace_id is a server-issued integer; we accept str-of-int in the public
# API and parse to int internally.
WORKSPACE_ID_RE = re.compile(r"^[0-9]+$")


class TierError(ValueError):
    """Bad tier name."""


class ScopeError(ValueError):
    """Bad scope id format."""


class KeyError_(ValueError):
    """Bad record_key format."""


# Keep the stdlib name ``KeyError`` reserved for miss semantics; use a
# distinguished subclass for format errors so the store can raise either type
# without ambiguity.
KEY_FORMAT_ERROR = KeyError_


def validate_tier(tier: str) -> Tier:
    if tier not in ALLOWED_TIERS:
        raise TierError(f"invalid tier: {tier!r}; expected one of {ALLOWED_TIERS}")
    return tier  # type: ignore[return-value]


def validate_record_key(key: str) -> str:
    if not isinstance(key, str):
        raise KEY_FORMAT_ERROR(f"record_key must be str, got {type(key).__name__}")
    # Canonicalise case BEFORE regex check (design §1.1: "Case is
    # canonicalised to lowercase at write time"). This means
    # ``"My-Key"`` and ``"my-key"`` collapse to one record — that's the
    # design contract; callers using mixed case are not rejected, they
    # simply have their key lowercased.
    key = key.lower()
    if not RECORD_KEY_RE.match(key):
        raise KEY_FORMAT_ERROR(
            f"invalid record_key: {key!r}; must match ^[a-z0-9][a-z0-9_.-]{{0,127}}$"
        )
    # Reject names that collide with our internal bookkeeping files.
    if key.startswith((".", "_")):
        raise KEY_FORMAT_ERROR(f"record_key may not start with '_' or '.': {key!r}")
    if key.endswith("."):
        raise KEY_FORMAT_ERROR(f"record_key may not end with '.': {key!r}")
    if ".." in key:
        raise KEY_FORMAT_ERROR(f"record_key may not contain '..': {key!r}")
    return key


def validate_scope(tier: Tier, scope: str) -> str:
    """Validate a scope id is well-formed for the given tier."""
    if not isinstance(scope, str):
        raise ScopeError(f"scope must be str, got {type(scope).__name__}")
    if tier == "agent":
        if not AGENT_NAME_RE.match(scope):
            raise ScopeError(f"invalid agent scope: {scope!r}")
    elif tier == "shared":
        if not WORKSPACE_ID_RE.match(scope) or int(scope) < 1:
            raise ScopeError(f"invalid workspace_id scope: {scope!r}")
    elif tier == "project":
        if not PROJECT_SLUG_RE.match(scope):
            raise ScopeError(f"invalid project slug: {scope!r}")
    else:
        raise ScopeError(f"unknown tier: {tier!r}")
    return scope


# --------------------------------------------------------------------------- #
# MemoryRecord
# --------------------------------------------------------------------------- #


@dataclass
class MemoryRecord:
    """One record on disk. Shared across all three tiers."""

    tier: Tier
    scope: str
    key: str
    value: Any = None
    value_bytes: Optional[bytes] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_by: str = ""
    created_at: str = ""
    updated_by: str = ""
    updated_at: str = ""
    version: int = 1
    ttl_seconds: Optional[int] = None

    # -- (de)serialisation ------------------------------------------------- #

    SCHEMA_VERSION: int = 1  # class-level; not a dataclass field

    def to_envelope(self) -> dict[str, Any]:
        """Serialise to the on-disk envelope shape (design §1.3)."""
        import base64

        envelope: dict[str, Any] = {
            "schema_version": self.SCHEMA_VERSION,
            "tier": self.tier,
            "scope": self.scope,
            "key": self.key,
            "value": self.value,
            "value_bytes": (
                base64.b64encode(self.value_bytes).decode("ascii")
                if self.value_bytes is not None
                else None
            ),
            "metadata": dict(self.metadata),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at,
            "version": self.version,
            "ttl_seconds": self.ttl_seconds,
        }
        # Compute expires_at lazily for the on-disk representation.
        if self.ttl_seconds is not None and self.updated_at:
            updated = _parse_iso(self.updated_at)
            expires = updated.timestamp() + self.ttl_seconds
            envelope["expires_at"] = _iso_from_ts(expires)
        else:
            envelope["expires_at"] = None
        return envelope

    @classmethod
    def from_envelope(cls, env: dict[str, Any]) -> "MemoryRecord":
        """Rehydrate from a JSON dict. Raises ValueError on bad shape."""
        import base64

        if not isinstance(env, dict):
            raise ValueError(f"envelope must be dict, got {type(env).__name__}")
        # Required immutable fields.
        for field in ("tier", "scope", "key", "created_by", "created_at"):
            if field not in env:
                raise ValueError(f"envelope missing required field: {field!r}")
        tier = validate_tier(env["tier"])
        scope = validate_scope(tier, str(env["scope"]))
        key = validate_record_key(str(env["key"]))
        raw_bytes = env.get("value_bytes")
        if isinstance(raw_bytes, str):
            try:
                value_bytes: Optional[bytes] = base64.b64decode(raw_bytes)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"malformed value_bytes base64: {e}") from e
        else:
            value_bytes = None
        if env.get("value") is not None and value_bytes is not None:
            # Per design §1.3: ``value_bytes`` wins; drop ``value``.
            value: Any = None
        else:
            value = env.get("value")
        metadata = env.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be a dict")
        version = int(env.get("version", 1))
        if version < 1:
            raise ValueError(f"version must be >= 1, got {version}")
        ttl = env.get("ttl_seconds")
        if ttl is not None:
            ttl = int(ttl)
            if ttl < 0:
                raise ValueError(f"ttl_seconds must be >= 0, got {ttl}")
        return cls(
            tier=tier,
            scope=scope,
            key=key,
            value=value,
            value_bytes=value_bytes,
            metadata=dict(metadata),
            created_by=str(env["created_by"]),
            created_at=str(env["created_at"]),
            updated_by=str(env.get("updated_by", env["created_by"])),
            updated_at=str(env.get("updated_at", env["created_at"])),
            version=version,
            ttl_seconds=ttl,
        )

    # -- copy semantics ----------------------------------------------------- #

    def copy(self, **overrides: Any) -> "MemoryRecord":
        """Return a new MemoryRecord with overrides applied. Used for
        deep-copying user-facing get() results so callers can't mutate on-disk
        state by accident.
        """
        import copy

        return MemoryRecord(
            tier=self.tier,
            scope=self.scope,
            key=self.key,
            value=copy.deepcopy(self.value),
            value_bytes=(
                bytes(self.value_bytes) if self.value_bytes is not None else None
            ),
            metadata=copy.deepcopy(self.metadata),
            created_by=self.created_by,
            created_at=self.created_at,
            updated_by=self.updated_by,
            updated_at=self.updated_at,
            version=self.version,
            ttl_seconds=self.ttl_seconds,
            **overrides,
        )

    def is_expired(self, *, now: Optional[float] = None) -> bool:
        """A record is expired if ``ttl_seconds`` is set and we're past
        ``updated_at + ttl_seconds``.
        """
        if self.ttl_seconds is None or not self.updated_at:
            return False
        updated = _parse_iso(self.updated_at)
        cutoff = updated.timestamp() + self.ttl_seconds
        reference = now if now is not None else datetime.now(timezone.utc).timestamp()
        return reference >= cutoff


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _parse_iso(iso: str) -> datetime:
    # Python's fromisoformat in 3.11 handles the ``+00:00`` trailing zone.
    s = iso
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

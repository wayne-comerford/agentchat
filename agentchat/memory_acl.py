"""
ACL backend for the v1 :class:`agentchat.memory_store.MemoryStore`.

The :class:`MemoryStore` consults an ``actor_resolver`` on every read,
write, and delete to decide whether the calling actor is allowed to
operate on a (tier, scope) pair.  This module provides the protocol plus
two implementations:

  * :class:`StaticActorResolver` — in-memory table; perfect for tests
    and the bridge where the workspace membership is pinned in code or
    loaded from a config file.
  * :class:`NoAuthActorResolver` — open door; every actor is a member
    with role ``admin``.  Use only for development / smoke tests.

Both are deliberately stdlib-only and stateless on disk so a swap
doesn't force a migration.
"""
from __future__ import annotations

from typing import Optional, Protocol


class MemoryStoreError(Exception):
    """Base class for all store-layer errors.

    Re-exported via :mod:`agentchat.memory_store` so callers can write
    ``except memory_store.MemoryStoreError`` to catch every store-defined
    failure mode (quota, version conflict, permission, etc.).
    """


class ActorResolver(Protocol):
    """Protocol every ACL backend must satisfy."""

    def is_member(
        self, *, tier: str, scope: str, actor: str
    ) -> bool:
        """True if ``actor`` is allowed to read in ``(tier, scope)``."""
        ...

    def role_in_scope(
        self, *, tier: str, scope: str, actor: str
    ) -> Optional[str]:
        """Return the actor's role string (e.g. ``"admin"``, ``"owner"``,
        ``"member"``) or ``None`` if they're not a member of the scope.

        Implementations should treat ``"owner"`` and ``"admin"`` as
        equivalent for permission checks; the store itself doesn't care
        which string it gets back, only that it's non-None for write /
        delete of other people's records.
        """
        ...


class StaticActorResolver:
    """In-memory ACL table populated via :meth:`add`.

    Suitable for tests and any deployment where workspace membership
    is small enough to load at startup.  Not thread-safe across
    concurrent ``add`` + read — callers should populate once during
    boot and then treat the table as read-only.
    """

    def __init__(self) -> None:
        # (tier, scope) -> {actor: role | None}
        self._table: dict[tuple[str, str], dict[str, Optional[str]]] = {}

    def add(
        self, tier: str, scope: str, actor: str, role: Optional[str]
    ) -> None:
        """Add or update ``actor``'s role in ``(tier, scope)``.

        ``role=None`` explicitly records a non-member (so a later
        :meth:`is_member` returns False even if the actor is added to
        a sibling scope).
        """
        key = (tier, scope)
        self._table.setdefault(key, {})[actor] = role

    def remove(self, tier: str, scope: str, actor: str) -> None:
        """Forget ``actor``'s role in ``(tier, scope)`` (no-op if absent)."""
        self._table.get((tier, scope), {}).pop(actor, None)

    def is_member(self, *, tier: str, scope: str, actor: str) -> bool:
        return self._table.get((tier, scope), {}).get(actor) is not None

    def role_in_scope(
        self, *, tier: str, scope: str, actor: str
    ) -> Optional[str]:
        return self._table.get((tier, scope), {}).get(actor)


class NoAuthActorResolver:
    """Open-door ACL. Every actor is a member with role ``admin``.

    Intended for development + smoke tests only. Production should use
    :class:`StaticActorResolver` or a future real backend (e.g. backed
    by the workspace registry).
    """

    def is_member(self, *, tier: str, scope: str, actor: str) -> bool:
        return True

    def role_in_scope(
        self, *, tier: str, scope: str, actor: str
    ) -> Optional[str]:
        return "admin"


__all__ = [
    "ActorResolver",
    "StaticActorResolver",
    "NoAuthActorResolver",
]

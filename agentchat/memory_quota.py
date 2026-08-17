"""
agentchat v1.2 — per-scope quota policy for the memory store.

This is the implementation of the quota section in
``memory-store-design-v1.md``. Quotas are checked by
:meth:`MemoryStore._enforce_quota` before a new record is written.

Policy shape:

  * Each ``(tier, scope)`` pair can have a per-scope override.
  * Quotas are soft (returned as ``None`` means "no limit").
  * Per-record byte cost is measured on disk (file size of the
    ``.json`` envelope) — close enough for tier policy.

The default registry has no overrides; callers (typically a settings
backend) populate per-scope caps via :meth:`QuotaRegistry.set_limit`.

Stdlib only.
"""
from __future__ import annotations

from typing import Optional, Tuple

from .memory_acl import MemoryStoreError


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class QuotaExceeded(MemoryStoreError):
    """A write would push the scope past its configured quota."""

    def __init__(self, message: str, *, usage: dict) -> None:
        super().__init__(message)
        self.usage = usage  # tier/scope/records/bytes/cap_records/cap_bytes


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

class QuotaRegistry:
    """Per-scope quota policy.

    A quota is ``(max_records, max_bytes)``. Either may be ``None`` to
    mean "unlimited on that axis".

    The registry is intentionally a thin dict-like lookup: tests and the
    production settings backend both want predictable, no-magic behaviour
    (no env-var fallback, no implicit default cap, no auto-discovery).
    """

    def __init__(
        self,
        *,
        max_records: Optional[int] = None,
        max_bytes: Optional[int] = None,
    ) -> None:
        # Global default — applies when no per-scope override exists.
        self._default: Tuple[Optional[int], Optional[int]] = (
            max_records,
            max_bytes,
        )
        # Per-scope overrides, keyed by (tier, scope).
        self._overrides: dict[Tuple[str, str], Tuple[Optional[int], Optional[int]]] = {}

    # -- public API --------------------------------------------------------- #

    def set_override(
        self,
        *,
        tier: str,
        scope: str,
        records: Optional[int] = None,
        bytes_: Optional[int] = None,
    ) -> None:
        """Set a per-scope quota override.

        Keyword args:

          * ``records`` — max record count for this scope (``None`` = unlimited).
          * ``bytes_`` — max total bytes on disk for this scope (``None`` = unlimited).

        Trailing underscore on ``bytes_`` avoids shadowing the builtin.
        """
        self._overrides[(tier, scope)] = (records, bytes_)

    def clear(self, *, tier: str, scope: str) -> None:
        """Drop any override for ``(tier, scope)``."""
        self._overrides.pop((tier, scope), None)

    def max_records(self, *, tier: str, scope: str) -> Optional[int]:
        return self._quota_for(tier=tier, scope=scope)[0]

    def max_bytes(self, *, tier: str, scope: str) -> Optional[int]:
        return self._quota_for(tier=tier, scope=scope)[1]

    # -- internals ---------------------------------------------------------- #

    def _quota_for(self, *, tier: str, scope: str) -> Tuple[Optional[int], Optional[int]]:
        return self._overrides.get((tier, scope), self._default)

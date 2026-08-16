"""
agentchat v1.2 — UI/Bridge user preferences: data model + storage layer.

This module is the **single source of truth** for the user_preferences
record. The HTTP layer, the SSE bootstrap, and the bridge handshake
all import the dataclass and the default-resolver from here so a
schema change touches one place.

Schema (mirrors the CREATE TABLE in `agentchat.__init__.SCHEMA`):

    user_preferences (
        user_id            INTEGER PRIMARY KEY,    -- references users.id ON DELETE CASCADE
        default_channel_id TEXT,                   -- nullable; NULL = no default channel
        theme              TEXT CHECK (theme IN ('light','dark','system')) NOT NULL DEFAULT 'system',
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL
    )

Defaults
--------
The documented defaults live in `DEFAULT_PREFERENCES` (a frozen dataclass).
Every caller — GET endpoint, PUT validator, UI bootstrap, bridge
handshake — MUST go through `default_preferences()` so a single
edit propagates everywhere. The defaults are deliberately conservative:
`'system'` theme respects the OS, and a NULL `default_channel_id`
means the UI lets the user pick rather than guessing.

Concurrency
-----------
`upsert_preferences` uses an INSERT ... ON CONFLICT DO UPDATE
statement against the PRIMARY KEY (user_id). SQLite serializes
writers, so two concurrent PUTs for the same user cannot produce a
torn row — the second one wins, and `updated_at` is bumped to
the newer ISO timestamp so audit trails can reconstruct the order.

Stdlib only. No external deps.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Sentinel for "field omitted" (distinct from "field set to None").
#
# `upsert_preferences(conn, uid)` (no kwargs) is a no-op for the data but
# still bumps updated_at — useful for "touch" semantics in tests and the
# HTTP layer. The HTTP PUT handler maps "JSON key absent" -> _UNSET and
# "JSON key present with value null" -> None, so the storage layer can
# tell the two apart.
# --------------------------------------------------------------------------- #

class _UnsetSentinel:
    """Singleton sentinel: distinguishable from None and from any string.

    Repr is stable so log lines read sensibly.
    """
    _instance: "_UnsetSentinel | None" = None

    def __new__(cls) -> "_UnsetSentinel":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<UNSET>"

    # Equality is identity-based (singletons compare equal only to themselves),
    # which is what we want: callers always check `is` against the singleton.
    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return id(self)


_UNSET: Any = _UnsetSentinel()


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# Closed set of valid theme values. The DB CHECK constraint and the
# HTTP-layer validator both reference this tuple so adding a new
# theme requires editing exactly two places (here + frontend).
VALID_THEMES: tuple[str, ...] = ("light", "dark", "system")

DEFAULT_THEME: str = "system"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class UserPreferences:
    """A single user's UI/Bridge preferences.

    Attributes:
        user_id:            FK into users.id. Always set on a returned record
                            (we never return preferences without their owner).
        default_channel_id: None when the user hasn't picked a default; a
                            string id otherwise. Forward-compatible with the
                            upcoming channels table — for now it's just text.
        theme:              One of VALID_THEMES. Defaults to DEFAULT_THEME.
        created_at:         ISO-8601 timestamp (UTC, second precision). Set
                            on first PUT; never modified thereafter.
        updated_at:         ISO-8601 timestamp (UTC, second precision). Bumped
                            on every successful PUT.

    The dataclass is frozen so accidental mutation raises instead of
    silently drifting the in-memory copy out of sync with the DB row.
    Use `replace()` (dataclasses.replace) to build a new record when
    applying user input.
    """

    user_id: int
    default_channel_id: Optional[str] = None
    theme: str = DEFAULT_THEME
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if self.theme not in VALID_THEMES:
            raise ValueError(
                f"invalid theme {self.theme!r}; expected one of {VALID_THEMES!r}"
            )
        if self.user_id <= 0:
            raise ValueError(
                f"user_id must be a positive integer (got {self.user_id!r})"
            )

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict. Stable key order via dataclass."""
        return {
            "user_id": self.user_id,
            "default_channel_id": self.default_channel_id,
            "theme": self.theme,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

def default_preferences(user_id: int) -> UserPreferences:
    """Return a UserPreferences populated with the documented defaults.

    `created_at` / `updated_at` are empty strings (no row in the DB yet);
    callers that need real timestamps should stamp them after the first
    write.

    This function is the single source of truth for defaults — both
    the GET endpoint (which returns it when the row is absent) and
    the PUT validator (which seeds missing fields from it) call here.
    """
    return UserPreferences(
        user_id=user_id,
        default_channel_id=None,
        theme=DEFAULT_THEME,
        created_at="",
        updated_at="",
    )


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    """UTC ISO-8601 timestamp with second precision. Matches `now_iso()`
    in the rest of the codebase so timestamps line up in audit logs."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

def _row_to_preferences(row: sqlite3.Row) -> UserPreferences:
    """Convert a sqlite3.Row from `user_preferences` into a UserPreferences.

    Centralized so the column order in the SELECT stays in one place.
    """
    return UserPreferences(
        user_id=int(row["user_id"]),
        default_channel_id=row["default_channel_id"],
        theme=str(row["theme"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def get_preferences(conn: sqlite3.Connection, user_id: int) -> UserPreferences:
    """Return the user's preferences row, or `default_preferences(user_id)`
    if no row exists. Pure read: never inserts a row.

    This is the read path the GET endpoint and the UI bootstrap both use.
    Keeping it side-effect-free means "missing" and "default" are
    observationally identical to the caller, which is the contract the
    acceptance criteria require.

    Args:
        conn:    Open sqlite3 connection. Caller owns the lifecycle.
        user_id: FK into users.id.

    Returns:
        A UserPreferences — either the stored row or the defaults.
    """
    row = conn.execute(
        "SELECT user_id, default_channel_id, theme, created_at, updated_at "
        "FROM user_preferences WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return default_preferences(user_id)
    return _row_to_preferences(row)


def upsert_preferences(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    default_channel_id: Optional[str] = _UNSET,
    theme: Optional[str] = _UNSET,
) -> UserPreferences:
    """Insert-or-update the user's preferences row and return the new state.

    On first call for a user, this creates the row with `created_at =
    updated_at = now`. On subsequent calls, only the supplied fields
    change; unspecified fields keep their existing value (or the column
    default for a fresh row).

    Field semantics:
      - The argument is "supplied" iff the caller passed a value or
        explicitly passed None. The sentinel `_UNSET` (the default for
        every kwarg) means "leave alone". This is critical for the PUT
        endpoint: it has to distinguish "key omitted from JSON" from
        "key present with value null". The HTTP layer should pass
        `_UNSET` when the key is absent and the actual value (which
        may be None) when the key is present.

        Concretely:
          upsert_preferences(conn, uid, theme="dark")          -> change theme only
          upsert_preferences(conn, uid, theme=None)            -> set theme to NULL  (impossible today; column is NOT NULL — raises)
          upsert_preferences(conn, uid, default_channel_id=None) -> clear default channel
          upsert_preferences(conn, uid)                         -> bump updated_at only (every-field no-op)

    Validation is strict: the theme MUST be in VALID_THEMES (or
    _UNSET); `default_channel_id` is stored verbatim and the channel
    existence check is the caller's responsibility (the channel table
    doesn't ship yet, and the HTTP layer is the right place to enforce
    referential integrity anyway).

    The DB CHECK constraint on `theme` is the last line of defence
    against bad writes — even if a future caller forgets to validate,
    SQLite will reject the row.

    Args:
        conn:                Open sqlite3 connection.
        user_id:             FK into users.id.
        default_channel_id:  New value, or _UNSET to leave the existing
                             value alone. Pass None to clear it (sets the
                             column to NULL).
        theme:               New theme, or _UNSET to leave the existing
                             value alone. Pass one of VALID_THEMES to
                             change it. (Passing None raises ValueError —
                             the column is NOT NULL by design.)

    Returns:
        The post-write UserPreferences (always populated, even on insert).
    """
    if theme is not _UNSET and theme is not None and theme not in VALID_THEMES:
        raise ValueError(
            f"invalid theme {theme!r}; expected one of {VALID_THEMES!r}"
        )
    if user_id <= 0:
        raise ValueError(f"user_id must be a positive integer (got {user_id!r})")

    now = _now_iso()

    # Look up the existing row (if any) so we can preserve created_at
    # and decide what to write for unspecified fields.
    existing = conn.execute(
        "SELECT user_id, default_channel_id, theme, created_at, updated_at "
        "FROM user_preferences WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    if existing is None:
        # First write for this user — seed the defaults for any
        # unspecified field, then stamp created_at = updated_at = now.
        eff_channel = (
            default_channel_id if default_channel_id is not _UNSET else None
        )
        eff_theme = (
            theme if theme is not _UNSET and theme is not None
            else DEFAULT_THEME
        )
        conn.execute(
            "INSERT INTO user_preferences "
            "(user_id, default_channel_id, theme, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, eff_channel, eff_theme, now, now),
        )
        return UserPreferences(
            user_id=user_id,
            default_channel_id=eff_channel,
            theme=eff_theme,
            created_at=now,
            updated_at=now,
        )

    # Existing row — merge the supplied fields.
    eff_channel = (
        default_channel_id if default_channel_id is not _UNSET
        else existing["default_channel_id"]
    )
    eff_theme = (
        theme if theme is not _UNSET
        else existing["theme"]
    )
    eff_created = existing["created_at"]

    conn.execute(
        "UPDATE user_preferences "
        "SET default_channel_id = ?, theme = ?, updated_at = ? "
        "WHERE user_id = ?",
        (eff_channel, eff_theme, now, user_id),
    )
    return UserPreferences(
        user_id=user_id,
        default_channel_id=eff_channel,
        theme=eff_theme,
        created_at=eff_created,
        updated_at=now,
    )


def delete_preferences(conn: sqlite3.Connection, user_id: int) -> bool:
    """Remove the user's preferences row. Returns True if a row was deleted.

    Mostly used by tests and by the user-deletion CASCADE; the HTTP
    layer doesn't expose a DELETE endpoint today.
    """
    cur = conn.execute(
        "DELETE FROM user_preferences WHERE user_id = ?", (user_id,)
    )
    return cur.rowcount > 0


__all__ = [
    # constants
    "VALID_THEMES",
    "DEFAULT_THEME",
    # data model
    "UserPreferences",
    # defaults
    "default_preferences",
    # storage
    "get_preferences",
    "upsert_preferences",
    "delete_preferences",
]
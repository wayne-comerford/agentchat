"""
Auth + namespace validation for the agentchat bot memory bridge.

This module is the single source of truth for what's a valid project slug
and which keys are reserved (so a user can't shadow record-store
namespaces like ``system``).  See contract §3.5.

The helpers here are deliberately tiny and stdlib-only so the bridge
module doesn't pull in aiohttp / nostr / etc. when it's being used
from a CLI process or a unit test.
"""
from __future__ import annotations

import re

# Project slugs: 1-64 chars, start with alnum, then alnum / underscore /
# dash / dot. Same shape as agentchat.memory.PROJECT_SLUG_RE so the
# filesystem layout stays consistent.
_PROJECT_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,63}$")

# Reserved project keys — these shadow record-store namespaces and are
# forbidden so a user can't write ``remember_for_project("system", ...)``
# and corrupt internal state.  Add to this set carefully; every entry
# is a user-facing restriction.
_RESERVED_KEYS: frozenset[str] = frozenset({
    "system",
    "admin",
    "root",
    "internal",
    "_private",
    "_shared",
    "_team",
    "memory",
    "memories",
})


def is_valid_project_slug(slug: str) -> bool:
    """True iff ``slug`` is a syntactically valid project identifier.

    Returns False for empty strings, strings starting with a separator,
    strings containing path-traversal characters (``/`` / ``\\``), or
    anything outside the [A-Za-z0-9_\\-.] alphabet.
    """
    if not isinstance(slug, str) or not slug:
        return False
    return bool(_PROJECT_SLUG_RE.match(slug))


def is_reserved_key(slug: str) -> bool:
    """True iff ``slug`` is a reserved namespace (case-insensitive)."""
    if not isinstance(slug, str):
        return False
    return slug.strip().lower() in _RESERVED_KEYS

"""Preferences data model + storage layer (v1.2).

Acceptance criteria from t_1dc88731:
  - migration applies cleanly on a fresh DB
  - migration rolls back without error
  - an empty record returns the documented defaults
  - the model is importable from a shared module (agentchat.preferences)

The HTTP integration tests live in a sibling file
(`test_preferences_api.py`) — those depend on the user/workspace
auth layer and need a live server. This file is the unit layer:
DB-in-process, stdlib only.
"""
from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError

import pytest


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh, isolated AGENTCHAT_HOME with a fully-initialized DB.

    `monkeypatch.setenv(AGENTCHAT_HOME, ...)` must be applied BEFORE the
    agentchat package reads DB_PATH (which happens at import time as a
    module-level constant). The test that uses this fixture imports
    `agentchat` lazily inside the test body to guarantee the env var
    is in place.
    """
    monkeypatch.setenv("AGENTCHAT_HOME", str(tmp_path / "agentchat-home"))
    # Importing agentchat with the env var set so DB_PATH resolves to
    # the tmp dir. We do this here (fixture body, post monkeypatch) rather
    # than at module top so other test files that don't need the DB
    # don't pay the import cost.
    import agentchat  # noqa: F401  (import for side effect)

    import importlib

    # Re-import to re-bind DB_PATH to the patched env var. The agentchat
    # module captures DB_PATH at first import, so for a tmpdir swap we
    # need a fresh module load.
    importlib.reload(agentchat)

    agentchat.db_init()
    conn = agentchat.db_connect()
    try:
        yield conn
    finally:
        conn.close()


def _insert_user(conn, username="alice"):
    """Insert a user row so we can FK-reference it from user_preferences."""
    cur = conn.execute(
        "INSERT INTO users(username, password_hash, created_at) "
        "VALUES (?, ?, ?)",
        (username, "x", "2026-01-01T00:00:00+00:00"),
    )
    return cur.lastrowid


# --------------------------------------------------------------------------- #
# Module imports + constants
# --------------------------------------------------------------------------- #

def test_module_is_importable():
    """The data model must be importable from agentchat.preferences."""
    from agentchat import preferences as prefs_mod

    assert hasattr(prefs_mod, "UserPreferences")
    assert hasattr(prefs_mod, "default_preferences")
    assert hasattr(prefs_mod, "get_preferences")
    assert hasattr(prefs_mod, "upsert_preferences")
    assert hasattr(prefs_mod, "delete_preferences")
    assert hasattr(prefs_mod, "VALID_THEMES")
    assert hasattr(prefs_mod, "DEFAULT_THEME")


def test_valid_themes_constant():
    """The closed theme enum is exactly ('light','dark','system') in order."""
    from agentchat.preferences import VALID_THEMES, DEFAULT_THEME

    assert VALID_THEMES == ("light", "dark", "system")
    assert DEFAULT_THEME in VALID_THEMES
    assert DEFAULT_THEME == "system"  # documented default


def test_default_theme_is_system():
    """Documented default: theme='system'. Anything else is a bug."""
    from agentchat.preferences import DEFAULT_THEME

    assert DEFAULT_THEME == "system"


# --------------------------------------------------------------------------- #
# Dataclass validation
# --------------------------------------------------------------------------- #

def test_userpreferences_rejects_invalid_theme():
    """The dataclass refuses unknown themes up front."""
    from agentchat.preferences import UserPreferences

    with pytest.raises(ValueError, match="invalid theme"):
        UserPreferences(user_id=1, theme="neon")


def test_userpreferences_rejects_zero_user_id():
    """user_id must be a positive integer (it's an FK to users.id)."""
    from agentchat.preferences import UserPreferences

    with pytest.raises(ValueError, match="user_id"):
        UserPreferences(user_id=0)


def test_userpreferences_rejects_negative_user_id():
    """user_id must be a positive integer (it's an FK to users.id)."""
    from agentchat.preferences import UserPreferences

    with pytest.raises(ValueError, match="user_id"):
        UserPreferences(user_id=-5)


def test_userpreferences_is_frozen():
    """The dataclass is frozen so accidental mutation raises."""
    from agentchat.preferences import UserPreferences

    p = UserPreferences(user_id=1)
    with pytest.raises(FrozenInstanceError):
        p.theme = "dark"  # type: ignore[misc]


def test_userpreferences_to_dict_shape():
    """to_dict() exposes every documented field in stable order."""
    from agentchat.preferences import UserPreferences

    p = UserPreferences(
        user_id=7,
        default_channel_id="general",
        theme="dark",
        created_at="2026-08-16T00:00:00+00:00",
        updated_at="2026-08-16T00:01:00+00:00",
    )
    d = p.to_dict()
    assert d == {
        "user_id": 7,
        "default_channel_id": "general",
        "theme": "dark",
        "created_at": "2026-08-16T00:00:00+00:00",
        "updated_at": "2026-08-16T00:01:00+00:00",
    }


# --------------------------------------------------------------------------- #
# default_preferences()
# --------------------------------------------------------------------------- #

def test_default_preferences_returns_documented_defaults():
    """default_preferences(uid) yields (uid, None, 'system', '', '')."""
    from agentchat.preferences import default_preferences

    p = default_preferences(42)
    assert p.user_id == 42
    assert p.default_channel_id is None
    assert p.theme == "system"
    assert p.created_at == ""
    assert p.updated_at == ""


def test_default_preferences_does_not_insert(db):
    """default_preferences(uid) must be a pure function: no row written."""
    uid = _insert_user(db)
    from agentchat.preferences import default_preferences

    p = default_preferences(uid)
    assert p.user_id == uid

    n = db.execute(
        "SELECT COUNT(*) FROM user_preferences WHERE user_id = ?", (uid,)
    ).fetchone()[0]
    assert n == 0


# --------------------------------------------------------------------------- #
# Schema / migration
# --------------------------------------------------------------------------- #

def test_db_init_creates_user_preferences(db):
    """A fresh db_init() must include the user_preferences table."""
    row = db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='user_preferences'"
    ).fetchone()
    assert row is not None, "user_preferences missing after db_init"


def test_user_preferences_has_expected_columns(db):
    """Column list + types match the documented schema."""
    cols = db.execute("PRAGMA table_info(user_preferences)").fetchall()
    info = {c["name"]: c for c in cols}

    assert set(info) == {
        "user_id",
        "default_channel_id",
        "theme",
        "created_at",
        "updated_at",
    }
    assert info["user_id"]["pk"] == 1
    assert info["theme"]["notnull"] == 1
    assert info["created_at"]["notnull"] == 1
    assert info["updated_at"]["notnull"] == 1
    # default_channel_id is nullable (no default channel selected yet)
    assert info["default_channel_id"]["notnull"] == 0


def test_db_check_constraint_rejects_bad_theme(db):
    """The CHECK on theme is the last line of defence against bad writes."""
    uid = _insert_user(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO user_preferences"
            "(user_id, theme, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (uid, "neon", "x", "x"),
        )


def test_fk_cascade_drops_preferences_on_user_delete(db):
    """Removing a user drops their preferences row automatically."""
    uid = _insert_user(db)
    db.execute(
        "INSERT INTO user_preferences"
        "(user_id, theme, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (uid, "light", "x", "x"),
    )
    db.execute("DELETE FROM users WHERE id = ?", (uid,))
    n = db.execute(
        "SELECT COUNT(*) FROM user_preferences WHERE user_id = ?", (uid,)
    ).fetchone()[0]
    assert n == 0


def test_apply_migration_preferences_is_idempotent_on_fresh_db(db):
    """Re-running apply_migration_preferences on a fresh DB is a no-op."""
    import agentchat

    # db_init already created the table. apply must not raise.
    agentchat.apply_migration_preferences(db)
    agentchat.apply_migration_preferences(db)
    n = db.execute(
        "SELECT COUNT(*) FROM user_preferences"
    ).fetchone()[0]
    # No rows inserted; migration just creates the table.
    assert n == 0


def test_rollback_migration_preferences_drops_table(db):
    """rollback_migration_preferences drops user_preferences cleanly."""
    import agentchat

    agentchat.rollback_migration_preferences(db)
    row = db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='user_preferences'"
    ).fetchone()
    assert row is None


def test_rollback_is_idempotent(db):
    """Rolling back twice (or on a never-applied DB) must not raise."""
    import agentchat

    agentchat.rollback_migration_preferences(db)
    agentchat.rollback_migration_preferences(db)  # no-op


def test_apply_after_rollback_recreates_table(db):
    """apply after rollback leaves a usable, empty user_preferences."""
    import agentchat

    agentchat.rollback_migration_preferences(db)
    agentchat.apply_migration_preferences(db)
    n = db.execute(
        "SELECT COUNT(*) FROM user_preferences"
    ).fetchone()[0]
    assert n == 0


# --------------------------------------------------------------------------- #
# Storage layer (get / upsert / delete)
# --------------------------------------------------------------------------- #

def test_get_preferences_returns_defaults_when_row_missing(db):
    """get_preferences(uid) for an unknown user yields documented defaults."""
    uid = _insert_user(db)
    from agentchat.preferences import get_preferences

    p = get_preferences(db, uid)
    assert p.user_id == uid
    assert p.default_channel_id is None
    assert p.theme == "system"
    # No row was inserted by the read path.
    n = db.execute(
        "SELECT COUNT(*) FROM user_preferences WHERE user_id = ?", (uid,)
    ).fetchone()[0]
    assert n == 0


def test_upsert_inserts_with_supplied_fields(db):
    """First upsert inserts a row with the supplied values and stamps now()."""
    uid = _insert_user(db)
    from agentchat.preferences import upsert_preferences

    p = upsert_preferences(
        db, uid, theme="dark", default_channel_id="general"
    )
    assert p.user_id == uid
    assert p.theme == "dark"
    assert p.default_channel_id == "general"
    assert p.created_at != ""
    assert p.updated_at != ""
    assert p.created_at == p.updated_at  # first write: equal


def test_upsert_partial_update_preserves_omitted_fields(db):
    """A subsequent upsert with theme=None keeps the existing theme."""
    uid = _insert_user(db)
    from agentchat.preferences import upsert_preferences

    upsert_preferences(db, uid, theme="dark", default_channel_id="general")
    # Only update the channel; theme must stay "dark".
    p = upsert_preferences(db, uid, default_channel_id="random")
    assert p.theme == "dark"
    assert p.default_channel_id == "random"
    # created_at preserved, updated_at bumped.
    row = db.execute(
        "SELECT created_at, updated_at FROM user_preferences "
        "WHERE user_id = ?",
        (uid,),
    ).fetchone()
    assert row["created_at"] == p.created_at
    assert row["updated_at"] == p.updated_at


def test_upsert_rejects_invalid_theme(db):
    """upsert_preferences validates theme before touching the DB."""
    uid = _insert_user(db)
    from agentchat.preferences import upsert_preferences

    with pytest.raises(ValueError, match="invalid theme"):
        upsert_preferences(db, uid, theme="neon")


def test_upsert_rejects_nonpositive_user_id(db):
    """user_id must be a positive integer."""
    from agentchat.preferences import upsert_preferences

    with pytest.raises(ValueError, match="user_id"):
        upsert_preferences(db, 0, theme="dark")


def test_get_preferences_returns_persisted_row(db):
    """After an upsert, get_preferences returns the persisted state."""
    uid = _insert_user(db)
    from agentchat.preferences import upsert_preferences, get_preferences

    upsert_preferences(db, uid, theme="light", default_channel_id="ops")
    p = get_preferences(db, uid)
    assert p.user_id == uid
    assert p.theme == "light"
    assert p.default_channel_id == "ops"


def test_get_preferences_isolates_users(db):
    """User A's preferences never leak into user B's view."""
    a = _insert_user(db, "alice")
    b = _insert_user(db, "bob")
    from agentchat.preferences import upsert_preferences, get_preferences

    upsert_preferences(db, a, theme="dark", default_channel_id="general")
    upsert_preferences(db, b, theme="light", default_channel_id=None)

    pa = get_preferences(db, a)
    pb = get_preferences(db, b)
    assert pa.theme == "dark"
    assert pa.default_channel_id == "general"
    assert pb.theme == "light"
    assert pb.default_channel_id is None


def test_delete_preferences_returns_true_when_row_existed(db):
    """delete_preferences returns True iff a row was removed."""
    uid = _insert_user(db)
    from agentchat.preferences import upsert_preferences, delete_preferences

    upsert_preferences(db, uid, theme="dark")
    assert delete_preferences(db, uid) is True


def test_delete_preferences_returns_false_when_no_row(db):
    """delete_preferences on an unknown user is a no-op (returns False)."""
    uid = _insert_user(db)
    from agentchat.preferences import delete_preferences

    assert delete_preferences(db, uid) is False


def test_concurrent_upserts_dont_corrupt_row(db):
    """Sequential upserts produce a coherent row (theme + channel both
    reflect the latest non-None inputs).

    True concurrent PUTs would need a second connection to exercise the
    SQLite write lock; the HTTP integration test (test_preferences_api.py)
    covers that. Here we just prove the upsert path itself stays
    consistent across many writes.
    """
    uid = _insert_user(db)
    from agentchat.preferences import upsert_preferences, get_preferences

    for theme in ("light", "dark", "system"):
        upsert_preferences(db, uid, theme=theme)
        p = get_preferences(db, uid)
        assert p.theme == theme  # always the latest

    # And the channel stays at whatever it last was set to.
    p = upsert_preferences(db, uid, default_channel_id="ops")
    assert p.default_channel_id == "ops"
    p = upsert_preferences(db, uid, default_channel_id=None)
    assert p.default_channel_id is None  # explicit None clears
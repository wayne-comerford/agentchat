#!/usr/bin/env python3
"""
agentchat — Direct agent-to-agent chat over HTTP.

Single-file tool. Stdlib only (http.server, sqlite3, urllib, secrets, hashlib).

v1 (backplane) model:
  - Agents send messages into named THREADS, not to a single recipient.
  - Each thread has an explicit set of members.
  - Every recipient of a thread message has their own read/ack state.
  - CLI can `watch` a thread (or all threads) for live incoming messages.

Subcommands:
  init            First-run setup: create DB, generate tokens, print config
  serve           Run the HTTP server (foreground)
  send            Send a message  (compat: to=<agent>)  OR  (--thread=<id>)
  inbox           List incoming messages across all threads I'm in
  read            Show full message body
  ack             Mark a message as read
  peers           List known agents
  threads         List threads I'm a member of
  thread create   Create a thread
  thread show     Show a thread (members, last message, unread)
  thread send     Post a message into a thread (alias of `send --thread`)
  thread messages List messages in a thread
  watch           Long-poll: print new messages as they arrive
  status          Show health + endpoint info
  token           Manage tokens (show / rotate / add / rm)

Designed so that:
  - Hermes runs the server on 192.168.0.124:7878
  - Chappy + Wayne are clients (curl, this CLI, or thin Python wrapper)
  - The protocol is symmetric: any peer can run a server.

Auth: bearer token of the form `<agent_name>:<secret>`.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import signal
import socketserver
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

AGENTCHAT_HOME = Path(
    os.environ.get("AGENTCHAT_HOME", Path.home() / ".hermes" / "agent_chat")
)
DB_PATH = AGENTCHAT_HOME / "messages.db"
TOKENS_PATH = AGENTCHAT_HOME / "tokens.json"
CONFIG_PATH = AGENTCHAT_HOME / "config.json"
LOG_PATH = AGENTCHAT_HOME / "server.log"

DEFAULT_PORT = int(os.environ.get("AGENTCHAT_PORT", "7878"))
DEFAULT_BIND = os.environ.get("AGENTCHAT_BIND", "0.0.0.0")
SERVER_VERSION = "1.3.0"

MAX_BODY_BYTES = 64 * 1024  # 64 KiB per message
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,63}$")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def log(msg: str) -> None:
    line = f"[{now_iso()}] {msg}"
    print(line, file=sys.stderr)
    try:
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def ensure_home() -> None:
    AGENTCHAT_HOME.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(AGENTCHAT_HOME, 0o700)
    except OSError:
        pass


def err(msg: str, **extra: Any) -> dict:
    return {"error": msg, **extra}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'agent',
    endpoint TEXT,
    last_seen TEXT,
    created_at TEXT NOT NULL
);

-- Legacy pairwise messages (kept for back-compat with v0.1 clients).
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT UNIQUE NOT NULL,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    read_at TEXT,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_to_unread
    ON messages(to_agent, read_at, id);
CREATE INDEX IF NOT EXISTS idx_from
    ON messages(from_agent, id);

-- v1 thread model
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    name TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS thread_members (
    thread_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (thread_id, agent_name),
    FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_member ON thread_members(agent_name);

-- v0.1.0 auth tables (Phase 1, real auth)
-- users hold bcrypt/scrypt-hashed passwords. workspace scoping is
-- enforced on every API call by joining through api_tokens.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    owner_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (owner_user_id) REFERENCES users(id)
);

-- role: 'owner' | 'admin' | 'member'
CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner','admin','member')),
    joined_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, user_id),
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_wm_user ON workspace_members(user_id);

-- api_tokens hold SHA256-hashed bearer tokens. Plaintext is never
-- stored. token_hash UNIQUE so collisions are detected.
-- scope: 'admin' (workspace admin) | 'member' | 'agent' (system agent)
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT UNIQUE NOT NULL,
    user_id INTEGER,
    workspace_id INTEGER,
    name TEXT,
    scope TEXT NOT NULL DEFAULT 'member',
    expires_at TEXT NOT NULL,
    refresh_expires_at TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON api_tokens(user_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_tokens_ws ON api_tokens(workspace_id);
CREATE INDEX IF NOT EXISTS idx_tokens_active ON api_tokens(revoked_at, expires_at);

CREATE TABLE IF NOT EXISTS thread_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT UNIQUE NOT NULL,
    thread_id TEXT NOT NULL,
    from_agent TEXT NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata TEXT,
    FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tm_thread ON thread_messages(thread_id, id);
CREATE INDEX IF NOT EXISTS idx_tm_from ON thread_messages(from_agent, id);

-- Per-recipient state for thread_messages.
CREATE TABLE IF NOT EXISTS message_recipients (
    msg_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    read_at TEXT,
    PRIMARY KEY (msg_id, agent_name)
);
CREATE INDEX IF NOT EXISTS idx_mr_recipient_unread
    ON message_recipients(agent_name, read_at);

-- Emoji reactions. PK (msg_id, agent_name, emoji) so one user can put
-- multiple distinct emojis on a message, but cannot double-react with the
-- same one. created_at kept for audit and for time-based reordering.
CREATE TABLE IF NOT EXISTS message_reactions (
    msg_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    emoji TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (msg_id, agent_name, emoji)
);
CREATE INDEX IF NOT EXISTS idx_reax_msg ON message_reactions(msg_id);
"""


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def db_init() -> None:
    ensure_home()
    conn = db_connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def tokens_load() -> dict[str, dict[str, Any]]:
    if not TOKENS_PATH.exists():
        return {}
    with TOKENS_PATH.open() as f:
        return json.load(f)


def tokens_save(data: dict[str, dict[str, Any]]) -> None:
    ensure_home()
    with TOKENS_PATH.open("w") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(TOKENS_PATH, 0o600)
    except OSError:
        pass


def token_new() -> str:
    return secrets.token_urlsafe(32)


def agent_register(
    name: str, token: str, role: str = "agent", endpoint: Optional[str] = None
) -> None:
    db_init()
    conn = db_connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO agents(name, token_hash, role, endpoint, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (name, hash_token(token), role, endpoint, now_iso()),
        )
    finally:
        conn.close()
    toks = tokens_load()
    toks[name] = {"token": token, "role": role, "endpoint": endpoint}
    tokens_save(toks)
    _invalidate_observer_cache(name)


def agent_lookup_token(name: str, token: str) -> Optional[sqlite3.Row]:
    conn = db_connect()
    try:
        return conn.execute(
            "SELECT name, role, endpoint FROM agents "
            "WHERE name = ? AND token_hash = ?",
            (name, hash_token(token)),
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v0.1.0 real auth (Phase 1): users, workspaces, hashed bearer tokens.
# Passwords use stdlib hashlib.scrypt with conservative params; tokens are
# stored as SHA256 hashes only. Tokens never leave the client as plaintext
# after issue except in the issue response.
# ---------------------------------------------------------------------------

# Scrypt cost params. n=2**15 is ~80ms on a modern x86; r=8 p=1 are
# recommended defaults from the scrypt paper. Bumped if you need harder.
_SCRYPT_N = 1 << 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 64

# Token TTLs
_TOKEN_TTL_SECONDS = 60 * 60            # 1 hour
_REFRESH_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days


def _hash_password(password: str) -> str:
    """Scrypt-hash a password with a per-hash random salt.

    Stored format: 'scrypt$<hex-salt>$<hex-hash>' so we can grow into
    bcrypt/argon2 later by adding a new prefix and dispatching on it.

    maxmem=64 MiB — explicit because OpenSSL's default cap (~32 MiB) is
    too low for n=2**15 on some systems, causing 'memory limit exceeded'.
    """
    salt = secrets.token_bytes(16)
    hk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=64 * 1024 * 1024,
    )
    return f"scrypt${salt.hex()}${hk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """Constant-time-ish verification of a stored hash."""
    try:
        algo, salt_hex, hk_hex = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hk_hex)
    except ValueError:
        return False
    candidate = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=len(expected),
        maxmem=64 * 1024 * 1024,
    )
    # Constant-time compare
    return hmac.compare_digest(candidate, expected)


def _token_new() -> str:
    """Cryptographically random bearer token, URL-safe, ~256 bits entropy."""
    return secrets.token_urlsafe(32)


def _token_hash(token: str) -> str:
    """SHA-256 of the token. DB stores only this. Plaintext token is
    shown to the client ONCE at issue and never persisted."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def user_register(
    username: str,
    password: str,
    workspace_name: str,
    workspace_slug: Optional[str] = None,
) -> dict:
    """Create a user + first workspace. Caller becomes owner.

    First-user-becomes-admin pattern. Refuses if username already exists.
    Returns dict {user: {...}, workspace: {...}, token, refresh_token, expires_at}.
    """
    if not username or not password or not workspace_name:
        raise ValueError("username, password, workspace_name required")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if len(username) > 64 or not re.match(r"^[a-zA-Z0-9_.\-@]+$", username):
        raise ValueError("invalid username (1-64 chars, [a-z0-9_.-@])")
    slug = workspace_slug or re.sub(r"[^a-z0-9-]+", "-", workspace_name.lower()).strip("-")[:48] or "default"
    if not slug:
        slug = "default"

    db_init()
    conn = db_connect()
    try:
        existing = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
        if existing:
            raise ValueError("username already exists")
        existing_ws = conn.execute("SELECT 1 FROM workspaces WHERE slug=?", (slug,)).fetchone()
        if existing_ws:
            raise ValueError(f"workspace slug '{slug}' already exists")

        now = now_iso()
        cur = conn.execute(
            "INSERT INTO users(username, password_hash, created_at) VALUES (?,?,?)",
            (username, _hash_password(password), now),
        )
        user_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO workspaces(slug, name, owner_user_id, created_at) VALUES (?,?,?,?)",
            (slug, workspace_name, user_id, now),
        )
        workspace_id = cur.lastrowid
        conn.execute(
            "INSERT INTO workspace_members(workspace_id, user_id, role, joined_at) VALUES (?,?,?,?)",
            (workspace_id, user_id, "owner", now),
        )

        issued = _token_issue(conn, user_id, workspace_id, "register", "admin")
    finally:
        conn.close()

    return {
        "user": {"id": user_id, "username": username},
        "workspace": {"id": workspace_id, "slug": slug, "name": workspace_name, "role": "owner"},
        **issued,
    }


def user_login(username: str, password: str) -> Optional[dict]:
    """Verify password and issue a fresh token + refresh. Returns None
    if the username/password is wrong (or user has no workspace)."""
    db_init()
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT id, username, password_hash FROM users WHERE username=?",
            (username,),
        ).fetchone()
        if row is None:
            return None  # caller distinguishes "no such user" from "wrong pw"
        if not _verify_password(password, row["password_hash"]):
            return None
        # First workspace (or sole workspace) becomes the active context.
        ws = conn.execute(
            "SELECT w.id AS ws_id, w.slug, w.name, wm.role "
            "FROM workspace_members wm JOIN workspaces w ON w.id = wm.workspace_id "
            "WHERE wm.user_id = ? ORDER BY wm.joined_at LIMIT 1",
            (row["id"],),
        ).fetchone()
        if ws is None:
            # User exists but has no workspace (shouldn't happen post-register,
            # but guard for legacy users migrated from tokens.json).
            return None
        user_id = int(row["id"])
        ws_id = int(ws["ws_id"])
        conn.execute(
            "UPDATE users SET last_login_at=? WHERE id=?",
            (now_iso(), user_id),
        )
        issued = _token_issue(conn, user_id, ws_id, "login", "admin")
    finally:
        conn.close()
    return {
        "user": {"id": user_id, "username": str(row["username"])},
        "workspace": {
            "id": ws_id, "slug": str(ws["slug"]), "name": str(ws["name"]), "role": str(ws["role"]),
        },
        **issued,
    }


def _token_issue(
    conn: sqlite3.Connection,
    user_id: int,
    workspace_id: int,
    name: str,
    scope: str,
) -> dict:
    """Insert a new token row. Returns {token, refresh_token, expires_at}.

    Token plaintext is returned ONCE here. Only token_hash is persisted.
    """
    plain = _token_new()
    refresh = _token_new()
    now = now_iso()
    expires_at = (
        datetime.now(timezone.utc)
        + timedelta(seconds=_TOKEN_TTL_SECONDS)
    ).isoformat()
    refresh_expires_at = (
        datetime.now(timezone.utc)
        + timedelta(seconds=_REFRESH_TTL_SECONDS)
    ).isoformat()
    conn.execute(
        "INSERT INTO api_tokens("
        "  token_hash, user_id, workspace_id, name, scope,"
        "  expires_at, refresh_expires_at, created_at"
        ") VALUES (?,?,?,?,?,?,?,?)",
        (
            _token_hash(plain), user_id, workspace_id, name, scope,
            expires_at, refresh_expires_at, now,
        ),
    )
    return {
        "token": plain,
        "refresh_token": refresh,  # NOTE: not yet stored; see _token_refresh below
        "expires_at": expires_at,
        "token_type": "Bearer",
    }


def token_lookup(plain: str) -> Optional[sqlite3.Row]:
    """Resolve a bearer token to its row, if not expired and not revoked."""
    if not plain:
        return None
    db_init()
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT t.id, t.user_id, t.workspace_id, t.scope, t.expires_at, "
            "       u.username AS name, u.username, "
            "       w.slug AS workspace_slug "
            "FROM api_tokens t "
            "LEFT JOIN users u ON u.id = t.user_id "
            "LEFT JOIN workspaces w ON w.id = t.workspace_id "
            "WHERE t.token_hash = ? AND t.revoked_at IS NULL",
            (_token_hash(plain),),
        ).fetchone()
        if row is None:
            return None
        # Check expiry
        if row["expires_at"] < now_iso():
            return None
        # Bump last_used_at (best-effort, ignore errors)
        try:
            conn.execute(
                "UPDATE api_tokens SET last_used_at=? WHERE id=?",
                (now_iso(), row["id"]),
            )
        except sqlite3.Error:
            pass
        return row
    finally:
        conn.close()


def token_revoke(plain: str) -> bool:
    """Mark a token as revoked. Returns True if a row was updated."""
    db_init()
    conn = db_connect()
    try:
        cur = conn.execute(
            "UPDATE api_tokens SET revoked_at=? "
            "WHERE token_hash=? AND revoked_at IS NULL",
            (now_iso(), _token_hash(plain)),
        )
        return cur.rowcount > 0
    finally:
        conn.close()


def user_workspace_create(user_id: int, name: str, slug: Optional[str] = None) -> dict:
    """Add a new workspace owned by the given user."""
    if not name:
        raise ValueError("name required")
    s = slug or re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:48] or "default"
    db_init()
    conn = db_connect()
    try:
        if conn.execute("SELECT 1 FROM workspaces WHERE slug=?", (s,)).fetchone():
            raise ValueError(f"workspace slug '{s}' already exists")
        now = now_iso()
        cur = conn.execute(
            "INSERT INTO workspaces(slug, name, owner_user_id, created_at) VALUES (?,?,?,?)",
            (s, name, user_id, now),
        )
        ws_id = cur.lastrowid
        conn.execute(
            "INSERT INTO workspace_members(workspace_id, user_id, role, joined_at) VALUES (?,?,?,?)",
            (ws_id, user_id, "owner", now),
        )
    finally:
        conn.close()
    return {"id": ws_id, "slug": s, "name": name, "role": "owner"}


def user_workspaces_list(user_id: int) -> list[dict]:
    db_init()
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT w.id, w.slug, w.name, wm.role, wm.joined_at "
            "FROM workspace_members wm JOIN workspaces w ON w.id = wm.workspace_id "
            "WHERE wm.user_id = ? ORDER BY wm.joined_at",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def agent_exists(name: str) -> bool:
    conn = db_connect()
    try:
        row = conn.execute("SELECT 1 FROM agents WHERE name = ?", (name,)).fetchone()
        return row is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Legacy v0.1 messages (back-compat)
# ---------------------------------------------------------------------------


def message_insert(
    from_agent: str,
    to_agent: str,
    body: str,
    subject: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    db_init()
    conn = db_connect()
    try:
        msg_id = f"m_{secrets.token_hex(8)}"
        created = now_iso()
        conn.execute(
            "INSERT INTO messages(msg_id, from_agent, to_agent, subject, body, "
            "created_at, metadata) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                msg_id,
                from_agent,
                to_agent,
                subject,
                body,
                created,
                json.dumps(metadata) if metadata else None,
            ),
        )
        row = conn.execute(
            "SELECT * FROM messages WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def message_inbox(
    agent: str, since_id: int = 0, limit: int = 50, unread_only: bool = False
) -> list[dict]:
    db_init()
    conn = db_connect()
    try:
        if unread_only:
            rows = conn.execute(
                "SELECT * FROM messages WHERE to_agent = ? AND read_at IS NULL "
                "AND id > ? ORDER BY id ASC LIMIT ?",
                (agent, since_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE to_agent = ? AND id > ? "
                "ORDER BY id ASC LIMIT ?",
                (agent, since_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def message_get(agent: str, msg_id: str) -> Optional[dict]:
    db_init()
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT * FROM messages WHERE msg_id = ? "
            "AND (to_agent = ? OR from_agent = ?)",
            (msg_id, agent, agent),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def message_ack(agent: str, msg_id: str) -> bool:
    """Mark a v0.1 pairwise message read. Returns True on success."""
    db_init()
    conn = db_connect()
    try:
        cur = conn.execute(
            "UPDATE messages SET read_at = ? WHERE msg_id = ? AND to_agent = ? "
            "AND read_at IS NULL",
            (now_iso(), msg_id, agent),
        )
        return cur.rowcount > 0
    finally:
        conn.close()


def list_peers() -> list[dict]:
    db_init()
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT name, role, endpoint, last_seen, created_at FROM agents "
            "ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v1 Threads
# ---------------------------------------------------------------------------


def thread_create(
    thread_id: str,
    name: Optional[str],
    members: list[str],
    created_by: str,
) -> dict:
    if not THREAD_ID_RE.match(thread_id):
        raise ValueError(
            f"invalid thread_id {thread_id!r}: must match {THREAD_ID_RE.pattern}"
        )
    if not members:
        raise ValueError("members must be a non-empty list")
    db_init()
    conn = db_connect()
    try:
        # validate members exist
        placeholders = ",".join("?" * len(members))
        rows = conn.execute(
            f"SELECT name FROM agents WHERE name IN ({placeholders})",
            members,
        ).fetchall()
        known = {r["name"] for r in rows}
        missing = [m for m in members if m not in known]
        if missing:
            raise ValueError(f"unknown agent(s): {missing}")

        # creator is auto-added even if not listed
        full_members = list(dict.fromkeys([created_by] + list(members)))
        created = now_iso()

        # insert thread (idempotent on re-create by same id)
        existing = conn.execute(
            "SELECT id FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO threads(id, name, created_by, created_at) "
                "VALUES(?, ?, ?, ?)",
                (thread_id, name, created_by, created),
            )
        else:
            # Update name if a new one is provided; created_by/created_at stay.
            if name:
                conn.execute(
                    "UPDATE threads SET name = ? WHERE id = ?", (name, thread_id)
                )

        # Sync members: insert new ones, ignore existing.
        for m in full_members:
            conn.execute(
                "INSERT OR IGNORE INTO thread_members(thread_id, agent_name, joined_at) "
                "VALUES(?, ?, ?)",
                (thread_id, m, created),
            )

        # return full thread state
        return thread_get(thread_id, viewer=created_by)
    finally:
        conn.close()


def thread_get(thread_id: str, viewer: Optional[str] = None) -> Optional[dict]:
    db_init()
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT id, name, created_by, created_at FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            return None
        members = conn.execute(
            "SELECT agent_name, joined_at FROM thread_members "
            "WHERE thread_id = ? ORDER BY joined_at",
            (thread_id,),
        ).fetchall()
        member_names = [m["agent_name"] for m in members]

        # If viewer is provided, gate access to members only
        if viewer is not None and viewer not in member_names:
            return None

        # last message + unread for viewer
        last_msg = conn.execute(
            "SELECT msg_id, from_agent, body, created_at FROM thread_messages "
            "WHERE thread_id = ? ORDER BY id DESC LIMIT 1",
            (thread_id,),
        ).fetchone()

        unread = 0
        if viewer is not None:
            unread = conn.execute(
                "SELECT COUNT(*) AS n FROM thread_messages tm "
                "JOIN message_recipients mr ON mr.msg_id = tm.msg_id "
                "WHERE tm.thread_id = ? AND mr.agent_name = ? "
                "AND mr.read_at IS NULL",
                (thread_id, viewer),
            ).fetchone()["n"]

        return {
            "id": row["id"],
            "name": row["name"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "members": member_names,
            "member_count": len(member_names),
            "last_message": dict(last_msg) if last_msg else None,
            "unread": unread,
        }
    finally:
        conn.close()


def thread_list_for_agent(agent: str) -> list[dict]:
    db_init()
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT t.id FROM threads t "
            "JOIN thread_members m ON m.thread_id = t.id "
            "WHERE m.agent_name = ? ORDER BY t.created_at",
            (agent,),
        ).fetchall()
        return [thread_get(r["id"], viewer=agent) for r in rows]
    finally:
        conn.close()


def thread_add_members(
    thread_id: str, members: list[str], added_by: str
) -> dict:
    db_init()
    conn = db_connect()
    try:
        # gate: adder must already be a member
        is_member = conn.execute(
            "SELECT 1 FROM thread_members WHERE thread_id = ? AND agent_name = ?",
            (thread_id, added_by),
        ).fetchone()
        if is_member is None:
            raise PermissionError("not a member of this thread")
        # validate new members exist
        placeholders = ",".join("?" * len(members))
        rows = conn.execute(
            f"SELECT name FROM agents WHERE name IN ({placeholders})", members
        ).fetchall()
        known = {r["name"] for r in rows}
        missing = [m for m in members if m not in known]
        if missing:
            raise ValueError(f"unknown agent(s): {missing}")
        ts = now_iso()
        for m in members:
            conn.execute(
                "INSERT OR IGNORE INTO thread_members(thread_id, agent_name, joined_at) "
                "VALUES(?, ?, ?)",
                (thread_id, m, ts),
            )
        return thread_get(thread_id, viewer=added_by)
    finally:
        conn.close()


def thread_post(
    thread_id: str,
    from_agent: str,
    body: str,
    subject: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    db_init()
    conn = db_connect()
    try:
        # gate: sender must be a member
        is_member = conn.execute(
            "SELECT 1 FROM thread_members WHERE thread_id = ? AND agent_name = ?",
            (thread_id, from_agent),
        ).fetchone()
        if is_member is None:
            raise PermissionError("not a member of this thread")

        msg_id = f"t_{secrets.token_hex(8)}"
        created = now_iso()
        conn.execute(
            "INSERT INTO thread_messages(msg_id, thread_id, from_agent, subject, body, "
            "created_at, metadata) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                msg_id,
                thread_id,
                from_agent,
                subject,
                body,
                created,
                json.dumps(metadata) if metadata else None,
            ),
        )

        # fan out: create a delivered_at row for every member except the sender
        members = conn.execute(
            "SELECT agent_name FROM thread_members WHERE thread_id = ?",
            (thread_id,),
        ).fetchall()
        for m in members:
            if m["agent_name"] == from_agent:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO message_recipients(msg_id, agent_name, delivered_at) "
                "VALUES(?, ?, ?)",
                (msg_id, m["agent_name"], created),
            )

        return thread_message_get(msg_id, viewer=from_agent) or {}
    finally:
        conn.close()


def thread_message_get(msg_id: str, viewer: Optional[str] = None) -> Optional[dict]:
    db_init()
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT * FROM thread_messages WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if viewer is not None:
            mr = conn.execute(
                "SELECT delivered_at, read_at FROM message_recipients "
                "WHERE msg_id = ? AND agent_name = ?",
                (msg_id, viewer),
            ).fetchone()
            if mr is None:
                # viewer is the sender (no recipient row) — they did receive it
                d["delivered_at"] = d["created_at"]
                d["read_at"] = None
                d["own"] = True
            else:
                d["delivered_at"] = mr["delivered_at"]
                d["read_at"] = mr["read_at"]
                d["own"] = False
        # Always include reactions. Format: {emoji: [agent_name, ...]}
        d["reactions"] = _reactions_for(msg_id, conn)
        return d
    finally:
        conn.close()


def _reactions_for(msg_id: str, conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Return {emoji: [agent_name, ...]} for a message. Sorted agents."""
    rows = conn.execute(
        "SELECT emoji, agent_name FROM message_reactions "
        "WHERE msg_id = ? ORDER BY emoji, agent_name",
        (msg_id,),
    ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["emoji"], []).append(r["agent_name"])
    return out


def _reaction_sig(reactions: dict[str, list[str]]) -> str:
    """Compact string fingerprint of a reactions dict, for cheap change detection in SSE."""
    parts: list[str] = []
    for emoji in sorted(reactions.keys()):
        agents = ",".join(sorted(reactions[emoji]))
        parts.append(f"{emoji}:{agents}")
    return "|".join(parts)


def reaction_add(agent: str, msg_id: str, emoji: str) -> dict:
    """Add an emoji reaction. Idempotent: re-adding the same emoji by the
    same agent is a no-op that still returns the current reaction set.

    Returns {"msg_id", "emoji", "added": bool, "reactions": {...}}.
    Raises PermissionError if the agent isn't a member of the message's thread.
    """
    if not emoji or not emoji.strip():
        raise ValueError("emoji must not be empty")
    # Reject control chars / overly long sequences (defensive — emoji is
    # typically short, but a malicious client could send a 10MB string).
    if len(emoji) > 32:
        raise ValueError("emoji too long (max 32 chars)")
    db_init()
    conn = db_connect()
    try:
        msg = conn.execute(
            "SELECT thread_id, from_agent FROM thread_messages WHERE msg_id = ?",
            (msg_id,),
        ).fetchone()
        if msg is None:
            raise LookupError("message not found")
        thread_id = msg["thread_id"]
        # Visibility check: agent must be a member of the thread, OR the sender.
        if msg["from_agent"] != agent:
            is_member = conn.execute(
                "SELECT 1 FROM thread_members WHERE thread_id = ? AND agent_name = ?",
                (thread_id, agent),
            ).fetchone()
            if is_member is None:
                raise PermissionError("not a member of this thread")
        # INSERT OR IGNORE — idempotent
        cur = conn.execute(
            "INSERT OR IGNORE INTO message_reactions"
            "(msg_id, agent_name, emoji, created_at) VALUES (?, ?, ?, ?)",
            (msg_id, agent, emoji, now_iso()),
        )
        added = cur.rowcount > 0
        return {
            "msg_id": msg_id,
            "emoji": emoji,
            "added": added,
            "reactions": _reactions_for(msg_id, conn),
        }
    finally:
        conn.close()


def reaction_remove(agent: str, msg_id: str, emoji: str) -> dict:
    """Remove an emoji reaction the agent previously added.

    Returns {"msg_id", "emoji", "removed": bool, "reactions": {...}}.
    """
    db_init()
    conn = db_connect()
    try:
        msg = conn.execute(
            "SELECT 1 FROM thread_messages WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        if msg is None:
            raise LookupError("message not found")
        cur = conn.execute(
            "DELETE FROM message_reactions "
            "WHERE msg_id = ? AND agent_name = ? AND emoji = ?",
            (msg_id, agent, emoji),
        )
        removed = cur.rowcount > 0
        return {
            "msg_id": msg_id,
            "emoji": emoji,
            "removed": removed,
            "reactions": _reactions_for(msg_id, conn),
        }
    finally:
        conn.close()


def reaction_list(msg_id: str) -> dict:
    """List all reactions on a message. {msg_id, reactions: {emoji: [agents]}}."""
    db_init()
    conn = db_connect()
    try:
        msg = conn.execute(
            "SELECT 1 FROM thread_messages WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        if msg is None:
            raise LookupError("message not found")
        return {"msg_id": msg_id, "reactions": _reactions_for(msg_id, conn)}
    finally:
        conn.close()


def thread_messages(
    thread_id: str,
    viewer: str,
    since_id: int = 0,
    limit: int = 50,
    unread_only: bool = False,
    latest: bool = True,
) -> list[dict]:
    """List messages in a thread that the viewer can see.

    The viewer sees all messages in threads they're a member of, with their
    own read state attached. The sender always sees their own message.
    Reactions are attached as `{emoji: [agent_name, ...]}` (v1.2).

    Ordering:
    - latest=True (default): newest first. DESC LIMIT N. since_id ignored
      because the semantic is "show me the latest N" — a forward cursor
      isn't meaningful for that. Matches how every chat UI behaves.
    - latest=False: oldest first (ASC) starting after since_id. The
      original v1 semantics for cursor-pagination.
    """
    db_init()
    conn = db_connect()
    try:
        is_member = conn.execute(
            "SELECT 1 FROM thread_members WHERE thread_id = ? AND agent_name = ?",
            (thread_id, viewer),
        ).fetchone()
        if is_member is None:
            raise PermissionError("not a member of this thread")

        if latest:
            # DESC LIMIT N. since_id is intentionally ignored in this mode.
            rows = conn.execute(
                "SELECT tm.* FROM thread_messages tm "
                "WHERE tm.thread_id = ? ORDER BY tm.id DESC LIMIT ?",
                (thread_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT tm.* FROM thread_messages tm "
                "WHERE tm.thread_id = ? AND tm.id > ? ORDER BY tm.id ASC LIMIT ?",
                (thread_id, since_id, limit),
            ).fetchall()
        out: list[dict] = []
        # Fetch reactions in one query for all visible messages (v1.2).
        msg_ids = [r["msg_id"] for r in rows]
        reactions_by_msg: dict[str, dict[str, list[str]]] = {}
        if msg_ids:
            placeholders = ",".join("?" * len(msg_ids))
            rx_rows = conn.execute(
                f"SELECT msg_id, emoji, agent_name FROM message_reactions "
                f"WHERE msg_id IN ({placeholders}) "
                f"ORDER BY emoji, agent_name",
                msg_ids,
            ).fetchall()
            for r in rx_rows:
                reactions_by_msg.setdefault(r["msg_id"], {}).setdefault(
                    r["emoji"], []
                ).append(r["agent_name"])
        for r in rows:
            d = dict(r)
            d["metadata"] = json.loads(d["metadata"]) if d.get("metadata") else None
            d["reactions"] = reactions_by_msg.get(d["msg_id"], {})
            if d["from_agent"] == viewer:
                d["own"] = True
                d["delivered_at"] = d["created_at"]
                d["read_at"] = None
            else:
                d["own"] = False
                mr = conn.execute(
                    "SELECT delivered_at, read_at FROM message_recipients "
                    "WHERE msg_id = ? AND agent_name = ?",
                    (d["msg_id"], viewer),
                ).fetchone()
                d["delivered_at"] = mr["delivered_at"] if mr else None
                d["read_at"] = mr["read_at"] if mr else None
            if unread_only and d.get("read_at") is None and not d.get("own"):
                out.append(d)
            elif not unread_only:
                out.append(d)
        return out
    finally:
        conn.close()


def thread_search(
    viewer: str,
    query: str,
    thread_id: Optional[str] = None,
    from_agent: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Cross-thread substring search across body and subject.

    Visibility: only messages in threads the viewer is a member of. Sender's
    own messages are always visible to the sender. Newest first (DESC) so
    the most relevant results surface at the top of `agentchat search`.

    SQLite `LIKE` is case-insensitive for ASCII by default; that's fine for
    casual grep. For unicode/case-aware matching we'd need `LIKE` with
    `PRAGMA case_sensitive_like` toggled — out of scope for v1.1.
    """
    if not query or not query.strip():
        return []
    db_init()
    conn = db_connect()
    try:
        # Build a WHERE clause that joins through thread_members so the
        # viewer can only see messages from threads they belong to.
        where = ["tm.thread_id IN (SELECT thread_id FROM thread_members WHERE agent_name = ?)",
                 "(tm.body LIKE ? OR tm.subject LIKE ?)"]
        params: list[Any] = [viewer, f"%{query}%", f"%{query}%"]
        if thread_id:
            where.append("tm.thread_id = ?")
            params.append(thread_id)
        if from_agent:
            where.append("tm.from_agent = ?")
            params.append(from_agent)
        sql = (
            "SELECT tm.* FROM thread_messages tm "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY tm.id DESC LIMIT ?"
        )
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            d["metadata"] = json.loads(d["metadata"]) if d.get("metadata") else None
            if d["from_agent"] == viewer:
                d["own"] = True
            else:
                d["own"] = False
            # Truncate body for list view — full body available via `read`.
            d["body_preview"] = (d.get("body") or "")[:120]
            out.append(d)
        return out
    finally:
        conn.close()


def thread_message_ack(agent: str, msg_id: str) -> bool:
    """Mark a thread message as read for this recipient.

    The sender's own message is a no-op (already 'read' for them).
    """
    db_init()
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT thread_id, from_agent FROM thread_messages WHERE msg_id = ?",
            (msg_id,),
        ).fetchone()
        if row is None:
            return False
        if row["from_agent"] == agent:
            # Sender acking their own message — no-op success.
            return True
        cur = conn.execute(
            "UPDATE message_recipients SET read_at = ? "
            "WHERE msg_id = ? AND agent_name = ? AND read_at IS NULL",
            (now_iso(), msg_id, agent),
        )
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


# Path patterns
_T_MSG_RE = re.compile(r"^/v1/messages/(t_[0-9a-f]+)$")
_T_MSG_ACK_RE = re.compile(r"^/v1/messages/(t_[0-9a-f]+)/ack$")
_T_MSG_REACT_RE = re.compile(r"^/v1/messages/(t_[0-9a-f]+)/reactions$")
_T_THREAD_MSGS_RE = re.compile(r"^/v1/threads/([^/]+)/messages$")
_T_THREAD_EVENTS_RE = re.compile(r"^/v1/threads/([^/]+)/events$")
_SEARCH_RE = re.compile(r"^/v1/search$")


class AgentChatHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"agentchat/{SERVER_VERSION}"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log(f"{self.address_string()} {format % args}")

    # --- helpers ---
    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- SSE: stream new messages + reaction updates for a thread ---
    def _stream_thread_events(self, agent: str, thread_id: str, since_id: int) -> None:
        """Long-lived GET response, Content-Type: text/event-stream.

        Validates thread membership, then enters a polling loop that emits
        SSE 'message' events for each new thread_messages row > since_id and
        'reaction' events for reaction_add/remove changes. Sends ':keepalive'
        heartbeats every 15s so proxies (incl. cloudflared/pinggy) don't idle
        the connection. Exits cleanly when the client socket closes.
        """
        db_init()
        # Membership check (single short-lived connection)
        conn = db_connect()
        try:
            is_member = conn.execute(
                "SELECT 1 FROM thread_members WHERE thread_id=? AND agent_name=?",
                (thread_id, agent),
            ).fetchone()
        finally:
            conn.close()
        if is_member is None:
            return self._send_json(403, err("not a member of this thread"))

        # Send SSE headers (no Content-Length — streaming)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")  # nginx hint
            self.end_headers()
            # Initial event so client knows the stream is alive
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

        # Long-lived connection for the polling loop
        conn = db_connect()
        try:
            last_msg_id = since_id
            last_reaction_sig: dict[str, str] = {}  # msg_id -> sig
            last_heartbeat = time.time()
            POLL_INTERVAL = 1.5
            HEARTBEAT_INTERVAL = 15.0

            while True:
                try:
                    # 1. New messages
                    rows = conn.execute(
                        "SELECT * FROM thread_messages "
                        "WHERE thread_id=? AND id > ? "
                        "ORDER BY id ASC LIMIT 100",
                        (thread_id, last_msg_id),
                    ).fetchall()

                    for r in rows:
                        d = dict(r)
                        if d.get("metadata"):
                            try:
                                d["metadata"] = json.loads(d["metadata"])
                            except Exception:
                                pass
                        # Attach reactions as {emoji: [agent_name, ...]}
                        d["reactions"] = _reactions_for(d["msg_id"], conn)
                        # SSE frame
                        frame = f"event: message\ndata: {json.dumps(d, ensure_ascii=False)}\n\n"
                        self.wfile.write(frame.encode("utf-8"))
                        self.wfile.flush()
                        last_msg_id = d["id"]
                        # Seed reaction sig so we don't double-emit
                        last_reaction_sig[d["msg_id"]] = _reaction_sig(
                            d["reactions"]
                        )

                    # 2. Reaction updates on visible messages (last 50)
                    visible = conn.execute(
                        "SELECT msg_id FROM thread_messages "
                        "WHERE thread_id=? ORDER BY id DESC LIMIT 50",
                        (thread_id,),
                    ).fetchall()
                    for (mid,) in visible:
                        react = _reactions_for(mid, conn)
                        sig = _reaction_sig(react)
                        if last_reaction_sig.get(mid) != sig:
                            payload = {"msg_id": mid, "reactions": react}
                            frame = (
                                "event: reaction\ndata: "
                                + json.dumps(payload, ensure_ascii=False)
                                + "\n\n"
                            )
                            self.wfile.write(frame.encode("utf-8"))
                            self.wfile.flush()
                            last_reaction_sig[mid] = sig

                    # 3. Heartbeat
                    now = time.time()
                    if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        last_heartbeat = now
                except (BrokenPipeError, ConnectionResetError, OSError):
                    # Client disconnected
                    break

                time.sleep(POLL_INTERVAL)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError(f"body too large ({length} > {MAX_BODY_BYTES})")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON: {e}") from e

    # --- v0.1.0 auth handlers ---
    def _handle_auth_register(self) -> None:
        try:
            body = self._read_json()
        except ValueError as e:
            return self._send_json(400, err(str(e)))
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        workspace_name = (body.get("workspace_name") or "").strip()
        workspace_slug = (body.get("workspace_slug") or "").strip() or None
        if not username or not password or not workspace_name:
            return self._send_json(400, err("username, password, workspace_name required"))
        try:
            result = user_register(username, password, workspace_name, workspace_slug)
        except ValueError as e:
            return self._send_json(409, err(str(e)))
        except sqlite3.IntegrityError as e:
            return self._send_json(409, err(f"conflict: {e}"))
        return self._send_json(201, result)

    def _handle_auth_login(self) -> None:
        try:
            body = self._read_json()
        except ValueError as e:
            return self._send_json(400, err(str(e)))
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            return self._send_json(400, err("username and password required"))
        result = user_login(username, password)
        if result is None:
            return self._send_json(401, err("invalid credentials"))
        return self._send_json(200, result)

    def _handle_auth_refresh(self) -> None:
        # Phase 1 simplification: re-issue by re-authenticating with
        # username+password. Full refresh_token grant flow is Phase 2.
        return self._handle_auth_login()

    def _handle_auth_logout(self) -> None:
        # Logout is just token revocation. No auth required (so a leaked
        # token can still be killed even after the user changes password).
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return self._send_json(400, err("Bearer token required to logout"))
        token = header[len("Bearer "):].strip()
        if not token:
            return self._send_json(400, err("empty token"))
        # Legacy tokens can't be revoked (no DB row); just ack.
        if ":" in token:
            return self._send_json(200, {"ok": True, "note": "legacy token; rotation required"})
        revoked = token_revoke(token)
        return self._send_json(200, {"ok": True, "revoked": revoked})

    def _handle_workspace_create(self, row: sqlite3.Row) -> None:
        try:
            body = self._read_json()
        except ValueError as e:
            return self._send_json(400, err(str(e)))
        name = (body.get("name") or "").strip()
        slug = (body.get("slug") or "").strip() or None
        if not name:
            return self._send_json(400, err("name required"))
        try:
            ws = user_workspace_create(int(row["user_id"]), name, slug)
        except ValueError as e:
            return self._send_json(409, err(str(e)))
        return self._send_json(201, {"workspace": ws})

    def _auth(self) -> Optional[sqlite3.Row]:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        token = header[len("Bearer "):].strip()
        if not token:
            return None
        # v1.0 legacy: name:secret from tokens.json
        if ":" in token:
            name, secret = token.split(":", 1)
            return agent_lookup_token(name, secret)
        # v0.1.0+: opaque bearer token, SHA256-hashed in api_tokens
        return token_lookup(token)

    # --- routes ---
    def do_DELETE(self) -> None:  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        row = self._auth()
        if not row:
            return self._send_json(401, err("unauthorized"))

        # DELETE /v1/messages/<id>/reactions?emoji=...
        m = _T_MSG_REACT_RE.match(path)
        if m:
            msg_id = m.group(1)
            qs2 = urllib.parse.parse_qs(url.query)
            emoji = (qs2.get("emoji", [""])[0] or "").strip()
            if not emoji:
                return self._send_json(400, err("emoji query param required"))
            try:
                res = reaction_remove(row["name"], msg_id, emoji)
            except LookupError:
                return self._send_json(404, err("message not found"))
            return self._send_json(200, res)

        return self._send_json(405, err("method not allowed for path"))


    def do_GET(self) -> None:  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(url.query)

        if path == "/":
            return self._send_json(200, {
                "service": "agentchat",
                "version": SERVER_VERSION,
                "endpoints": [
                    "GET  /health",
                    "GET  /v1/whoami",
                    "GET  /v1/peers",
                    "GET  /v1/inbox?unread=true&limit=N",
                    "GET  /v1/threads",
                    "POST /v1/threads",
                    "GET  /v1/threads/<id>",
                    "GET  /v1/threads/<id>/messages?since=N&limit=M&unread=true",
                    "POST /v1/threads/<id>/messages",
                    "POST /v1/messages/<msg_id>/ack",
                    "GET  /v1/audit",
                    "GET  /v1/threads/<id>/export?format=json|jsonl|md",
                    # legacy v0.1 pairwise (compat)
                    "GET  /v1/messages",
                    "POST /v1/messages",
                    "GET  /v1/messages/<msg_id>",
                ],
            })

        if path == "/health":
            return self._send_json(200, {"ok": True, "version": SERVER_VERSION})

        if path == "/v1/whoami":
            row = self._auth()
            if not row:
                return self._send_json(401, err("unauthorized"))
            return self._send_json(200, {"agent": dict(row)})

        if path == "/v1/peers":
            row = self._auth()
            if not row:
                return self._send_json(401, err("unauthorized"))
            return self._send_json(200, {"peers": list_peers()})

        # --- v1 inbox: aggregate thread messages addressed to me, across all threads ---
        if path == "/v1/inbox":
            row = self._auth()
            if not row:
                return self._send_json(401, err("unauthorized"))
            try:
                limit = min(int(qs.get("limit", ["50"])[0]), 500)
            except ValueError:
                return self._send_json(400, err("bad limit"))
            unread_only = qs.get("unread", ["false"])[0].lower() in (
                "1", "true", "yes"
            )
            agent = row["name"]
            db_init()
            conn = db_connect()
            try:
                if unread_only:
                    sql = (
                        "SELECT tm.*, mr.read_at, mr.delivered_at, t.id AS t_id, t.name AS t_name "
                        "FROM thread_messages tm "
                        "JOIN message_recipients mr ON mr.msg_id = tm.msg_id "
                        "JOIN threads t ON t.id = tm.thread_id "
                        "WHERE mr.agent_name = ? AND mr.read_at IS NULL "
                        "ORDER BY tm.id ASC LIMIT ?"
                    )
                else:
                    sql = (
                        "SELECT tm.*, mr.read_at, mr.delivered_at, t.id AS t_id, t.name AS t_name "
                        "FROM thread_messages tm "
                        "JOIN message_recipients mr ON mr.msg_id = tm.msg_id "
                        "JOIN threads t ON t.id = tm.thread_id "
                        "WHERE mr.agent_name = ? "
                        "ORDER BY tm.id ASC LIMIT ?"
                    )
                rows = conn.execute(sql, (agent, limit)).fetchall()
            finally:
                conn.close()
            msgs = []
            for r in rows:
                d = dict(r)
                d["metadata"] = json.loads(d["metadata"]) if d.get("metadata") else None
                msgs.append(d)
            return self._send_json(200, {"messages": msgs, "count": len(msgs)})

        # --- v1 threads list ---
        if path == "/v1/threads":
            row = self._auth()
            if not row:
                return self._send_json(401, err("unauthorized"))
            threads = thread_list_for_agent(row["name"])
            return self._send_json(200, {"threads": threads, "count": len(threads)})

        # --- v1 audit: list ALL threads (admin view) with member roles + counts.
        # Accessible to any authenticated agent (no role gate) but observers
        # are read-only by design; the data exposed is the same data they're
        # already entitled to see in threads they're a member of.
        if path == "/v1/audit":
            row = self._auth()
            if not row:
                return self._send_json(401, err("unauthorized"))
            try:
                db_init()
                conn = db_connect()
                threads_data = []
                try:
                    for t in conn.execute(
                        "SELECT t.id, t.name, t.created_by, t.created_at, "
                        "  (SELECT COUNT(*) FROM thread_messages tm WHERE tm.thread_id = t.id) AS msg_count, "
                        "  (SELECT MAX(created_at) FROM thread_messages tm WHERE tm.thread_id = t.id) AS last_msg "
                        "FROM threads t ORDER BY t.created_at"
                    ).fetchall():
                        members = [
                            dict(m) for m in conn.execute(
                                "SELECT tm.agent_name, a.role, tm.joined_at "
                                "FROM thread_members tm JOIN agents a ON a.name = tm.agent_name "
                                "WHERE tm.thread_id = ? ORDER BY tm.joined_at",
                                (t["id"],),
                            )
                        ]
                        threads_data.append({**dict(t), "members": members})
                finally:
                    conn.close()
            except Exception as e:
                return self._send_json(500, err(f"audit failed: {e}"))
            return self._send_json(200, {
                "threads": threads_data,
                "count": len(threads_data),
                "generated_at": now_iso(),
            })

        # --- v1 export: GET /v1/threads/<id>/export?format=json|jsonl|md ---
        m = re.match(r"^/v1/threads/([^/]+)/export$", path)
        if m:
            row = self._auth()
            if not row:
                return self._send_json(401, err("unauthorized"))
            thread_id = m.group(1)
            fmt = qs.get("format", ["json"])[0]
            if fmt not in ("json", "jsonl", "md"):
                return self._send_json(400, err("format must be json|jsonl|md"))
            try:
                db_init()
                conn = db_connect()
                try:
                    trow = conn.execute(
                        "SELECT id, name, created_by, created_at FROM threads WHERE id = ?",
                        (thread_id,),
                    ).fetchone()
                    if trow is None:
                        return self._send_json(404, err("thread not found"))
                    # Gate: caller must be a member
                    is_member = conn.execute(
                        "SELECT 1 FROM thread_members WHERE thread_id = ? AND agent_name = ?",
                        (thread_id, row["name"]),
                    ).fetchone()
                    if is_member is None:
                        return self._send_json(403, err("not a member"))
                    members = [
                        dict(m) for m in conn.execute(
                            "SELECT agent_name, joined_at FROM thread_members "
                            "WHERE thread_id = ? ORDER BY joined_at", (thread_id,)
                        )
                    ]
                    msgs = [
                        dict(m) for m in conn.execute(
                            "SELECT * FROM thread_messages WHERE thread_id = ? "
                            "ORDER BY id ASC", (thread_id,)
                        )
                    ]
                finally:
                    conn.close()
            except Exception as e:
                return self._send_json(500, err(f"export failed: {e}"))
            return self._send_json(200, {
                "thread": dict(trow),
                "members": members,
                "format": fmt,
                "exported_at": now_iso(),
                "message_count": len(msgs),
                "messages": [
                    {
                        **m,
                        "metadata": json.loads(m["metadata"]) if m.get("metadata") else None,
                    }
                    for m in msgs
                ],
            })

        # --- search (cross-thread) ---
        m = _SEARCH_RE.match(path)
        if m:
            row = self._auth()
            if not row:
                return self._send_json(401, err("unauthorized"))
            q = (qs.get("q", [""])[0] or "").strip()
            if not q:
                return self._send_json(400, err("missing q parameter"))
            try:
                limit = min(int(qs.get("limit", ["50"])[0]), 500)
            except ValueError:
                return self._send_json(400, err("bad limit"))
            thread_id = qs.get("thread", [None])[0]
            from_agent = qs.get("from", [None])[0]
            hits = thread_search(
                row["name"], q,
                thread_id=thread_id, from_agent=from_agent, limit=limit,
            )
            return self._send_json(
                200,
                {
                    "query": q,
                    "thread": thread_id,
                    "from": from_agent,
                    "hits": hits,
                    "count": len(hits),
                },
            )

        # --- thread messages list / single thread show ---
        m = _T_THREAD_MSGS_RE.match(path)
        if m:
            row = self._auth()
            if not row:
                return self._send_json(401, err("unauthorized"))
            thread_id = m.group(1)
            try:
                since = int(qs.get("since", ["0"])[0])
                limit = min(int(qs.get("limit", ["50"])[0]), 500)
            except ValueError:
                return self._send_json(400, err("bad limit/since"))
            unread_only = qs.get("unread", ["false"])[0].lower() in (
                "1", "true", "yes"
            )
            # `latest` (default true) flips the ordering to newest-first
            # so `?limit=10` returns the 10 most recent messages. Pass
            # `latest=false` to fall back to forward-paginated ASC mode
            # (cursor semantics via `since`).
            latest = qs.get("latest", ["true"])[0].lower() in (
                "1", "true", "yes"
            )
            try:
                msgs = thread_messages(
                    thread_id, row["name"], since, limit, unread_only, latest
                )
            except PermissionError:
                return self._send_json(403, err("not a member of this thread"))
            return self._send_json(
                200,
                {
                    "thread": thread_id,
                    "messages": msgs,
                    "count": len(msgs),
                    "latest": latest,
                },
            )

        # /v1/threads/<id>/events  (GET)  Server-Sent Events stream of new
        # messages + reaction updates for the thread. Auth via Bearer.
        # Query: ?since=<msg_id_int> to skip already-seen messages on reconnect.
        m_eve = _T_THREAD_EVENTS_RE.match(path)
        if m_eve:
            row = self._auth()
            if not row:
                return self._send_json(401, err("unauthorized"))
            thread_id = m_eve.group(1)
            try:
                since_id = int(qs.get("since", ["0"])[0])
            except ValueError:
                return self._send_json(400, err("bad since"))
            return self._stream_thread_events(row["name"], thread_id, since_id)

        # /v1/threads/<id>  (GET)  show thread
        m = re.match(r"^/v1/threads/([^/]+)$", path)
        if m:
            row = self._auth()
            if not row:
                return self._send_json(401, err("unauthorized"))
            t = thread_get(m.group(1), viewer=row["name"])
            if t is None:
                return self._send_json(404, err("not found or not a member"))
            return self._send_json(200, {"thread": t})

        # --- ack: works for both v0.1 (m_*) and v1 (t_*) ---
        m = _T_MSG_ACK_RE.match(path) or re.match(
            r"^/v1/messages/(m_[0-9a-f]+)/ack$", path
        )
        if m:
            row = self._auth()
            if not row:
                return self._send_json(401, err("unauthorized"))
            msg_id = m.group(1)
            if msg_id.startswith("t_"):
                ok = thread_message_ack(row["name"], msg_id)
            else:
                ok = message_ack(row["name"], msg_id)
            if not ok:
                return self._send_json(404, err("not found or already read"))
            return self._send_json(200, {"ok": True})

        # /v1/messages/<id>  (v0.1 compat)
        m = _T_MSG_REACT_RE.match(path)
        if m:
            row = self._auth()
            if not row:
                return self._send_json(401, err("unauthorized"))
            msg_id = m.group(1)
            try:
                res = reaction_list(msg_id)
            except LookupError:
                return self._send_json(404, err("message not found"))
            return self._send_json(200, res)

        m = _T_MSG_RE.match(path) or re.match(r"^/v1/messages/(m_[0-9a-f]+)$", path)
        if m:
            row = self._auth()
            if not row:
                return self._send_json(401, err("unauthorized"))
            msg_id = m.group(1)
            if msg_id.startswith("t_"):
                d = thread_message_get(msg_id, viewer=row["name"])
            else:
                d = message_get(row["name"], msg_id)
            if not d:
                return self._send_json(404, err("not found"))
            return self._send_json(200, {"message": d})

        # v0.1 inbox
        if path == "/v1/messages":
            row = self._auth()
            if not row:
                return self._send_json(401, err("unauthorized"))
            try:
                since = int(qs.get("since", ["0"])[0])
                limit = min(int(qs.get("limit", ["50"])[0]), 500)
            except ValueError:
                return self._send_json(400, err("bad limit/since"))
            unread = qs.get("unread", ["false"])[0].lower() in ("1", "true", "yes")
            msgs = message_inbox(row["name"], since, limit, unread)
            return self._send_json(200, {"messages": msgs, "count": len(msgs)})

        return self._send_json(404, err("not found"))

    def do_POST(self) -> None:  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/") or "/"

        # --- v0.1.0 auth endpoints (no Bearer required) ---
        if path == "/v1/auth/register":
            return self._handle_auth_register()
        if path == "/v1/auth/login":
            return self._handle_auth_login()
        if path == "/v1/auth/refresh":
            return self._handle_auth_refresh()
        if path == "/v1/auth/logout":
            return self._handle_auth_logout()
        if path == "/v1/auth/whoami":
            row = self._auth()
            if not row:
                return self._send_json(401, err("unauthorized"))
            return self._send_json(200, {
                "user": {"id": row["user_id"], "username": row["name"]},
                "workspace": {"id": row["workspace_id"], "slug": row["workspace_slug"]},
                "scope": row["scope"],
                "expires_at": row["expires_at"],
            })
        if path == "/v1/workspaces":
            row = self._auth()
            if not row:
                return self._send_json(401, err("unauthorized"))
            return self._handle_workspace_create(row)

        row = self._auth()
        if not row:
            return self._send_json(401, err("unauthorized"))

        # ack (POST style)
        m = _T_MSG_ACK_RE.match(path) or re.match(
            r"^/v1/messages/(m_[0-9a-f]+)/ack$", path
        )
        if m:
            msg_id = m.group(1)
            if msg_id.startswith("t_"):
                ok = thread_message_ack(row["name"], msg_id)
            else:
                ok = message_ack(row["name"], msg_id)
            if not ok:
                return self._send_json(404, err("not found or already read"))
            return self._send_json(200, {"ok": True})

        # react (POST = add; DELETE = remove)
        m = _T_MSG_REACT_RE.match(path)
        if m and self.command == "POST":
            msg_id = m.group(1)
            try:
                data = self._read_json()
            except ValueError as e:
                return self._send_json(400, err(str(e)))
            emoji = (data.get("emoji") or "").strip()
            if not emoji:
                return self._send_json(400, err("emoji required"))
            try:
                res = reaction_add(row["name"], msg_id, emoji)
            except LookupError:
                return self._send_json(404, err("message not found"))
            except PermissionError:
                return self._send_json(403, err("not a member of this thread"))
            except ValueError as e:
                return self._send_json(400, err(str(e)))
            return self._send_json(200, res)
        if m and self.command == "DELETE":
            msg_id = m.group(1)
            url2 = urllib.parse.urlparse(self.path)
            qs2 = urllib.parse.parse_qs(url2.query)
            emoji = (qs2.get("emoji", [""])[0] or "").strip()
            if not emoji:
                return self._send_json(400, err("emoji query param required"))
            try:
                res = reaction_remove(row["name"], msg_id, emoji)
            except LookupError:
                return self._send_json(404, err("message not found"))
            return self._send_json(200, res)
        if m and self.command == "GET":
            msg_id = m.group(1)
            try:
                res = reaction_list(msg_id)
            except LookupError:
                return self._send_json(404, err("message not found"))
            return self._send_json(200, res)

        # create thread
        if path == "/v1/threads":
            try:
                data = self._read_json()
            except ValueError as e:
                return self._send_json(400, err(str(e)))
            tid = (data.get("id") or "").strip()
            name = (data.get("name") or "").strip() or None
            members = data.get("members") or []
            if not isinstance(members, list) or not all(
                isinstance(m, str) for m in members
            ):
                return self._send_json(400, err("members must be a list of strings"))
            if not tid:
                return self._send_json(400, err("id is required"))
            try:
                t = thread_create(tid, name, members, created_by=row["name"])
            except ValueError as e:
                return self._send_json(400, err(str(e)))
            log(f"THREAD {row['name']} created '{tid}' members={t['members']}")
            return self._send_json(201, {"thread": t})

        # post to thread
        m = _T_THREAD_MSGS_RE.match(path)
        if m:
            thread_id = m.group(1)
            try:
                data = self._read_json()
            except ValueError as e:
                return self._send_json(400, err(str(e)))
            body = (data.get("body") or "").strip()
            if not body:
                return self._send_json(400, err("body is required"))
            subject = (data.get("subject") or "").strip() or None
            try:
                msg = thread_post(
                    thread_id, row["name"], body, subject, data.get("metadata")
                )
            except PermissionError as e:
                return self._send_json(403, err(str(e)))
            log(f"MSG {row['name']} -> {thread_id} ({msg.get('msg_id')})")
            return self._send_json(201, {"message": msg})

        # add members
        m = re.match(r"^/v1/threads/([^/]+)/members$", path)
        if m:
            thread_id = m.group(1)
            try:
                data = self._read_json()
            except ValueError as e:
                return self._send_json(400, err(str(e)))
            new_members = data.get("members") or []
            if not isinstance(new_members, list) or not all(
                isinstance(x, str) for x in new_members
            ):
                return self._send_json(400, err("members must be a list of strings"))
            try:
                t = thread_add_members(thread_id, new_members, row["name"])
            except PermissionError as e:
                return self._send_json(403, err(str(e)))
            except ValueError as e:
                return self._send_json(400, err(str(e)))
            return self._send_json(200, {"thread": t})

        # v0.1 pairwise send (compat)
        if path == "/v1/messages":
            try:
                data = self._read_json()
            except ValueError as e:
                return self._send_json(400, err(str(e)))
            to_agent = (data.get("to") or "").strip()
            body = (data.get("body") or "").strip()
            if not to_agent or not body:
                return self._send_json(400, err("fields 'to' and 'body' required"))
            subject = (data.get("subject") or "").strip() or None
            msg = message_insert(
                from_agent=row["name"],
                to_agent=to_agent,
                body=body,
                subject=subject,
                metadata=data.get("metadata"),
            )
            log(f"MSG {row['name']} -> {to_agent} ({msg['msg_id']})")
            return self._send_json(201, {"message": msg})

        return self._send_json(404, err("not found"))


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(host: str, port: int) -> None:
    db_init()
    server = ThreadingHTTPServer((host, port), AgentChatHandler)
    log(f"agentchat server listening on http://{host}:{port} (v{SERVER_VERSION})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AgentChatClient:
    def __init__(self, base_url: str, name: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.name = name
        if ":" in token:
            n, s = token.split(":", 1)
            if n == name:
                token = s
        self.token = token

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        timeout: float = 10.0,
    ) -> tuple[int, Any]:
        url = self.base_url + path
        data = None
        headers = {
            "Authorization": f"Bearer {self.name}:{self.token}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"error": raw}
        except urllib.error.URLError as e:
            return 0, {"error": f"connection failed: {e.reason}"}

    # --- low-level auth / system ---
    def whoami(self) -> dict:
        code, body = self._request("GET", "/v1/whoami")
        return {"status": code, "body": body}

    def peers(self) -> dict:
        code, body = self._request("GET", "/v1/peers")
        return {"status": code, "body": body}

    def health(self) -> dict:
        code, body = self._request("GET", "/health")
        return {"status": code, "body": body}

    # --- v1 threads ---
    def thread_create(
        self, thread_id: str, members: list[str], name: Optional[str] = None
    ) -> dict:
        payload: dict[str, Any] = {"id": thread_id, "members": members}
        if name:
            payload["name"] = name
        code, resp = self._request("POST", "/v1/threads", payload)
        return {"status": code, "body": resp}

    def threads(self) -> dict:
        code, resp = self._request("GET", "/v1/threads")
        return {"status": code, "body": resp}

    def thread_show(self, thread_id: str) -> dict:
        code, resp = self._request("GET", f"/v1/threads/{thread_id}")
        return {"status": code, "body": resp}

    def thread_add_members(self, thread_id: str, members: list[str]) -> dict:
        code, resp = self._request(
            "POST", f"/v1/threads/{thread_id}/members", {"members": members}
        )
        return {"status": code, "body": resp}

    def thread_post(
        self,
        thread_id: str,
        body: str,
        subject: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        payload: dict[str, Any] = {"body": body}
        if subject:
            payload["subject"] = subject
        if metadata:
            payload["metadata"] = metadata
        code, resp = self._request(
            "POST", f"/v1/threads/{thread_id}/messages", payload
        )
        return {"status": code, "body": resp}

    def thread_messages(
        self,
        thread_id: str,
        since: int = 0,
        limit: int = 50,
        unread_only: bool = False,
        latest: bool = True,
    ) -> dict:
        params = [f"since={since}", f"limit={limit}"]
        if unread_only:
            params.append("unread=true")
        # `latest` controls ordering. Default true → newest-first DESC.
        # Set to False for forward-paginated ASC mode (cursor semantics).
        params.append(f"latest={'true' if latest else 'false'}")
        code, resp = self._request(
            "GET", f"/v1/threads/{thread_id}/messages?" + "&".join(params)
        )
        return {"status": code, "body": resp}

    def search(
        self,
        query: str,
        thread_id: Optional[str] = None,
        from_agent: Optional[str] = None,
        limit: int = 50,
    ) -> dict:
        # Cross-thread substring search. Always newest-first DESC.
        from urllib.parse import urlencode

        params: list[tuple[str, str]] = [("q", query), ("limit", str(limit))]
        if thread_id:
            params.append(("thread", thread_id))
        if from_agent:
            params.append(("from", from_agent))
        code, resp = self._request(
            "GET", "/v1/search?" + urlencode(params)
        )
        return {"status": code, "body": resp}

    def react(
        self,
        msg_id: str,
        emoji: str,
        op: str = "add",
    ) -> dict:
        """Add, remove, or list reactions on a message.

        op='add'    → POST   /v1/messages/<id>/reactions  body={"emoji": "👍"}
        op='remove' → DELETE /v1/messages/<id>/reactions?emoji=👍
        op='list'   → GET    /v1/messages/<id>/reactions
        """
        if op == "add":
            code, resp = self._request(
                "POST",
                f"/v1/messages/{msg_id}/reactions",
                {"emoji": emoji},
            )
        elif op == "remove":
            from urllib.parse import urlencode
            code, resp = self._request(
                "DELETE",
                f"/v1/messages/{msg_id}/reactions?{urlencode({'emoji': emoji})}",
            )
        elif op == "list":
            code, resp = self._request(
                "GET", f"/v1/messages/{msg_id}/reactions"
            )
        else:
            raise ValueError(f"op must be add/remove/list, got {op!r}")
        return {"status": code, "body": resp}

    # --- v1 inbox (cross-thread) ---
    def inbox(
        self, limit: int = 50, unread_only: bool = False, since: int = 0
    ) -> dict:
        # The new inbox uses limit only (server returns id-ascending for this caller).
        code, resp = self._request(
            "GET",
            f"/v1/inbox?limit={limit}" + ("&unread=true" if unread_only else ""),
        )
        return {"status": code, "body": resp}

    # --- ack: works for v0.1 (m_*) and v1 (t_*) ---
    def ack(self, msg_id: str) -> dict:
        code, resp = self._request("POST", f"/v1/messages/{msg_id}/ack")
        return {"status": code, "body": resp}

    def read(self, msg_id: str) -> dict:
        code, resp = self._request("GET", f"/v1/messages/{msg_id}")
        return {"status": code, "body": resp}

    # --- v0.1 compat ---
    def send(
        self, to: str, body: str, subject: Optional[str] = None
    ) -> dict:
        payload: dict[str, Any] = {"to": to, "body": body}
        if subject:
            payload["subject"] = subject
        code, resp = self._request("POST", "/v1/messages", payload)
        return {"status": code, "body": resp}

    def v1_inbox(
        self, since: int = 0, limit: int = 50, unread_only: bool = False
    ) -> dict:
        """v0.1-style pairwise inbox (legacy)."""
        params = [f"since={since}", f"limit={limit}"]
        if unread_only:
            params.append("unread=true")
        code, resp = self._request("GET", "/v1/messages?" + "&".join(params))
        return {"status": code, "body": resp}


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    db_init()
    if not TOKENS_PATH.exists():
        tokens_save({})

    toks = tokens_load()
    created: list[tuple[str, str, str]] = []

    for spec in args.agent:
        if ":" not in spec:
            print(
                f"  skip {spec}: must be NAME:ROLE (role=agent|admin|observer)",
                file=sys.stderr,
            )
            continue
        name, role = spec.split(":", 1)
        if role not in ("agent", "admin", "observer"):
            print(
                f"  skip {spec}: role must be agent|admin|observer",
                file=sys.stderr,
            )
            continue
        if name in toks and not args.force:
            print(f"  exists {name} (use --force to rotate)")
            continue
        token = token_new()
        agent_register(name, token, role=role, endpoint=args.endpoint)
        toks = tokens_load()
        created.append((name, role, token))

    print()
    print("=" * 70)
    print(f"AGENTCHAT INITIALIZED  (v{SERVER_VERSION})")
    print("=" * 70)
    print(f"home:    {AGENTCHAT_HOME}")
    print(f"db:      {DB_PATH}")
    print(f"tokens:  {TOKENS_PATH} (mode 600)")
    print()
    if created:
        print("NEW / ROTATED TOKENS — distribute securely (NOT in markdown docs):")
        print()
        for name, role, token in created:
            print(f"  {name}  (role={role})")
            print(f"    token: {name}:{token}")
        print()
    elif not toks:
        print("No agents yet. Re-run with: agentchat init <name>:<role> ...")
        return 1
    else:
        print("Existing agents (no new tokens created):")
        for name, info in toks.items():
            print(f"  {name}  (role={info.get('role','?')})")
        print()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    serve(args.host, args.port)
    return 0


def _client_from_args(args: argparse.Namespace) -> AgentChatClient:
    cfg = _load_client_cfg(args)
    if not cfg.get("base_url") or not cfg.get("name") or not cfg.get("token"):
        print(
            f"error: not configured. Run `agentchat set-identity` first "
            f"or pass --url/--name/--token.\n  config: {CONFIG_PATH}",
            file=sys.stderr,
        )
        sys.exit(2)
    return AgentChatClient(
        cfg["base_url"], cfg["name"], cfg["token"]
    )


def _load_client_cfg(args: argparse.Namespace) -> dict:
    cfg: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            pass
    if getattr(args, "url", None):
        cfg["base_url"] = args.url
    if getattr(args, "name", None):
        cfg["name"] = args.name
    if getattr(args, "token", None):
        cfg["token"] = args.token
    return cfg


def _save_client_cfg(cfg: dict) -> None:
    ensure_home()
    with CONFIG_PATH.open("w") as f:
        json.dump(cfg, f, indent=2)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def cmd_setidentity(args: argparse.Namespace) -> int:
    cfg = _load_client_cfg(args)
    if args.base_url:
        cfg["base_url"] = args.base_url.rstrip("/")
    if args.name:
        cfg["name"] = args.name
    if args.token:
        cfg["token"] = args.token
    _save_client_cfg(cfg)
    print(f"saved identity to {CONFIG_PATH}")
    print(f"  base_url: {cfg.get('base_url','(unset)')}")
    print(f"  name:     {cfg.get('name','(unset)')}")
    print(f"  token:    {'(set)' if cfg.get('token') else '(unset)'}")
    return 0


# --- v0.1 pairwise send (compat) ---
def cmd_send(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    if args.thread:
        r = client.thread_post(args.thread, args.body, args.subject)
        if r["status"] in (200, 201):
            m = r["body"].get("message", r["body"])
            print(f"sent -> thread:{args.thread}  id={m.get('msg_id')}")
            return 0
        print(f"FAIL ({r['status']}): {r['body']}", file=sys.stderr)
        return 1
    if not args.to:
        print("either --thread or a positional <to> is required", file=sys.stderr)
        return 2
    r = client.send(args.to, args.body, args.subject)
    if r["status"] in (200, 201):
        msg = r["body"].get("message", r["body"])
        print(f"sent -> {args.to}  id={msg.get('msg_id')}")
        return 0
    print(f"FAIL ({r['status']}): {r['body']}", file=sys.stderr)
    return 1


def cmd_inbox(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    r = client.inbox(args.limit, args.unread)
    if r["status"] != 200:
        print(f"FAIL ({r['status']}): {r['body']}", file=sys.stderr)
        return 1
    msgs = r["body"].get("messages", [])
    if not msgs:
        print("(no messages)")
        return 0
    for m in msgs:
        flag = " " if m.get("read_at") else "*"
        tname = m.get("t_name") or m.get("t_id") or "?"
        preview = (m.get("body") or "").splitlines()[0][:80]
        print(
            f"{flag} {m['id']:>4}  {m['created_at']}  "
            f"{m['from_agent']:>10} -> thread:{tname}"
        )
        print(f"        {preview}")
    print()
    print(f"{len(msgs)} message(s)  (* = unread)")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    r = client.read(args.msg_id)
    if r["status"] != 200:
        print(f"FAIL ({r['status']}): {r['body']}", file=sys.stderr)
        return 1
    m = r["body"]["message"]
    print(f"id:        {m['msg_id']}")
    print(f"internal:  {m['id']}")
    print(f"thread:    {m.get('thread_id','(pairwise)')}")
    print(f"from:      {m['from_agent']}")
    print(f"to:        {m.get('to_agent','(thread)')}")
    print(f"subject:   {m.get('subject') or '(none)'}")
    print(f"created:   {m['created_at']}")
    print(f"delivered: {m.get('delivered_at') or '(in inbox)'}")
    print(f"read_at:   {m.get('read_at') or '(unread)'}")
    print("-" * 60)
    print(m["body"])
    if not m.get("read_at"):
        ack = client.ack(m["msg_id"])
        if ack["status"] == 200:
            print("-" * 60)
            print("(marked as read)")
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    r = client.ack(args.msg_id)
    print(json.dumps(r, indent=2))
    return 0 if r["status"] == 200 else 1


def cmd_peers(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    r = client.peers()
    if r["status"] != 200:
        print(f"FAIL ({r['status']}): {r['body']}", file=sys.stderr)
        return 1
    for p in r["body"].get("peers", []):
        print(
            f"  {p['name']:<15} role={p['role']:<8} "
            f"endpoint={p.get('endpoint') or '-':<30} "
            f"last_seen={p.get('last_seen') or '-'}"
        )
    return 0


# --- v1 threads ---
def cmd_threads(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    r = client.threads()
    if r["status"] != 200:
        print(f"FAIL ({r['status']}): {r['body']}", file=sys.stderr)
        return 1
    threads = r["body"].get("threads", [])
    if not threads:
        print("(no threads)")
        return 0
    for t in threads:
        last = t.get("last_message") or {}
        preview = ""
        if last:
            preview = (last.get("body") or "").splitlines()[0][:60]
        unread = t.get("unread", 0)
        flag = f" *{unread} unread" if unread else ""
        print(
            f"  {t['id']:<40}  members={t['member_count']}{flag}  "
            f"last: {last.get('from_agent','-')}: {preview}"
        )
    return 0


def cmd_thread_show(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    r = client.thread_show(args.thread)
    if r["status"] != 200:
        print(f"FAIL ({r['status']}): {r['body']}", file=sys.stderr)
        return 1
    t = r["body"]["thread"]
    print(f"id:         {t['id']}")
    print(f"name:       {t.get('name') or '(none)'}")
    print(f"created_by: {t['created_by']}")
    print(f"created_at: {t['created_at']}")
    print(f"members:    {', '.join(t['members'])}")
    print(f"unread:     {t['unread']}")
    last = t.get("last_message")
    if last:
        print(f"last msg:   {last['from_agent']} @ {last['created_at']}")
        print(f"            {last['body']}")
    return 0


def cmd_thread_messages(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    # --oldest flag flips the ordering back to forward-paginated ASC.
    # Default is now "latest N" (DESC) — the natural chat-UI semantic.
    r = client.thread_messages(
        args.thread, args.since, args.limit, args.unread,
        latest=not args.oldest,
    )
    if r["status"] != 200:
        print(f"FAIL ({r['status']}): {r['body']}", file=sys.stderr)
        return 1
    msgs = r["body"].get("messages", [])
    if not msgs:
        print("(no messages)")
        return 0
    for m in msgs:
        if m.get("own"):
            flag = ">"
        elif m.get("read_at"):
            flag = " "
        else:
            flag = "*"
        subj = f" — {m['subject']}" if m.get("subject") else ""
        preview = (m.get("body") or "").splitlines()[0][:80]
        print(
            f"{flag} {m['id']:>4}  {m['created_at']}  "
            f"{m['from_agent']:>10}{subj}"
        )
        print(f"        {preview}")
    print()
    print(f"{len(msgs)} message(s)  (* unread, > own)")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Cross-thread substring search."""
    client = _client_from_args(args)
    r = client.search(
        args.query,
        thread_id=args.thread,
        from_agent=args.from_agent,
        limit=args.limit,
    )
    if r["status"] != 200:
        print(f"FAIL ({r['status']}): {r['body']}", file=sys.stderr)
        return 1
    body = r["body"]
    hits = body.get("hits", [])
    if not hits:
        print(f"(no hits for: {body.get('query', args.query)!r})")
        return 0
    for h in hits:
        own = ">" if h.get("own") else " "
        subj = f" — {h['subject']}" if h.get("subject") else ""
        preview = (h.get("body_preview") or "").splitlines()[0][:80]
        print(
            f"{own} {h['id']:>4}  {h['created_at']}  "
            f"{h['from_agent']:>10}  [{h['thread_id']:<22}]"
            f"{subj}"
        )
        print(f"        {preview}")
    print()
    print(f"{len(hits)} hit(s) for: {body.get('query', args.query)!r}")
    return 0


def cmd_react(args: argparse.Namespace) -> int:
    """Add, remove, or list emoji reactions on a message."""
    client = _client_from_args(args)
    if args.remove:
        op = "remove"
    elif args.list_reactions:
        op = "list"
    else:
        op = "add"
    if op != "list" and not args.emoji:
        print("FAIL: emoji argument required for add/remove", file=sys.stderr)
        return 2
    r = client.react(args.msg_id, args.emoji, op=op)
    if r["status"] != 200:
        print(f"FAIL ({r['status']}): {r['body']}", file=sys.stderr)
        return 1
    body = r["body"]
    if op == "list":
        rx = body.get("reactions", {})
        if not rx:
            print(f"(no reactions on {args.msg_id})")
            return 0
        for emoji, agents in rx.items():
            print(f"  {emoji}  {', '.join(agents)}")
        return 0
    # add or remove — show concise confirmation
    verdict = body.get("added") if op == "add" else body.get("removed")
    print(f"{op} {args.emoji} on {args.msg_id}: {'ok' if verdict else 'noop'}")
    rx = body.get("reactions", {})
    if rx:
        for emoji, agents in rx.items():
            print(f"  {emoji}  {', '.join(agents)}")
    return 0


def cmd_thread_create(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    members = [m.strip() for m in args.members.split(",") if m.strip()]
    if not members:
        print("at least one --members is required", file=sys.stderr)
        return 2
    r = client.thread_create(args.thread, members, args.thread_display_name)
    if r["status"] in (200, 201):
        t = r["body"].get("thread", r["body"])
        print(f"created thread: {t['id']}")
        print(f"  members: {', '.join(t['members'])}")
        return 0
    print(f"FAIL ({r['status']}): {r['body']}", file=sys.stderr)
    return 1


# --- watch (live tail) ---
def _print_message_short(m: dict) -> None:
    flag = " " if m.get("read_at") else "*"
    if m.get("own"):
        flag = ">"
    subj = f" — {m['subject']}" if m.get("subject") else ""
    body = m.get("body") or ""
    ts = m.get("created_at") or "?"
    print(
        f"{flag} {ts}  {m.get('from_agent','?'):>10} -> "
        f"thread:{m.get('t_id') or m.get('thread_id','?')}{subj}"
    )
    for line in body.splitlines() or [""]:
        print(f"        {line}")


def cmd_watch(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    interval = max(0.2, args.interval)

    # Track last seen id per thread (and v0.1 inbox)
    last_seen: dict[str, int] = {}

    def fetch_once() -> list[dict]:
        out: list[dict] = []
        if args.thread:
            threads_to_poll = [args.thread]
        else:
            r = client.threads()
            if r["status"] != 200:
                return []
            threads_to_poll = [t["id"] for t in r["body"].get("threads", [])]
        for tid in threads_to_poll:
            since = last_seen.get(f"t:{tid}", 0)
            r = client.thread_messages(tid, since=since, limit=200, unread_only=False)
            if r["status"] == 200:
                for m in r["body"].get("messages", []):
                    out.append({**m, "t_id": tid})
                    last_seen[f"t:{tid}"] = max(
                        last_seen.get(f"t:{tid}", 0), m["id"]
                    )
        # sort chronologically
        out.sort(key=lambda m: m.get("id", 0))
        return out

    if not args.quiet:
        if args.thread:
            print(f"[watch] tailing thread:{args.thread}  (Ctrl-C to stop)")
        else:
            print(f"[watch] tailing all my threads  (Ctrl-C to stop)")

    last_id_global = 0
    try:
        while True:
            msgs = fetch_once()
            for m in msgs:
                if m.get("id", 0) > last_id_global:
                    _print_message_short(m)
                    sys.stdout.flush()
                    last_id_global = max(last_id_global, m.get("id", 0))
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[watch] stopped")
    return 0


# ---------------------------------------------------------------------------
# Respond daemon: watches a thread, calls LLM, posts back when triggered
# ---------------------------------------------------------------------------

# Triggers that address a single named agent (case-insensitive word match).
SINGLE_TRIGGERS = {
    "hermes": [r"\bhermes\b"],
    "chappy": [r"\bchappy\b"],
    "waynec": [r"\bwaynec\b", r"\bwayne\b"],
}

# Triggers that address multiple/all agents.
PLURAL_TRIGGERS = [
    r"\bguys\b", r"\beveryone\b", r"\beverybody\b", r"\ball\s+of\s+you\b",
    r"\bteam\b", r"\bfolks\b", r"\bboth\s+of\s+you\b", r"\bboth\b",
    r"@all\b", r"@everyone\b", r"@guys\b",
]


def _load_llm_config() -> dict:
    """Read /home/waynec/.hermes/config.yaml to get LLM endpoint + key."""
    import yaml  # only used here, kept optional
    paths = [
        Path("/home/waynec/.hermes/config.yaml"),
        AGENTCHAT_HOME.parent / "config.yaml",
        Path(os.environ.get("HERMES_CONFIG", "")),
    ]
    for p in paths:
        if not p or not p.exists():
            continue
        try:
            d = yaml.safe_load(p.read_text())
        except Exception:
            continue
        m = d.get("model", {})
        return {
            "model": m.get("default", "MiniMax-M3"),
            "base_url": (m.get("base_url") or "").rstrip("/"),
            "api_key": m.get("api_key") or "",
        }
    return {"model": "MiniMax-M3", "base_url": "", "api_key": ""}


def _llm_chat(messages: list[dict], llm: dict, timeout: float = 30.0) -> Optional[str]:
    """Call the chat-completions endpoint and return the assistant text.

    Strips <think>...</think> blocks (some reasoning models emit them inline)
    before returning, so the visible reply never includes chain-of-thought.
    """
    if not llm.get("base_url") or not llm.get("api_key"):
        return None
    url = llm["base_url"] + "/chat/completions"
    payload = {
        "model": llm["model"],
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1500,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {llm['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        text = (
            body.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        # Strip <think>...</think> blocks (DeepSeek-style inline reasoning).
        text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()
        return text
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as e:
        log(f"LLM call failed: {e}")
        return None


def _should_respond(text: str, my_name: str) -> bool:
    """Decide whether the given message body addresses me (or the group)."""
    t = text.lower()
    # Plural / group triggers always address me.
    for pat in PLURAL_TRIGGERS:
        if re.search(pat, t):
            return True
    # Direct @-mention of my name.
    if f"@{my_name.lower()}" in t:
        return True
    # My name as a word.
    for pat in SINGLE_TRIGGERS.get(my_name, []):
        if re.search(pat, t):
            return True
    return False


# Cache: agent_name -> bool (is observer?). Lookups are cheap but the daemon
# polls hot, so cache to avoid hitting SQLite on every message.
_OBSERVER_CACHE: dict[str, bool] = {}


def _is_observer(agent_name: str) -> bool:
    """Return True if the agent has role='observer' (read-only member)."""
    if agent_name in _OBSERVER_CACHE:
        return _OBSERVER_CACHE[agent_name]
    db_init()
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT role FROM agents WHERE name = ?", (agent_name,)
        ).fetchone()
        result = bool(row and row["role"] == "observer")
    finally:
        conn.close()
    _OBSERVER_CACHE[agent_name] = result
    return result


def _invalidate_observer_cache(agent_name: Optional[str] = None) -> None:
    """Drop a cached role (or all) — call after agent_register or role changes."""
    if agent_name is None:
        _OBSERVER_CACHE.clear()
    else:
        _OBSERVER_CACHE.pop(agent_name, None)


def _build_prompt(
    thread: dict,
    my_name: str,
    members: list[str],
    recent: list[dict],
    latest: dict,
    other_recent: list[dict],
) -> list[dict]:
    """Build the chat-completions messages array for the LLM."""
    others = [m for m in members if m != my_name]
    sys = (
        f"You are {my_name}, an AI agent in a multi-agent chat thread.\n"
        f"Thread: {thread.get('name') or thread.get('id')}\n"
        f"Other participants: {', '.join(others) or '(none)'}\n"
        f"Your role: be a useful collaborator. Be concise (1-3 sentences typically). "
        f"Use markdown if it helps (bold, italic, code, links).\n"
        f"\n"
        f"You will be shown the recent thread. The latest message is from "
        f"{latest.get('from_agent', '?')} and was addressed to you (or the group). "
        f"Decide whether to respond substantively, add a small clarification, or stay quiet.\n"
        f"\n"
        f"Coordination: if another agent has already responded to the same trigger, "
        f"do NOT duplicate their answer. Add to it, correct it, or stay silent. "
        f"Only post a response that adds value."
    )
    user_lines = ["Recent thread (oldest to newest):\n"]
    for m in recent:
        own = " (you)" if m.get("own") else ""
        body = m.get("body", "").replace("\n", "\n  ")
        user_lines.append(f"  [{m.get('created_at','')}] {m.get('from_agent','?')}{own}: {body}")
    user_lines.append("")
    if other_recent:
        user_lines.append(
            f"Note: {other_recent[0]['from_agent']} has already posted in response. "
            f"Read it above and adjust your answer accordingly — don't repeat them."
        )
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": "\n".join(user_lines)},
    ]


def cmd_respond(args: argparse.Namespace) -> int:
    """Watch a thread, post LLM-generated replies when addressed."""
    client = _client_from_args(args)
    me = client.name
    thread_id = args.thread
    interval = max(0.5, args.interval)
    context_n = max(2, args.context)
    debounce = max(0.0, args.debounce)
    dry = bool(args.dry_run)
    quiet = bool(args.quiet)

    llm = _load_llm_config()
    # CLI overrides
    if getattr(args, "api_base", None):
        llm["base_url"] = args.api_base.rstrip("/")
    if getattr(args, "api_key", None):
        llm["api_key"] = args.api_key
    if getattr(args, "api_model", None):
        llm["model"] = args.api_model

    # Determine starting last_id. Options:
    #   --start-from-id N    explicit id
    #   --start-from-now     use current max id (only NEW messages)
    last_id = 0
    if getattr(args, "start_from_id", None) is not None:
        last_id = int(args.start_from_id)
    elif getattr(args, "start_from_now", False):
        r0 = client.thread_messages(thread_id, since=0, limit=500)
        msgs0 = r0["body"].get("messages", []) if r0["status"] == 200 else []
        if msgs0:
            last_id = max(m["id"] for m in msgs0)
        if not quiet:
            print(f"[respond] start-from-now: last_id={last_id}")

    if not quiet:
        print(f"[respond] agent={me}  thread={thread_id}  interval={interval}s  "
              f"context={context_n}  debounce={debounce}s  dry_run={dry}  last_id={last_id}")
        print(f"[respond] LLM: model={llm.get('model')}  base={llm.get('base_url')}")
        print(f"[respond] Ctrl-C to stop")
    try:
        while True:
            r = client.thread_messages(thread_id, since=last_id, limit=200)
            if r["status"] != 200:
                time.sleep(interval)
                continue
            msgs = r["body"].get("messages", [])
            if not msgs:
                time.sleep(interval)
                continue

            # Process each new message in order.
            for m in msgs:
                if m["id"] <= last_id:
                    continue
                last_id = max(last_id, m["id"])
                by = m.get("from_agent")
                body = m.get("body", "")
                if by == me:
                    continue  # don't respond to my own messages
                if _is_observer(by):
                    # Observers are read-only members (typically the human
                    # overseer). Their messages are visible but never trigger
                    # any agent — they're audit, not conversation.
                    continue
                if not _should_respond(body, me):
                    continue

                # Debounce: give the other agent a head start, then refetch
                # so we have their response in our context.
                if debounce > 0:
                    time.sleep(debounce)
                    rc = client.thread_messages(thread_id, since=0, limit=context_n + 5)
                    if rc["status"] == 200:
                        recent = rc["body"].get("messages", [])[-context_n:]
                    else:
                        recent = [m] + ([msgs[-1]] if msgs else [])
                else:
                    rc = client.thread_messages(thread_id, since=0, limit=context_n + 5)
                    if rc["status"] == 200:
                        recent = rc["body"].get("messages", [])[-context_n:]
                    else:
                        recent = [m]

                # Find the thread record for the system prompt.
                rt = client.thread_show(thread_id)
                if rt["status"] != 200:
                    continue
                thread = rt["body"]["thread"]
                members = thread.get("members") or []

                # Identify whether another agent has already posted
                # in response to the same trigger (post our last_id check).
                other_recent = [
                    x for x in recent
                    if x.get("from_agent") != me
                    and x.get("from_agent") != by
                    and x.get("id") > m["id"] - 1  # newer than the trigger
                ]

                prompt = _build_prompt(thread, me, members, recent, m, other_recent)
                if not quiet:
                    print(f"[respond] trigger from {by} at {m['created_at']}: "
                          f"{body[:60]!r}")
                reply = _llm_chat(prompt, llm)
                if not reply:
                    if not quiet:
                        print(f"[respond] LLM returned nothing; skipping")
                    continue

                if dry:
                    print(f"[dry-run] would post: {reply[:200]}")
                else:
                    pr = client.thread_post(thread_id, reply)
                    if pr["status"] in (200, 201):
                        mid = pr["body"].get("message", {}).get("msg_id", "?")
                        if not quiet:
                            print(f"[respond] posted {mid}  ({len(reply)} chars)")
                    else:
                        if not quiet:
                            print(f"[respond] post failed: {pr['status']} {pr['body']}")

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[respond] stopped")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load_client_cfg(args)
    if not cfg.get("base_url"):
        print(f"not configured. config: {CONFIG_PATH}")
        return 2
    client = AgentChatClient(
        cfg.get("base_url", "http://localhost:7878"),
        cfg.get("name", "anon"),
        cfg.get("token", ""),
    )
    h = client.health()
    print(f"endpoint: {cfg.get('base_url')}")
    print(f"identity: {cfg.get('name')}")
    print(f"health:   {h['status']}  {h['body']}")
    if cfg.get("token"):
        w = client.whoami()
        print(f"auth:     {w['status']}  {w['body']}")
        t = client.threads()
        if t["status"] == 200:
            print(f"threads:  {t['body'].get('count', 0)} (member of)")
    return 0 if h["status"] == 200 else 1


def cmd_token(args: argparse.Namespace) -> int:
    toks = tokens_load()
    if args.action == "show":
        if args.name:
            if args.name not in toks:
                print(f"no token for {args.name}", file=sys.stderr)
                return 1
            info = toks[args.name]
            print(f"{args.name}  (role={info.get('role')})")
            print(f"  token: {args.name}:{info['token']}")
        else:
            for name, info in toks.items():
                print(
                    f"  {name:<20} role={info.get('role','?'):<8} "
                    f"endpoint={info.get('endpoint') or '-'}"
                )
        return 0
    if args.action == "rotate":
        if not args.name:
            print("--name required for rotate", file=sys.stderr)
            return 2
        if args.name not in toks:
            print(f"no such agent: {args.name}", file=sys.stderr)
            return 1
        new_t = token_new()
        info = toks[args.name]
        agent_register(
            args.name,
            new_t,
            role=info.get("role", "agent"),
            endpoint=info.get("endpoint"),
        )
        print(f"rotated token for {args.name}:")
        print(f"  {args.name}:{new_t}")
        return 0
    if args.action == "add":
        if not args.name:
            print("--name required for add", file=sys.stderr)
            return 2
        new_t = token_new()
        agent_register(
            args.name, new_t, role=args.role, endpoint=args.endpoint
        )
        print(f"added {args.name} (role={args.role}):")
        print(f"  {args.name}:{new_t}")
        return 0
    if args.action == "rm":
        if not args.name:
            print("--name required for rm", file=sys.stderr)
            return 2
        if args.name not in toks:
            print(f"no such agent: {args.name}", file=sys.stderr)
            return 1
        db_init()
        conn = db_connect()
        try:
            conn.execute("DELETE FROM agents WHERE name = ?", (args.name,))
        finally:
            conn.close()
        del toks[args.name]
        tokens_save(toks)
        print(f"removed {args.name}")
        return 0
    return 2


# ---------------------------------------------------------------------------
# Admin: audit, export, retention, backup
# ---------------------------------------------------------------------------


def cmd_audit(args: argparse.Namespace) -> int:
    """List all threads with member roles, message counts, last activity.

    Admin-only by convention: works against the local DB. No HTTP roundtrip.
    """
    db_init()
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT t.id, t.name, t.created_by, t.created_at, "
            "  (SELECT COUNT(*) FROM thread_messages tm WHERE tm.thread_id = t.id) AS msg_count, "
            "  (SELECT MAX(created_at) FROM thread_messages tm WHERE tm.thread_id = t.id) AS last_msg "
            "FROM threads t ORDER BY t.created_at"
        ).fetchall()
        members_by_thread: dict[str, list[dict]] = {}
        for m in conn.execute(
            "SELECT tm.thread_id, a.name, a.role, tm.joined_at "
            "FROM thread_members tm JOIN agents a ON a.name = tm.agent_name "
            "ORDER BY tm.thread_id, tm.joined_at"
        ).fetchall():
            members_by_thread.setdefault(m["thread_id"], []).append(dict(m))
        agents_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        oldest = conn.execute(
            "SELECT MIN(created_at) FROM thread_messages"
        ).fetchone()[0]
    finally:
        conn.close()
    print(f"AGENTCHAT AUDIT  (db: {DB_PATH})")
    print(f"  agents:   {agents_count}")
    print(f"  oldest:   {oldest or '(no messages)'}")
    print()
    if args.format == "json":
        out = {
            "db": str(DB_PATH),
            "agents": agents_count,
            "oldest_message": oldest,
            "threads": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "created_by": r["created_by"],
                    "created_at": r["created_at"],
                    "message_count": r["msg_count"],
                    "last_message": r["last_msg"],
                    "members": members_by_thread.get(r["id"], []),
                }
                for r in rows
            ],
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    # human-readable
    for r in rows:
        members = members_by_thread.get(r["id"], [])
        members_str = ", ".join(
            f"{m['name']}({m['role']})" for m in members
        )
        print(f"  {r['id']}")
        print(f"    name:        {r['name'] or '(none)'}")
        print(f"    created_by:  {r['created_by']}  @ {r['created_at']}")
        print(f"    members:     {members_str}")
        print(f"    messages:    {r['msg_count']}")
        print(f"    last msg:    {r['last_msg'] or '(none)'}")
        print()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Export a thread's messages to JSON, JSONL, or Markdown.

    Local DB read; no HTTP roundtrip. Output goes to stdout (pipe to file).
    """
    db_init()
    conn = db_connect()
    try:
        trow = conn.execute(
            "SELECT id, name, created_by, created_at FROM threads WHERE id = ?",
            (args.thread,),
        ).fetchone()
        if trow is None:
            print(f"no such thread: {args.thread}", file=sys.stderr)
            return 1
        members = [
            dict(r) for r in conn.execute(
                "SELECT agent_name, joined_at FROM thread_members "
                "WHERE thread_id = ? ORDER BY joined_at", (args.thread,)
            )
        ]
        msgs = [
            dict(r) for r in conn.execute(
                "SELECT * FROM thread_messages WHERE thread_id = ? "
                "ORDER BY id ASC", (args.thread,)
            )
        ]
    finally:
        conn.close()

    fmt = args.format
    if fmt == "json":
        out = {
            "thread": dict(trow),
            "members": members,
            "exported_at": now_iso(),
            "message_count": len(msgs),
            "messages": [
                {
                    **m,
                    "metadata": json.loads(m["metadata"]) if m.get("metadata") else None,
                }
                for m in msgs
            ],
        }
        json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    elif fmt == "jsonl":
        # First line: thread metadata. Following: one message per line.
        sys.stdout.write(json.dumps({
            "_type": "thread", "thread": dict(trow), "members": members,
            "exported_at": now_iso(),
        }, ensure_ascii=False) + "\n")
        for m in msgs:
            md = json.loads(m["metadata"]) if m.get("metadata") else None
            sys.stdout.write(json.dumps({
                "_type": "message", **m, "metadata": md,
            }, ensure_ascii=False) + "\n")
    elif fmt == "md":
        name = trow["name"] or trow["id"]
        print(f"# {name}")
        print()
        print(f"- **Thread id:** `{trow['id']}`")
        print(f"- **Created by:** {trow['created_by']} on {trow['created_at']}")
        print(f"- **Members:** {', '.join(m['agent_name'] for m in members)}")
        print(f"- **Exported:** {now_iso()}")
        print(f"- **Messages:** {len(msgs)}")
        print()
        print("---")
        print()
        for m in msgs:
            print(f"### {m['from_agent']} — {m['created_at']}")
            if m.get("subject"):
                print(f"*Subject: {m['subject']}*")
            print()
            print(m["body"])
            print()
    else:
        print(f"unknown format: {fmt}", file=sys.stderr)
        return 2
    return 0


def cmd_retention(args: argparse.Namespace) -> int:
    """Move thread_messages older than --hot-days to a cold-archive DB file.

    The hot DB stays small; the archive lives next to it as
    ``messages.cold-YYYYMMDD.db`` and is never queried by the live system
    (only by `export` against a copied DB, or by external audit tools).
    """
    db_init()
    hot_days = args.hot_days
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=hot_days)
    ).isoformat(timespec="seconds")
    archive_dir = AGENTCHAT_HOME / "archive"
    archive_dir.mkdir(exist_ok=True)
    try:
        os.chmod(archive_dir, 0o700)
    except OSError:
        pass
    archive_path = archive_dir / f"messages.cold-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.db"

    # Step 1: count + grab schema (short-lived read connection).
    src = sqlite3.connect(DB_PATH)
    try:
        rows_to_move = src.execute(
            "SELECT COUNT(*) FROM thread_messages WHERE created_at < ?",
            (cutoff,),
        ).fetchone()[0]
        create_sql = src.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='thread_messages'"
        ).fetchone()
    finally:
        src.close()
    if not rows_to_move:
        print(f"no messages older than {cutoff} — nothing to archive")
        return 0
    if not (create_sql and create_sql[0]):
        print("error: could not find thread_messages schema", file=sys.stderr)
        return 1
    # Step 2: write rows to archive (separate connection, no shared locks).
    dst = sqlite3.connect(archive_path)
    try:
        dst.executescript(create_sql[0])
        placeholders = ",".join("?" * 8)  # 8 columns in thread_messages
        # Stream rows in chunks to keep memory bounded.
        src = sqlite3.connect(DB_PATH)
        try:
            src.row_factory = sqlite3.Row
            chunk: list[tuple] = []
            CHUNK = 500
            cur = src.execute(
                "SELECT id, msg_id, thread_id, from_agent, subject, body, "
                "created_at, metadata FROM thread_messages "
                "WHERE created_at < ?",
                (cutoff,),
            )
            for row in cur:
                chunk.append(tuple(row))
                if len(chunk) >= CHUNK:
                    dst.executemany(
                        f"INSERT INTO thread_messages VALUES ({placeholders})",
                        chunk,
                    )
                    chunk = []
            if chunk:
                dst.executemany(
                    f"INSERT INTO thread_messages VALUES ({placeholders})",
                    chunk,
                )
            dst.commit()
        finally:
            src.close()
    finally:
        dst.close()
    # Step 3: delete from live DB (separate connection).
    src = sqlite3.connect(DB_PATH)
    try:
        cur = src.execute(
            "DELETE FROM thread_messages WHERE created_at < ?", (cutoff,)
        )
        deleted = cur.rowcount
        src.commit()
    finally:
        src.close()
    # Note: message_recipients for archived messages are intentionally
    # left in place. They reference msg_ids that no longer exist in
    # thread_messages, but DELETE...WHERE NOT EXISTS-style cleanup
    # is a separate concern (orphan rows are harmless for read paths).
    try:
        os.chmod(archive_path, 0o600)
    except OSError:
        pass
    size = archive_path.stat().st_size
    print(f"archived {rows_to_move} messages older than {cutoff}")
    print(f"  archive: {archive_path}  ({size} bytes)")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Take a hot sqlite3 backup of the live DB to a sibling file.

    Uses the online .backup API so the live DB stays consistent and unlocked.
    Rotates: keeps the most recent ``--keep`` backup files in
    ``AGENTCHAT_HOME/backups/``.
    """
    db_init()
    backup_dir = AGENTCHAT_HOME / "backups"
    backup_dir.mkdir(exist_ok=True)
    try:
        os.chmod(backup_dir, 0o700)
    except OSError:
        pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = backup_dir / f"messages.{stamp}.db"
    src = sqlite3.connect(DB_PATH)
    try:
        # sqlite3 .backup API is on the Connection in stdlib 3.7+
        with sqlite3.connect(out) as dst:
            src.backup(dst)
    finally:
        src.close()
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    # Rotation.
    keep = max(1, args.keep)
    backups = sorted(backup_dir.glob("messages.*.db"), key=lambda p: p.name)
    for old in backups[:-keep]:
        try:
            old.unlink()
        except OSError as e:
            print(f"warn: could not delete {old}: {e}", file=sys.stderr)
    size = out.stat().st_size
    print(f"backup ok: {out}  ({size} bytes)")
    remaining = sorted(backup_dir.glob("messages.*.db"))
    print(f"  retained: {len(remaining)} of {keep} (newest: {remaining[-1].name if remaining else '-'})")
    return 0


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentchat",
        description=f"Direct agent-to-agent chat (HTTP + SQLite, stdlib only, v{SERVER_VERSION}).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="first-run setup: create DB, register agents")
    p_init.add_argument(
        "agent",
        nargs="*",
        default=["hermes:admin", "chappy:agent", "waynec:admin"],
        help="agents to register as NAME:ROLE (default: hermes, chappy, waynec)",
    )
    p_init.add_argument(
        "--endpoint", default=None,
        help="optional URL where this agent can be reached",
    )
    p_init.add_argument(
        "--force", action="store_true", help="rotate existing tokens"
    )
    p_init.set_defaults(func=cmd_init)

    p_serve = sub.add_parser("serve", help="run the HTTP server")
    p_serve.add_argument("--host", default=DEFAULT_BIND)
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_serve.set_defaults(func=cmd_serve)

    p_id = sub.add_parser(
        "set-identity", help="save local client identity (base_url, name, token)"
    )
    p_id.add_argument("--base-url", dest="base_url")
    p_id.add_argument("--name")
    p_id.add_argument("--token")
    p_id.set_defaults(func=cmd_setidentity)

    # send (works for both v0.1 pairwise and v1 thread)
    p_send = sub.add_parser("send", help="send a message (to=<agent> OR --thread=<id>)")
    p_send.add_argument("to", nargs="?", help="recipient agent name (v0.1 pairwise)")
    p_send.add_argument("body", help="message body")
    p_send.add_argument("--thread", help="post into a thread instead")
    p_send.add_argument("--subject", "-s", default=None)
    p_send.add_argument("--url")
    p_send.add_argument("--name")
    p_send.add_argument("--token")
    p_send.set_defaults(func=cmd_send)

    p_inbox = sub.add_parser("inbox", help="list incoming messages across all threads")
    p_inbox.add_argument("--limit", type=int, default=20)
    p_inbox.add_argument("--unread", action="store_true")
    p_inbox.add_argument("--url")
    p_inbox.add_argument("--name")
    p_inbox.add_argument("--token")
    p_inbox.set_defaults(func=cmd_inbox)

    p_read = sub.add_parser("read", help="show one message")
    p_read.add_argument("msg_id")
    p_read.add_argument("--url")
    p_read.add_argument("--name")
    p_read.add_argument("--token")
    p_read.set_defaults(func=cmd_read)

    p_ack = sub.add_parser("ack", help="mark message as read")
    p_ack.add_argument("msg_id")
    p_ack.add_argument("--url")
    p_ack.add_argument("--name")
    p_ack.add_argument("--token")
    p_ack.set_defaults(func=cmd_ack)

    p_peers = sub.add_parser("peers", help="list known agents")
    p_peers.add_argument("--url")
    p_peers.add_argument("--name")
    p_peers.add_argument("--token")
    p_peers.set_defaults(func=cmd_peers)

    # --- search (cross-thread) ---
    p_search = sub.add_parser(
        "search",
        help="substring search across threads I'm in (body + subject)",
    )
    p_search.add_argument("query", help="substring to match (LIKE syntax)")
    p_search.add_argument("--thread", help="restrict to one thread id")
    p_search.add_argument("--from", dest="from_agent", help="restrict to one sender")
    p_search.add_argument("--limit", type=int, default=50)
    p_search.add_argument("--url")
    p_search.add_argument("--name")
    p_search.add_argument("--token")
    p_search.set_defaults(func=cmd_search)

    # --- react (add/remove/list emoji reactions on a message) ---
    p_react = sub.add_parser(
        "react",
        help="add/remove/list an emoji reaction on a thread message",
    )
    p_react.add_argument("msg_id", help="message id (t_...)")
    # emoji is optional so `agentchat react <msg_id> --list` works without one.
    p_react.add_argument(
        "emoji", nargs="?", default="",
        help="emoji to react with (omit for --list)",
    )
    p_react.add_argument("--remove", action="store_true", help="remove the reaction")
    p_react.add_argument("--list", dest="list_reactions", action="store_true",
                          help="list current reactions on the message")
    p_react.add_argument("--url")
    p_react.add_argument("--name")
    p_react.add_argument("--token")
    p_react.set_defaults(func=cmd_react)

    # --- v1 threads ---
    p_threads = sub.add_parser("threads", help="list threads I'm a member of")
    p_threads.add_argument("--url")
    p_threads.add_argument("--name")
    p_threads.add_argument("--token")
    p_threads.set_defaults(func=cmd_threads)

    p_thread = sub.add_parser("thread", help="thread operations")
    p_thread_sub = p_thread.add_subparsers(dest="thread_cmd", required=True)

    p_t_create = p_thread_sub.add_parser("create", help="create a thread")
    p_t_create.add_argument("thread", help="thread id (e.g. wayne-chappy-hermes)")
    p_t_create.add_argument("--display-name", dest="thread_display_name",
                            help="human-readable thread label (optional)")
    p_t_create.add_argument(
        "--members", required=True,
        help="comma-separated member agent names (creator is auto-added)",
    )
    p_t_create.add_argument("--url")
    p_t_create.add_argument("--name")
    p_t_create.add_argument("--token")
    p_t_create.set_defaults(func=cmd_thread_create)

    p_t_show = p_thread_sub.add_parser("show", help="show thread details")
    p_t_show.add_argument("thread")
    p_t_show.add_argument("--url")
    p_t_show.add_argument("--name")
    p_t_show.add_argument("--token")
    p_t_show.set_defaults(func=cmd_thread_show)

    p_t_msgs = p_thread_sub.add_parser(
        "messages", help="list messages in a thread"
    )
    p_t_msgs.add_argument("thread")
    p_t_msgs.add_argument("--since", type=int, default=0)
    p_t_msgs.add_argument("--limit", type=int, default=50)
    p_t_msgs.add_argument("--unread", action="store_true")
    # Default ordering is now NEWEST FIRST (latest N). Use --oldest to
    # flip back to forward-paginated ASC for cursor-style replay.
    p_t_msgs.add_argument(
        "--oldest", action="store_true",
        help="order oldest-first (ASC); default is newest-first (DESC).",
    )
    p_t_msgs.add_argument("--url")
    p_t_msgs.add_argument("--name")
    p_t_msgs.add_argument("--token")
    p_t_msgs.set_defaults(func=cmd_thread_messages)

    p_t_send = p_thread_sub.add_parser("send", help="post a message into a thread")
    p_t_send.add_argument("thread")
    p_t_send.add_argument("body")
    p_t_send.add_argument("--subject", "-s", default=None)
    p_t_send.add_argument("--url")
    p_t_send.add_argument("--name")
    p_t_send.add_argument("--token")
    p_t_send.set_defaults(func=cmd_send)  # reuses cmd_send, looks at --thread

    # --- watch ---
    p_watch = sub.add_parser(
        "watch",
        help="long-poll: print new messages as they arrive "
             "(--thread X for one thread, none for all)",
    )
    p_watch.add_argument("--thread", help="specific thread to tail")
    p_watch.add_argument(
        "--interval", type=float, default=1.0,
        help="poll interval in seconds (default 1.0)",
    )
    p_watch.add_argument("--quiet", action="store_true")
    p_watch.add_argument("--url")
    p_watch.add_argument("--name")
    p_watch.add_argument("--token")
    p_watch.set_defaults(func=cmd_watch)

    # --- respond (mention-aware auto-responder) ---
    p_respond = sub.add_parser(
        "respond",
        help="watch a thread and post LLM-generated replies when addressed "
             "(triggers: @hermes, @chappy, @wayne, 'guys', 'everyone', "
             "'team', 'both', etc.)",
    )
    p_respond.add_argument("--thread", required=True, help="thread id to watch")
    p_respond.add_argument(
        "--interval", type=float, default=1.5,
        help="poll interval in seconds (default 1.5)",
    )
    p_respond.add_argument(
        "--context", type=int, default=10,
        help="number of recent messages to include in LLM context (default 10)",
    )
    p_respond.add_argument(
        "--debounce", type=float, default=2.0,
        help="seconds to wait before responding, so other agents can post first "
             "(default 2.0; set 0 to disable)",
    )
    p_respond.add_argument(
        "--start-from-id", type=int, default=None,
        help="only consider messages with id > N (use to skip history)",
    )
    p_respond.add_argument(
        "--start-from-now", action="store_true",
        help="only consider messages posted after the daemon starts",
    )
    p_respond.add_argument(
        "--dry-run", action="store_true",
        help="print what would be posted, don't actually post",
    )
    p_respond.add_argument("--quiet", action="store_true")
    p_respond.add_argument("--url")
    p_respond.add_argument("--name")
    p_respond.add_argument("--token")
    # LLM overrides (otherwise read from ~/.hermes/config.yaml)
    p_respond.add_argument(
        "--api-base", dest="api_base", default=None,
        help="override LLM base URL (default: read from config.yaml)",
    )
    p_respond.add_argument(
        "--api-key", dest="api_key", default=None,
        help="override LLM API key (default: read from config.yaml)",
    )
    p_respond.add_argument(
        "--api-model", dest="api_model", default=None,
        help="override LLM model name (default: model.default from config.yaml)",
    )
    p_respond.set_defaults(func=cmd_respond)

    p_status = sub.add_parser("status", help="check endpoint + auth")
    p_status.add_argument("--url")
    p_status.add_argument("--name")
    p_status.add_argument("--token")
    p_status.set_defaults(func=cmd_status)

    p_token = sub.add_parser("token", help="manage tokens (show/rotate/add/rm)")
    p_token.add_argument(
        "action", choices=["show", "rotate", "add", "rm"]
    )
    p_token.add_argument("--name")
    p_token.add_argument("--role", default="agent")
    p_token.add_argument("--endpoint")
    p_token.set_defaults(func=cmd_token)

    # --- admin: audit, export, retention, backup ---
    p_audit = sub.add_parser(
        "audit",
        help="list all threads with members (incl. roles), msg counts, last activity",
    )
    p_audit.add_argument(
        "--format", choices=["text", "json"], default="text"
    )
    p_audit.set_defaults(func=cmd_audit)

    p_export = sub.add_parser(
        "export", help="export a thread's messages to JSON/JSONL/Markdown (stdout)"
    )
    p_export.add_argument("thread", help="thread id to export")
    p_export.add_argument(
        "--format", choices=["json", "jsonl", "md"], default="json"
    )
    p_export.set_defaults(func=cmd_export)

    p_retention = sub.add_parser(
        "retention",
        help="move thread_messages older than --hot-days to a cold-archive DB",
    )
    p_retention.add_argument(
        "--hot-days", type=int, default=365,
        help="keep messages newer than N days in the live DB (default 365)",
    )
    p_retention.set_defaults(func=cmd_retention)

    p_backup = sub.add_parser(
        "backup",
        help="take a hot sqlite3 backup of messages.db; rotates --keep copies",
    )
    p_backup.add_argument(
        "--keep", type=int, default=7,
        help="number of recent backups to keep (default 7)",
    )
    p_backup.set_defaults(func=cmd_backup)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

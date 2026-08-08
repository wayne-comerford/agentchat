"""
Nostr relay-side protocol helpers for agentchat v1.2.

This module is the **inbound** surface — for when agentchat itself
acts as a Nostr relay and wants to authenticate incoming WebSocket
clients via the NIP-42 challenge/response flow.

NIP-42 spec recap (https://github.com/nostr-protocol/nips/blob/master/42.md):
    1. Server sends ["AUTH", "<challenge-string>"]
    2. Client builds a kind:22242 event with tags:
         - ["relay", "<relay-url>"]
         - ["challenge", "<challenge-string>"]
       signs with the user's secret key, sends
       ["AUTH", <signed-event-json>]
    3. Server verifies: signature valid, created_at recent, challenge matches,
       relay-url matches. On success, marks the connection as authenticated.

This module provides pure-Python primitives the route handlers will call:
    - create_challenge()              : server-side nonce generator
    - create_auth_event(...)          : client-side event builder (tests + Hermes)
    - verify_auth_event(...)          : server-side verifier (the security boundary)
    - AuthRateLimiter                 : per-IP sliding-window rate limit for AUTH attempts

It does NOT provide a full WebSocket server — that is wired into
agentchat/__init__.py in a later step. For now these functions are the
pure primitives that the route handlers will call.
"""
from __future__ import annotations

import hashlib
import os
import time
from collections import defaultdict, deque

from pynostr.event import Event as PynostrEvent


# NIP-42 spec allows up to a minute; we use 10 minutes for slack with
# slow / lossy networks. Tighten in production.
DEFAULT_AUTH_MAX_AGE_SECONDS = 600


def create_challenge() -> str:
    """
    Generate a server-side challenge nonce. Returns a 32-char hex string.

    Uses os.urandom so the challenge is cryptographically random. The
    challenge is opaque to the client and only meaningful within the
    lifetime of one WebSocket connection.
    """
    return hashlib.sha256(os.urandom(32)).hexdigest()[:32]


def create_auth_event(
    secret_key_hex: str,
    relay_url: str,
    challenge: str,
    *,
    created_at: int | None = None,
) -> PynostrEvent:
    """
    Build a NIP-42 kind:22242 auth event signed by `secret_key_hex`.

    This is the **client-side** primitive. Used in tests to fabricate
    valid auth events; production clients (Hermes, Chappy) will call
    this from their own Nostr client code.

    The event has empty `content` (NIP-42 says content should be empty
    or arbitrary — we leave it empty for privacy).
    """
    ev = PynostrEvent(
        kind=22242,
        content="",
        pubkey="",  # Event.sign() sets this from the secret key
        created_at=created_at if created_at is not None else int(time.time()),
        tags=[["relay", relay_url], ["challenge", challenge]],
    )
    ev.sign(secret_key_hex)
    return ev


def verify_auth_event(
    auth_event: PynostrEvent | dict,
    *,
    expected_challenge: str,
    expected_relay_url: str,
    max_age_seconds: int = DEFAULT_AUTH_MAX_AGE_SECONDS,
    now: int | None = None,
) -> bool:
    """
    Verify a NIP-42 auth event. Returns True if valid, False otherwise.

    Security checks (in order, all must pass):
        1. Event is well-formed (kind == 22242, has pubkey/created_at/sig)
        2. Event signature is valid (via PynostrEvent.verify)
        3. created_at is within max_age_seconds of now (replay protection)
        4. `challenge` tag matches expected_challenge
        5. `relay` tag matches expected_relay_url

    `now` is injectable for testing.
    """
    now = now if now is not None else int(time.time())

    # Coerce dict -> Event if needed (the agentchat handler will receive
    # JSON from the wire, so this is the common case)
    if isinstance(auth_event, dict):
        try:
            ev = PynostrEvent.from_dict(auth_event)
        except Exception:
            return False
    else:
        ev = auth_event

    # 1. Shape
    if ev.kind != 22242:
        return False
    if not ev.pubkey or not ev.sig:
        return False

    # 2. Signature
    try:
        if not ev.verify():
            return False
    except Exception:
        return False

    # 3. Recency
    created = getattr(ev, "created_at", None)
    if not isinstance(created, int):
        return False
    if abs(now - created) > max_age_seconds:
        return False

    # 4. Challenge match
    try:
        challenge_tags = ev.get_tag_list("challenge")
    except Exception:
        return False
    # get_tag_list returns a list-of-lists (one inner list per tag occurrence).
    # We only care about the first challenge tag.
    if (
        not challenge_tags
        or not challenge_tags[0]
        or challenge_tags[0][0] != expected_challenge
    ):
        return False

    # 5. Relay-url match
    try:
        relay_tags = ev.get_tag_list("relay")
    except Exception:
        return False
    if (
        not relay_tags
        or not relay_tags[0]
        or relay_tags[0][0] != expected_relay_url
    ):
        return False

    return True


# --------------------------------------------------------------------------- #
# Rate limiter (per-IP, sliding-window)
# --------------------------------------------------------------------------- #

class AuthRateLimiter:
    """
    Per-IP sliding-window rate limiter for AUTH attempts.

    Defaults: 5 attempts per 60 seconds per IP. Beyond that, returns False
    from `allow()` until the window slides forward.

    This is process-local (no Redis) — fine for the agentchat single-server
    topology. If we ever scale to multiple agentchat instances behind a load
    balancer we'll need to move to Redis.
    """

    def __init__(self, max_attempts: int = 5, window_seconds: int = 60):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._history: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, ip: str, *, now: float | None = None) -> bool:
        """Return True if `ip` may attempt AUTH right now, False otherwise."""
        now = now if now is not None else time.time()
        bucket = self._history[ip]
        cutoff = now - self.window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.max_attempts:
            return False
        bucket.append(now)
        return True

    def reset(self, ip: str) -> None:
        """Forget all history for one IP. Use after a successful auth."""
        self._history.pop(ip, None)


__all__ = [
    "DEFAULT_AUTH_MAX_AGE_SECONDS",
    "create_challenge",
    "create_auth_event",
    "verify_auth_event",
    "AuthRateLimiter",
]
"""agentchat v1.2 — Nostr-native agent bus.

This subpackage ports the Nostr primitives Wayne needs from Block's Buzz
(https://github.com/block/buzz) into agentchat directly. Clean-room
Python; we read Buzz's source for reference but write our own
implementation under agentchat's MIT license.

Scope (v1.2):
    - NIP-01 keypairs (this package)
    - NIP-29 channels (kind:9, kind:9007, kind:39000)
    - NIP-42 auth (challenge/response)
    - NIP-17 DMs (deferred to step 2)
    - @<pubkey> mention router (deferred to step 2)

Reference impl: Buzz at /home/waynec/buzz (relay binary built).
Interop target: ws://localhost:3000 once MinIO port binding is fixed.

Note: pynostr (our only Nostr dep) uses NIP-28 kind numbers
(CHANNEL_MESSAGE = 42). We bypass pynostr's enum and use the NIP-29
integers directly so we interop with Buzz.
"""

from agentchat.nostr.keys import NostrKeys, load_keys, save_keys
from agentchat.nostr.events import (
    ChannelMeta,
    build_channel_create,
    build_channel_message,
    build_channel_metadata,
    build_user_metadata,
    build_reaction,
    build_delete,
)
from agentchat.nostr.nips import (
    NIP42Challenge,
    bech32_to_pubkey,
    pubkey_to_npub,
    parse_mentions,
)
from agentchat.nostr.client import (
    IncomingEvent,
    RelayEndpoint,
    RelayPool,
    load_pool,
    make_pool_for,
)
from agentchat.nostr.server import (
    DEFAULT_AUTH_MAX_AGE_SECONDS,
    AuthRateLimiter,
    create_auth_event,
    create_challenge,
    verify_auth_event,
)

__version__ = "1.2.0.dev1"
__all__ = [
    "NostrKeys",
    "load_keys",
    "save_keys",
    "ChannelMeta",
    "build_channel_create",
    "build_channel_message",
    "build_channel_metadata",
    "build_user_metadata",
    "build_reaction",
    "build_delete",
    "NIP42Challenge",
    "bech32_to_pubkey",
    "pubkey_to_npub",
    "parse_mentions",
    "IncomingEvent",
    "RelayEndpoint",
    "RelayPool",
    "load_pool",
    "make_pool_for",
    "DEFAULT_AUTH_MAX_AGE_SECONDS",
    "AuthRateLimiter",
    "create_auth_event",
    "create_challenge",
    "verify_auth_event",
]
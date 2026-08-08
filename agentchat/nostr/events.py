"""Nostr event builders for agentchat v1.2 — NIP-29 channels subset.

We use NIP-29 kind integers directly (not pynostr's EventKind enum, which
uses the older NIP-28 numbers — CHANNEL_MESSAGE = 42 there vs. NIP-29 = 9).
This matches Buzz's relay and gives us interop.

Kinds implemented:
    0      SET_METADATA         — user profile (name, about, picture)
    5      DELETE               — event deletion
    7      REACTION             — emoji reaction to another event
    9      NIP-29 GROUP_MESSAGE — channel/group message with #h tag
    9007   NIP-29 GROUP_CREATE  — channel/group creation
    39000  NIP-29 GROUP_META    — relay-signed channel metadata (addressable, d tag)

Every builder returns an unsigned pynostr `Event`. Call `.sign(private_key_hex)`
on it before sending. (`sign()` is in-place; it also fills `id` and `sig`.)

Tag shape per NIP-29:
    - kind:9     tags = [["h", "<group-id>"]]
    - kind:9007  tags = []  (relay assigns the group id; the response event carries it)
    - kind:39000 tags = [["d", "<group-id>"]]   (addressable; d tag = stable id)

Optional tags:
    - kind:9     [["p", "<pubkey>", "<relay-url>"] for @mentions
                 ["e", "<event-id>", "<relay-url>"] for replies
                 ["subject", "<thread-topic>"] to set / change thread topic
    - kind:9007  ["name", "<channel-name>"], ["about", "<description>"],
                 ["picture", "<image-url>"], ["visibility", "public"|"private"]
    - kind:0     ["client", "<agent-name>"]
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from pynostr.event import Event

from agentchat.nostr.keys import NostrKeys


# Kind integers (NIP-29 channel spec, not NIP-28).
KIND_SET_METADATA = 0
KIND_DELETE = 5
KIND_REACTION = 7
KIND_GROUP_MESSAGE = 9
KIND_GROUP_CREATE = 9007
KIND_GROUP_META = 39000


@dataclass(frozen=True)
class ChannelMeta:
    """Channel metadata payload for kind:39000 (relay-signed)."""

    name: str
    about: str = ""
    picture: str = ""
    visibility: str = "public"  # "public" | "private"

    def tags(self, group_id: str) -> List[List[str]]:
        out: List[List[str]] = [["d", group_id], ["name", self.name]]
        if self.about:
            out.append(["about", self.about])
        if self.picture:
            out.append(["picture", self.picture])
        if self.visibility != "public":
            out.append(["visibility", self.visibility])
        return out


def build_user_metadata(
    keys: NostrKeys,
    name: str,
    about: str = "",
    picture: str = "",
    client: str = "agentchat/1.2",
) -> Event:
    """Build a kind:0 SET_METADATA event for the local agent."""
    profile = {
        "name": name,
        "about": about,
        "picture": picture,
        "client": client,
    }
    import json as _json
    return Event(
        content=_json.dumps(profile, separators=(",", ":")),
        pubkey=keys.public_key_hex,
        created_at=int(time.time()),
        kind=KIND_SET_METADATA,
        tags=[],
    )


def build_channel_create(
    keys: NostrKeys,
    name: str,
    about: str = "",
    picture: str = "",
    visibility: str = "public",
) -> Event:
    """Build a kind:9007 GROUP_CREATE event. The relay assigns the group id."""
    tags: List[List[str]] = []
    if name:
        tags.append(["name", name])
    if about:
        tags.append(["about", about])
    if picture:
        tags.append(["picture", picture])
    if visibility != "public":
        tags.append(["visibility", visibility])
    return Event(
        content="",
        pubkey=keys.public_key_hex,
        created_at=int(time.time()),
        kind=KIND_GROUP_CREATE,
        tags=tags,
    )


def build_channel_metadata(
    keys: NostrKeys,
    group_id: str,
    meta: ChannelMeta,
) -> Event:
    """Build a kind:39000 GROUP_META (addressable, uses d tag)."""
    return Event(
        content="",
        pubkey=keys.public_key_hex,
        created_at=int(time.time()),
        kind=KIND_GROUP_META,
        tags=meta.tags(group_id),
    )


def build_channel_message(
    keys: NostrKeys,
    group_id: str,
    content: str,
    mentions: Optional[Iterable[str]] = None,
    reply_to: Optional[str] = None,
    subject: Optional[str] = None,
    extra_tags: Optional[Sequence[Sequence[str]]] = None,
) -> Event:
    """Build a kind:9 GROUP_MESSAGE event.

    Args:
        keys:      the local agent's keypair (sets pubkey)
        group_id:  NIP-29 group id (the value carried in the #h tag)
        content:   message body. Mention syntax inside content is preserved
                   verbatim — `parse_mentions()` extracts bech32 npubs if you
                   want to also add explicit #p tags.
        mentions:  optional iterable of pubkey hex strings to add as #p tags
                   (so clients / harnesses can resolve mentions without parsing
                   content).
        reply_to:  optional event id (hex) to thread under as an #e reply tag.
        subject:   optional thread subject — NIP-29 thread topic. First post
                   sets it; subsequent posts with the same subject continue
                   the thread.
        extra_tags: extra tags appended verbatim (escape hatch for clients that
                    need custom tags).
    """
    tags: List[List[str]] = [["h", group_id]]
    if mentions:
        for pk in mentions:
            tags.append(["p", pk])
    if reply_to:
        tags.append(["e", reply_to, "", "reply"])
    if subject:
        tags.append(["subject", subject])
    if extra_tags:
        for t in extra_tags:
            tags.append([str(x) for x in t])
    return Event(
        content=content,
        pubkey=keys.public_key_hex,
        created_at=int(time.time()),
        kind=KIND_GROUP_MESSAGE,
        tags=tags,
    )


def build_reaction(
    keys: NostrKeys,
    target_event_id: str,
    emoji: str = "+",
    relay_url: str = "",
) -> Event:
    """Build a kind:7 REACTION to another event (e.g. a kind:9 message).

    Args:
        target_event_id: the hex id of the event being reacted to
        emoji:           the reaction content. "+" is the standard "like".
        relay_url:       optional relay hint in the #e tag.
    """
    tags: List[List[str]] = [["e", target_event_id]]
    if relay_url:
        tags[0] = ["e", target_event_id, relay_url]
    return Event(
        content=emoji,
        pubkey=keys.public_key_hex,
        created_at=int(time.time()),
        kind=KIND_REACTION,
        tags=tags,
    )


def build_delete(
    keys: NostrKeys,
    target_event_ids: Iterable[str],
) -> Event:
    """Build a kind:5 DELETE for one or more events authored by `keys`.

    The relay is expected to enforce that only the author can delete their
    own events (NIP-09).
    """
    tags: List[List[str]] = [["e", eid] for eid in target_event_ids]
    return Event(
        content="",
        pubkey=keys.public_key_hex,
        created_at=int(time.time()),
        kind=KIND_DELETE,
        tags=tags,
    )
"""NIP primitives for agentchat v1.2 — NIP-19 / NIP-21 / NIP-42.

What lives here:
    NIP-19 bech32 helpers (npub1... <-> 32-byte hex pubkey)
    NIP-21 nostr: URI parsing for @mention extraction
    NIP-42 client-side auth challenge/response

What lives elsewhere (step 2):
    NIP-44 encrypted DMs (in agentchat/nostr/dm.py) — uses NostrKeys.shared_secret
    NIP-17 gift-wrap envelope (depends on NIP-44)
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from typing import List, Optional

from pynostr.event import Event

from agentchat.nostr.keys import NostrKeys


# ---------------------------------------------------------------------------
# NIP-19 bech32 helpers
# ---------------------------------------------------------------------------

def bech32_to_pubkey(bech: str) -> str:
    """Decode `npub1...` or `nprofile1...` (without relay hints) to a
    64-char lowercase hex pubkey. Raises ValueError on bad input."""
    from pynostr.bech32 import bech32_decode, convertbits

    decoded_bech = bech32_decode(bech)
    if decoded_bech is None or len(decoded_bech) < 2:
        raise ValueError("invalid bech32 input")
    hrp, data = decoded_bech[0], decoded_bech[1]
    if hrp not in ("npub", "nprofile"):
        raise ValueError(f"unexpected bech32 hrp {hrp!r} (want 'npub' or 'nprofile')")
    decoded_bits = convertbits(data, 5, 8, False)
    if decoded_bits is None:
        raise ValueError("bech32 convertbits failed")
    raw = bytes(decoded_bits)
    return raw.hex()


def pubkey_to_npub(pubkey_hex: str) -> str:
    """Encode a 64-char hex pubkey as `npub1...`."""
    from pynostr.bech32 import bech32_encode, convertbits
    bits = convertbits(bytes.fromhex(pubkey_hex), 8, 5, True)
    if bits is None:
        raise ValueError("convertbits failed")
    return bech32_encode("npub", bits, spec="bech32")


# ---------------------------------------------------------------------------
# NIP-21 nostr: URI parsing
# ---------------------------------------------------------------------------

# Matches `nostr:npub1...`, `nostr:nprofile1...`, `nostr:note1...`,
# plus bare `npub1...` references embedded in free-form message content.
#
# Bech32 charset excludes 1, b, i, o to avoid visual ambiguity with 0/1.
# A 32-byte pubkey encodes to 52 base32 chars; with 6 checksum chars the
# total is 58. We allow >=50 to be lenient with test fixtures / short refs.
_BECH32_CHARS = "ac-hj-np-z02-9"
_NOSTR_URI_RE = re.compile(
    r"(?:nostr:)?((?:npub|nprofile|note|naddr)1[" + _BECH32_CHARS + r"]{50,})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NostrMention:
    """An extracted mention/reference from message content."""

    raw: str            # the original bech32 string (np..., nostr:np...)
    pubkey_hex: str     # 64-char hex (for npub / nprofile)
    kind: str           # "npub" | "nprofile" | "note" | "naddr"


def parse_mentions(content: str) -> List[NostrMention]:
    """Extract every Nostr reference from a message body.

    Returns a list of NostrMention. Order preserved; duplicates kept (callers
    can dedupe with `set()` if they want). Unknown bech32 forms are skipped.
    """
    from pynostr.bech32 import bech32_decode

    out: List[NostrMention] = []
    for match in _NOSTR_URI_RE.finditer(content):
        bech = match.group(1)
        decoded = bech32_decode(bech)
        if decoded is None or len(decoded) < 1:
            continue
        hrp = decoded[0]
        if hrp not in ("npub", "nprofile", "note", "naddr"):
            continue
        if hrp in ("npub", "nprofile"):
            try:
                pub_hex = bech32_to_pubkey(bech)
            except Exception:  # noqa: BLE001
                continue
        else:
            # note / naddr point at events, not pubkeys — caller can resolve
            # via the relay if needed. We carry the bech as both raw and
            # pubkey_hex="" so consumers can tell apart.
            pub_hex = ""
        out.append(NostrMention(raw=bech, pubkey_hex=pub_hex, kind=hrp))
    return out


# ---------------------------------------------------------------------------
# NIP-42 client-side authentication
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NIP42Challenge:
    """A NIP-42 AUTH challenge issued by the relay.

    `challenge` is an opaque random string the relay embeds in its AUTH
    event. The client must respond with a signed kind:22242 event that
    contains:
        tags:  [["relay", "<relay-url>"], ["challenge", "<this challenge>"]]
        content: "" (NIP-42) or the relay URL (both accepted by Buzz)
    """

    challenge: str
    relay_url: str

    def build_auth_event(self, keys: NostrKeys) -> Event:
        """Construct the signed kind:22242 AUTH response.

        Signs in-place and returns the event. Caller should publish it
        immediately on the same WebSocket it was challenged on.
        """
        ev = Event(
            content=self.relay_url,
            pubkey=keys.public_key_hex,
            created_at=int(time.time()),
            kind=22242,
            tags=[
                ["relay", self.relay_url],
                ["challenge", self.challenge],
            ],
        )
        ev.sign(keys.private_key_hex)
        return ev

    def verify_response(self, response: Event, *, max_age_seconds: int = 60) -> bool:
        """Validate a kind:22242 AUTH response against this challenge.

        Replay protection: rejects responses whose `created_at` is more than
        `max_age_seconds` away from wall-clock time. (A signed response is
        bound to a single window; the relay's nonce is the secondary check.)
        """
        if response.kind != 22242:
            return False
        if not response.verify():
            return False
        tags = {t[0]: t[1:] for t in response.tags if t}
        relay_in_tag = tags.get("relay", [None])[0]
        if relay_in_tag != self.relay_url:
            return False
        challenge_in_tag = tags.get("challenge", [None])[0]
        if challenge_in_tag != self.challenge:
            return False
        if response.created_at is None:
            return False
        skew = abs(int(time.time()) - int(response.created_at))
        if skew > max_age_seconds:
            return False
        return True


def make_challenge(relay_url: str, *, length: int = 16) -> NIP42Challenge:
    """Server-side helper. Generates a fresh NIP-42 challenge for a relay URL."""
    if length < 8:
        raise ValueError("challenge must be at least 8 chars to be unguessable")
    raw = secrets.token_bytes(length)
    challenge = raw.hex()
    return NIP42Challenge(challenge=challenge, relay_url=relay_url)


def derive_challenge_id(challenge: str) -> str:
    """Stable id for a challenge, used as a primary key in the server's
    challenge store. SHA256 → hex. Cheap to compute; not security-critical."""
    return hashlib.sha256(challenge.encode("utf-8")).hexdigest()
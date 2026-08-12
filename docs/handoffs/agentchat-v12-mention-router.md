# agentchat v1.2 — Mention Router Spec (Chappy's lane)

## Goal
When a NIP-29 channel message (kind:9) contains `@<pubkey>` mentions, dispatch
each mention to the right agent's harness. This is the missing piece between
"we can post/read Nostr events" and "agents can actually talk to each other."

## What's already done (Hermes's side — v1.2.0.dev1)
- `agentchat/nostr/nips.py` — `parse_mentions(content)` handles both forms:
  - bare `npub1...` / `npub1...`
  - prefixed `nostr:npub1...`
  Returns set of bech32 npub strings.
- `agentchat/nostr/events.py` — `build_channel_message(...)` accepts
  `mentions: Iterable[str]` (pubkey hex) and adds them as explicit `#p` tags
  alongside whatever's in content.
- Auth gate (`verify_auth_event`) — used to authenticate who sent the event.

## Chappy's scope — `agentchat/nostr/mention.py`

Pure-Python module. No network. No I/O except calling registered handlers.
Must be testable without a relay.

### Public API

```python
@dataclass
class MentionTarget:
    pubkey_hex: str      # 32-byte x-only schnorr pubkey
    npub: str            # bech32 form
    handler: Callable[["MentionedEvent"], None]  # sync or async OK
    name: str | None = None  # optional human label, e.g. "hermes"


@dataclass
class MentionedEvent:
    """What handlers receive."""
    source_event: dict         # raw Nostr event dict
    channel_id: str            # from #h tag
    mentioned_by_pubkey: str   # who posted the message
    mentioned_at: int          # created_at timestamp
    content: str               # raw content (mentions left as-is)
    reply_to: str | None       # from #e reply tag


class MentionRouter:
    def __init__(self) -> None: ...
    def register(self, target: MentionTarget) -> None: ...
    def unregister(self, pubkey_hex: str) -> bool: ...
    def lookup(self, pubkey_hex_or_npub: str) -> MentionTarget | None: ...
    async def route(self, event: dict) -> list[MentionedEvent]:
        """Extract mentions, dispatch to handlers, return list of dispatched events."""

def extract_mentions(event: dict) -> set[str]:
    """Union of NIP-21 mentions in content + explicit #p tags.
    Returns set of pubkey hex (32-byte)."""
```

### Behavior
- `route()` must be **async** because handlers may be async (e.g. calling
  another relay). Sync handlers should still work.
- Unknown pubkey → silently skipped, but logged at DEBUG.
- Self-mention (mentioned_by == target.pubkey) → optionally skipped via
  `skip_self=True` flag in `route()`.
- Multiple mentions in same event → all dispatched, in registration order.
- Handler exception → logged, does NOT abort other handlers (use
  `asyncio.gather(..., return_exceptions=True)` internally).
- Idempotency: deduplicate by (event_id, target_pubkey) within a single
  `route()` call so a single event with the same pubkey in both content
  and #p tags doesn't double-dispatch.

### Tests — `tests/test_nostr_mention.py`

Minimum coverage:
- `extract_mentions`:
  - bare npub in content
  - `nostr:` prefixed npub
  - `#p` tag (hex pubkey) added to set
  - event with no mentions → empty set
  - malformed mention (truncated, wrong checksum) → silently ignored
- `MentionRouter`:
  - register + lookup
  - route to single handler
  - route to multiple handlers (all fire)
  - route with self-mention (skip_self=True)
  - unknown pubkey → no error, no dispatch
  - handler exception → other handlers still fire
  - double-dispatch prevention (same pubkey in content AND #p tag)
- Use the existing `~/.hermes/nostr/registry.json` as fixture data for one
  test (load registry, register targets, dispatch against it).

### Out of scope for this PR
- Wiring into the relay's on_event (Hermes's job — touch relay, not router)
- Wiring into agentchat's HTTP routes (Hermes's job — touch agentchat/__init__.py)
- Persistence of the router registry (in-memory only)
- DM mentions (NIP-17 kind:14 gift-wrapped) — deferred to v1.2.x

## Coordination with Hermes (next session)

Once `mention.py` lands:
- **Hermes wires** `MentionRouter` into `echo_relay.py`'s `on_event` so the
  echo relay dispatches mentions to a test handler that prints to stdout.
  Scope: 10 lines, no agentchat core touched.
- **Hermes writes** an end-to-end test: post a kind:9 with `@<chappy-npub>`,
  assert the test handler fires.
- **Hermes writes** the same wiring into `agentchat/__init__.py` for the
  /v1/messages route — when an HTTP message has `@chappy` in content, post
  a mirror event to the configured Nostr channel. Scope: ~30 lines.

## Reference: existing event builder signature

```python
from agentchat.nostr.events import build_channel_message

ev = build_channel_message(
    keys=my_keys,
    group_id="dinner-channel",
    content="hey @npub14w6mrc... what's up?",
    mentions=["14w6mrch326m320aqxkutrwdldqcu9jyct536khhjeq3zwj26mmjqcajrlq"],  # chappy hex
)
```

## Reference: our npubs (from ~/.hermes/nostr/registry.json)

```
hermes          npub1d8em5mg3ve5hvuqxywmf08xr7tggjadcyav04pn0yyr2fef9fjksavdaqj
chappy          npub14w6mrch326m320aqxkutrwdldqcu9jyct536khhjeq3zwj26mmjqcajrlq
wayne-observer  npub1c9xu8vdvf97z5qnhzuppp7d9mxn0stp4qhs6d62da9vd9z9mjhjqjgjg28
```

## Acceptance criteria
- [ ] `mention.py` written, public API above complete
- [ ] `tests/test_nostr_mention.py` with ≥12 tests, full suite green
- [ ] No modifications to existing files outside `agentchat/nostr/mention.py`
      and `tests/test_nostr_mention.py`
- [ ] Committed to `main` with commit message naming Chappy as author
- [ ] Heads-up posted to Hermes once committed (agentchat thread or TG DM)

— Hermes, 2026-08-08, on Wayne's behalf (Wayne requested the split)
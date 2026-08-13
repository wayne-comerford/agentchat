# Nudge: mention router (2026-08-13)

yo chappy — nudge on the mention router brief.

## What's shipped on my side (Slice 1 UX)
- **Bridge** on `:9877` — HTTP + SSE polling relay `/events` every 1.5s
- **Relay** on `:9876` — 5 events stored (3 historical + 2 fresh)
- **kind:9 events** with `#h` (channel) + `#p` (mention) tags flowing end-to-end
- **@mention autocomplete** + agents list wired to `~/.hermes/nostr/registry.json`
- **154/154 tests** green, committed `2c1d1ae v1.2.0.dev3`

## What I need from you
Per brief at `docs/handoffs/agentchat-v12-mention-router.md`:
- `extract_mentions(event) -> set[pubkey_hex]` (NIP-21 + `#p` tags union)
- `MentionRouter` class with handler-exception isolation
- Double-dispatch dedupe
- Self-mention skip
- 12+ tests

## What unlocks once you ship
~30 lines of wiring into `agentchat/web/nostr_bridge.py` (SSE handler + `/v1/messages` post route) → agent replies show up in the UI automatically.

## Where to drop it
- Push to main, OR
- Filesystem handoff at `docs/handoffs/from-chappy/` (I poll on Sentinel cron)

— hermes
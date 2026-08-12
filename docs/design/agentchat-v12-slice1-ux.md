# agentchat v1.2 — Slice 1 UX Plan

**Goal:** Ship a Slack-quality chat UI that demonstrates agentchat v1.2 as a
real workspace for humans + agents. Demo-able in 2 weeks of focused work.

**Out of scope this slice:** voice, git repos, workflows, threads, DMs,
reactions, file upload UI, search, presence. (Those are Slices 2 & 3.)

## Tech stack

- **Frontend:** HTMX 2 + Alpine.js 3 + Tailwind 3 (via CDN, no build step)
- **Backend bridge:** Python `aiohttp` SSE handler → subscribes to Nostr relay
  via `pynostr`, fans out incoming `EVENT` messages to browser as SSE events
- **No React/Next.js** — keeps Slice 1 to a single Python file + a few HTML
  templates. Matches RestTech's Tailwind aesthetic.

## Layout (Slack-inspired, dark by default)

```
┌──────────────────────────────────────────────────────────────┐
│  ▌Channels  ▌DMs  ▌Agents           [Hermes ▾]   [Search 🔍] │
├──────────┬───────────────────────────────────────────────────┤
│ # dinner │  # dinner-channel                            ◉ 3  │
│ # opera  │  ──────────────────────────────────────────────── │
│ + new    │                                                    │
│          │  [fizz 🤖]   hermes                          14:23 │
│ Agents   │  hey @chappy, are you there?                       │
──────────│                                                     │
│ 🤖 hermes│  [bumble 🤖] chappy                       14:23   │
│ 🤖 chappy│  pong — relay on, mention router queued            │
│ 🤖 wayne │                                                     │
│          │  ──────────────────────────────────────────────── │
│          │  [Post to #dinner-channel..............] [Send 📤]│
└──────────┴───────────────────────────────────────────────────┘
```

Three columns on desktop, collapses to one on mobile.

## Components (frontend)

| Component | Tech | Data source |
|-----------|------|-------------|
| `sidebar.html` | Tailwind + HTMX | `GET /v1/ui/channels` + `GET /v1/ui/agents` |
| `channel_header.html` | HTMX partial | `GET /v1/ui/channels/{id}` |
| `message_stream.html` | HTMX + SSE | `GET /v1/ui/stream?channel={id}` (SSE) |
| `message.html` | Tailwind | SSE event payload |
| `post_box.html` | Alpine.js | `POST /v1/ui/post` |
| `agent_card.html` | Tailwind | `GET /v1/ui/agents/{npub}` |
| `mention_autocomplete.html` | Alpine.js | `GET /v1/ui/agents?prefix={q}` |

## Backend bridge (`agentchat/web/nostr_bridge.py`)

Single Python file. Uses:
- `aiohttp` for HTTP server + SSE
- `agentchat.nostr.client.RelayPool` to subscribe to configured channels
- `agentchat.nostr.keys.load_keys` to load Hermes keypair for signing

Endpoints:
- `GET /` — render base shell
- `GET /v1/ui/channels` — list channels (from local config + relay-discovered)
- `GET /v1/ui/agents` — list agents from `~/.hermes/nostr/registry.json`
- `GET /v1/ui/stream?channel=<id>` — SSE bridge: subscribe to relay, push
  `kind:9` events for that channel
- `POST /v1/ui/post` — sign + publish kind:9 to configured channel
- `GET /static/*` — serve HTML/CSS/JS

## Config

New file: `~/.hermes/nostr/agentchat-bridge.yaml`
```yaml
listen:
  host: 127.0.0.1
  port: 9877
relays:
  - ws://127.0.0.1:9876  # local echo relay
identity:
  key_path: ~/.hermes/nostr/hermes.json
  name: hermes
default_channel: dinner-channel
```

## Design tokens (Tailwind config)

- **Palette:** slate-900 background, slate-800 surfaces, sky-400 accent
  (matches RestTech, easy to swap)
- **Font:** Inter (UI), JetBrains Mono (npub/keys)
- **Avatars:** generated from npub prefix — colored circle with first 2 chars
  (no need for user-uploaded profile pics; Nostr identity is enough)
- **Density:** Slack-style compact by default, comfortable toggle

## Files to create

```
agentchat/web/
├── nostr_bridge.py          # main entrypoint (aiohttp SSE server)
├── templates/
│   ├── base.html             # shell + Tailwind + HTMX CDN
│   ├── sidebar.html          # channels + agents
│   ├── channel.html          # header + stream + post box
│   ├── message.html          # single message render
│   ├── agent_card.html       # agent identity card
│   └── post_box.html         # input + send
├── static/
│   ├── app.css               # minimal overrides
│   └── app.js                # SSE handler, autocomplete glue
└── README.md                 # how to run
```

## Tests

- `tests/web/test_nostr_bridge.py` — endpoint smoke tests
  - `/v1/ui/channels` returns list
  - `/v1/ui/agents` returns registry entries
  - `/v1/ui/post` requires body, returns event id
  - SSE stream emits events as relay publishes them
- Manual smoke test:
  - Start `echo_relay.py`
  - Start `nostr_bridge.py`
  - Open `http://127.0.0.1:9877` in browser
  - See existing events, post a new one, see it appear

## Dependency on Chappy

**None for Slice 1's UI shell.** The bridge reads kind:9 from the relay and
displays them. Mentions are parsed and shown highlighted, but the router
(Chappy's `mention.py`) is not invoked yet. Slice 1 demos the workspace
without dynamic agent routing.

When Chappy's `mention.py` lands:
- Slice 1.5 (small): wire `MentionRouter` into `nostr_bridge.py`'s SSE handler
  so when a kind:9 mentions `@<hermes>`, we display a "needs response" badge

## Demo path (when Slice 1 is done)

1. `RELAY_URL=ws://127.0.0.1:9876 .venv/bin/python agentchat/nostr/echo_relay.py &`
2. `.venv/bin/python agentchat/web/nostr_bridge.py &`
3. Open `http://127.0.0.1:9877`
4. See channels from relay, agents from registry
5. Post a message from the UI
6. Open a second browser tab, see it appear in real-time via SSE
7. Post from CLI (`pynostr` or our `RelayPool.publish_channel_message`),
   see it appear in browser

## Honest timeline

| Stage | Days | Cumulative |
|-------|------|-----------|
| Plan + scaffold | 1 | 1 |
| Backend bridge (aiohttp + SSE) | 2 | 3 |
| Sidebar + agent cards | 1 | 4 |
| Message stream + SSE render | 2 | 6 |
| Post box + sign + publish | 1 | 7 |
| @mention autocomplete | 1 | 8 |
| Design tokens + polish | 1 | 9 |
| Smoke test + manual QA | 1 | 10 |
| **Slice 1 demo-able** | | **~10 working days** |

Assumes no RestTech emergencies interrupt. If RestTech fires, this slips
by that amount.

— Hermes, 2026-08-12, kicked off on Wayne's go-ahead
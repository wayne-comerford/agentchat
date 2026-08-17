# agentchat

> **Status:** Active development — **v1.2 = Nostr-native pivot** (2026-08-07).
> We're porting the Nostr primitives from Block's [Buzz](https://github.com/block/buzz)
> into agentchat directly (clean-room Python, no code copy). Reference:
> Buzz serves as our interop test bed on Node3.

---

Self-hostable agent-to-agent chat bus. Bearer-token auth, workspace scoping,
SQLite by default, zero third-party deps. Built so two different agent
ecosystems (Hermes, OpenClaw, Goose, your own scripts) can talk to each
other — and to a human in a Telegram-style UI.

```
$ python3 -m agentchat serve --host 127.0.0.1 --port 7878
$ python3 -m agentchat web   --host 0.0.0.0  --port 7879 --api http://127.0.0.1:7878
$ open http://127.0.0.1:7879/
```

* ~4,200 LOC, single Python module + single HTML file
* stdlib only — no `pip install` needed
* SQLite + WAL, ~50 MB RAM for 10 peers
* Mobile-first web UI, installable as a PWA

---

## Install

```bash
git clone https://github.com/wayne-comerford/agentchat
cd agentchat
python3 -m agentchat init          # creates ~/.agentchat/ + writes a workspace
python3 -m agentchat serve         # API on :7878
python3 -m agentchat web --port 7879 --api http://127.0.0.1:7878
```

Or with Docker:

```bash
docker compose up -d
```

---

## Quick start

1. Open `http://localhost:7879/` in your browser
2. Click **Register**, pick a username / password / workspace name
3. You're in. Create or join a thread and start chatting.

From the CLI:

```bash
python3 -m agentchat register   --username wayne --password *** --workspace resttech
python3 -m agentchat login      --username wayne --password *** --workspace resttech
python3 -m agentchat threads list
python3 -m agentchat messages post --thread hermes-chappy --body "hi from CLI"
```

---

## Deploy with TLS (Caddy)

Production deployments should run agentchat behind a reverse proxy that
terminates TLS. We recommend **Caddy** for zero-config Let's Encrypt + HTTP/2.

1. Point a DNS A record at your server (`chat.example.com`)
2. Install Caddy (`apt install caddy` or `brew install caddy`)
3. Copy `Caddyfile.example` to `/etc/caddy/Caddyfile`, replacing the domain
4. Run agentchat behind Caddy:

```bash
# API bound to loopback only — Caddy talks to it
AGENTCHAT_HOME=/var/lib/agentchat python3 -m agentchat serve --host 127.0.0.1 --port 7878

# WebUI bound to loopback only
python3 -m agentchat web --host 127.0.0.1 --port 7879 --api http://127.0.0.1:7878
```

5. `systemctl reload caddy`

Caddy will fetch a Let's Encrypt cert automatically. The included
`Caddyfile.example` sets:
* `flush_interval -1` so SSE streams aren't buffered
* `read_timeout 0` so long-lived event connections don't get killed
* `rate_limit` on `/v1/auth/*` as defense in depth (server has its own limiter too)

---

## API at a glance

All endpoints under `/v1/*` require `Authorization: Bearer <token>` unless
otherwise noted. Same-origin requests don't need a CORS allowlist entry;
cross-origin requests must include the production origin in
`AgentChatHandler._ALLOWED_ORIGINS`.

| Method | Path                                  | Notes                               |
|--------|---------------------------------------|-------------------------------------|
| POST   | `/v1/auth/register`                   | Public; rate-limited                |
| POST   | `/v1/auth/login`                      | Public; rate-limited                |
| POST   | `/v1/auth/logout`                     | Revokes the current token           |
| POST   | `/v1/auth/forgot`                     | Public; issues a reset token        |
| POST   | `/v1/auth/reset`                      | Public; consumes a reset token      |
| GET    | `/v1/whoami`                          | Current agent + workspace           |
| GET    | `/v1/threads`                         | Threads you're a member of          |
| POST   | `/v1/threads`                         | Create a thread                     |
| GET    | `/v1/threads/all`                     | All threads (admin view, all members) |
| GET    | `/v1/audit`                           | Structured event log (filterable)   |
| GET    | `/v1/threads/<id>/messages?limit=N`   | Latest N messages (newest-first)    |
| POST   | `/v1/threads/<id>/messages`           | Post a message                      |
| GET    | `/v1/threads/<id>/events?since=N`     | SSE stream (15s heartbeat)          |
| GET    | `/v1/messages/<id>/reactions`         | List reactions on a message         |
| POST   | `/v1/messages/<id>/reactions`         | Add an emoji reaction (idempotent)  |
| DELETE | `/v1/messages/<id>/reactions?emoji=X` | Remove an emoji reaction            |
| GET    | `/v1/search?q=...`                    | Cross-thread full-text search       |
| POST   | `/v1/webhooks/subscribe`              | Subscribe to event topics           |
| DELETE | `/v1/webhooks/<id>`                   | Deactivate a subscription           |
| GET    | `/v1/webhooks/subscriptions`          | List your subscriptions             |
| GET    | `/v1/webhooks/deliveries?sub_id=N`    | Delivery log (status, attempts)     |
| POST   | `/v1/files`                           | Upload (multipart, sha256-dedup)    |
| GET    | `/v1/files/<id>`                      | File metadata                       |
| GET    | `/v1/files/<id>/download`             | Download the file bytes             |
| DELETE | `/v1/files/<id>`                      | Delete your file (refcount-aware)   |
| GET    | `/health`                             | Liveness probe (no auth)            |

See `HANDOFF.md` for the full peer-integration guide and `openapi.yaml` for
the machine-readable OpenAPI 3.1 spec covering every endpoint.

### Nostr Bridge (`agentchat.web.nostr_bridge`) — v1.2+

The Nostr-native chat surface that powers the web UI. The bridge runs on
`:9877` by default and talks to the local echo relay (`: `:9876`) via HTTP.
All mutations require a logged-in session cookie (set by
`/v1/auth/login`); reads are public.

| Method | Path                                                       | Notes                               |
|--------|------------------------------------------------------------|-------------------------------------|
| GET    | `/`                                                        | Chat workspace (HTML)               |
| GET    | `/settings`                                                | Settings page (HTML)                |
| GET    | `/health`                                                  | Liveness probe                      |
| GET    | `/v1/ui/channels`                                          | Channels the bridge knows about     |
| GET    | `/v1/ui/agents`                                            | Loaded agents + npubs               |
| GET    | `/v1/ui/stream?channel=<id>`                               | SSE: kind:9 events for a channel    |
| GET    | `/v1/ui/stream?channel=<id>&since=<ts>`                    | SSE with resume cutoff (unix ts)    |
| GET    | `/v1/ui/stream` + `Last-Event-ID: <nostr_event_id>`        | SSE resume from last seen event     |
| POST   | `/v1/ui/post`                                              | Publish a kind:9 message            |
| POST   | `/v1/auth/login`                                           | Public; sets session cookie         |
| POST   | `/v1/auth/logout`                                          | Clear session cookie                |
| GET    | `/v1/auth/whoami`                                          | Current session                     |
| GET    | `/v1/auth/identities`                                      | List known local identities         |
| GET    | `/v1/ui/memory/sources`                                    | List snapshot dirs (for import)     |
| POST   | `/v1/ui/memory/import`                                     | Bootstrap a new agent's memory      |
| GET    | `/v1/ui/memory/agents`                                     | All agents + structured sections    |
| GET    | `/v1/ui/memory/agents/{name}`                              | One agent's sections                |
| POST   | `/v1/ui/memory/agents/{name}/sections/{s}/lines`           | Append line                         |
| DELETE | | `/v1/ui/memory/agents/{name}/sections/{s}/lines/{idx}`   | Remove line by index                |
| PUT    | `/v1/ui/memory/agents/{name}/sections/{s}`                 | Replace section body                |

Curl examples:

```bash
# Login (sets agentchat_session cookie)
curl -c /tmp/jar -X POST http://localhost:9877/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"name":"hermes"}'

# Post a message
curl -b /tmp/jar -X POST http://localhost:9877/v1/ui/post \
  -H 'Content-Type: application/json' \
  -d '{"channel":"general","content":"hello from curl"}'

# Subscribe to a channel via SSE (3s preview)
timeout 3 curl -N http://localhost:9877/v1/ui/stream?channel=general

# Read an agent's structured memory
curl http://localhost:9877/v1/ui/memory/agents/hermes | jq

# Edit a line in a section
curl -b /tmp/jar -X PUT http://localhost:9877/v1/ui/memory/agents/hermes/sections/Prefs \
  -H 'Content-Type: application/json' \
  -d '{"lines":["loves terse replies","no fluff","uses vi"]}'
```

Smoke test: `./scripts/bridge-smoke.sh` (or `BRIDGE=host:port ./scripts/bridge-smoke.sh`).
Full e2e suite: `pytest tests/test_bridge_e2e.py`.

### Webhooks (v1.1.0+)

Subscribe external services to `thread_create`, `message_post`, and `react`
events. Subscribed targets receive an HTTP POST with an HMAC-SHA256
signature header.

```
# 1. Subscribe
curl -X POST http://127.0.0.1:7878/v1/webhooks/subscribe \
     -H "Authorization: Bearer $AC_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"topic":"message_post","target_url":"https://myapp.example.com/hooks/agentchat"}'
# → 201 { "id": 7, "topic": "message_post", "secret": "<shown once>" }

# 2. Verify (Node.js)
const crypto = require('crypto');
const sig = req.headers['x-agentchat-signature'];
const payload = req.rawBody; // need raw body, NOT parsed JSON
const ok = crypto.createHmac('sha256', secret).update(payload).digest('hex') === sig;
```

Backoff is exponential: `1s → 5s → 30s → 5m → 30m`, max 5 attempts. After 5
failures the delivery is marked `failed` and the subscription auto-disables.
Inspect failures via `GET /v1/webhooks/deliveries?sub_id=7`.

### File storage (v1.1.1+)

Upload via `multipart/form-data`, dedupe on `sha256`. Identical bytes
uploaded twice share one row (refcount++). Deleting once decrements; only
the last `DELETE` actually removes the bytes.

```
# Upload
curl -X POST http://127.0.0.1:7878/v1/files \
     -H "Authorization: Bearer $AC_TOKEN" \
     -F "file=@./photo.png"
# → 201 { "id": 42, "deduped": false, "size_bytes": 184230, ... }

curl http://127.0.0.1:7878/v1/files/42/download \
     -H "Authorization: Bearer $AC_TOKEN" -o out.png
```

Default cap 25 MiB (`AGENTCHAT_MAX_UPLOAD_BYTES`), default mime allowlist
`image/*,application/pdf,text/*,application/json,application/octet-stream`
(`AGENTCHAT_ALLOWED_MIME`). Local-disk default (`AGENTCHAT_FILES_DIR` or
`$AGENTCHAT_HOME/files/`); S3 swap-in via:

```
export AGENTCHAT_FILE_BACKEND=s3
export S3_BUCKET=my-bucket
export S3_ACCESS_KEY=...
export S3_SECRET_KEY=...
pip install 'agentchat[s3]'
```

---

## Security

- **Passwords**: `hashlib.scrypt` with `n=2**15, r=8, p=1, maxmem=64 MiB`,
  per-user 16-byte salt, stored as `scrypt$<salt-hex>$<hash-hex>`
- **Tokens**: opaque `secrets.token_urlsafe(32)`, SHA-256 hashed before
  persistence, 24h TTL, revocable via `/v1/auth/logout`
- **Auth brute-force**: 10 attempts / 60s per IP on `/v1/auth/*` (in-memory)
- **CORS**: empty allowlist = same-origin only; add prod origins before
  exposing the API to a browser on a different domain
- **SQLite**: WAL + `synchronous=NORMAL` + `busy_timeout=5s` for
  concurrency + crash safety
- **Logs**: bearer tokens + passwords scrubbed before write
- **Signal handling**: SIGTERM/SIGINT trigger a graceful drain (no
  dropped requests on `systemctl stop` or `docker stop`)

See `SECURITY.md` for the threat model and how to report issues.

---

## Verify

Quick end-to-end smoke test (no deps beyond curl + Python):

```bash
bash scripts/verify-roundtrip.sh wayne "test-secret" hermes-chappy
```

Runs an 8-step check: register → whoami → threads → post → search →
reactions → logout → forgot-password. Should print `All 8 steps OK`.

## Test suite

Finer-grained assertions live in `tests/` and drive a real `agentchat serve`
process over HTTP (stdlib only; pytest is the sole dev dependency):

```bash
pip install -r requirements-dev.txt
python3 -m pytest
```

Coverage includes auth (register/login/logout/forgot/reset), threads and
membership gating, messages and acks, reactions, cross-thread search, the SSE
event stream, the MCP stdio server, and the auth rate limiter.

---

## License

MIT. See `LICENSE`.

## Contributing

See `CONTRIBUTING.md`. PRs welcome — keep it small, keep it stdlib.
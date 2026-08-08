# Changelog

All notable changes to agentchat are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/) and the project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added (v1.2.0 — Nostr-native pivot, in progress)

- **`agentchat.nostr` subpackage** — clean-room Python port of Nostr
  primitives inspired by Block's Buzz (Apache-2.0 reference impl).
  Wayne's pivot: instead of migrating to Buzz, we port what we need
  into agentchat directly. No Rust toolchain. Single Python codebase.
- **NIP-01 keypairs** — `NostrKeys` class with chmod-600 file load/save
  (`load_keys`, `save_keys`). Refuses to load key files with group/world
  read permissions.
- **NIP-29 channel events** — `build_channel_create` (kind:9007),
  `build_channel_message` (kind:9), `build_channel_metadata` (kind:39000),
  with optional `#p` mention tags, `#e` reply tags, and `subject`
  thread tags.
- **User / reaction / delete events** — `build_user_metadata` (kind:0),
  `build_reaction` (kind:7), `build_delete` (kind:5).
- **NIP-19 bech32** — `pubkey_to_npub` / `bech32_to_pubkey` roundtrip.
- **NIP-21 mention parser** — `parse_mentions(content)` extracts npub /
  nprofile / note / naddr references from free-form text.
- **NIP-42 auth** — `NIP42Challenge` with `build_auth_event` (client) and
  `verify_response` (server). Replay protection via timestamp skew check.
- **37 new tests** in `tests/test_nostr.py` — keys, events, signing,
  bech32 roundtrip, mention parsing, NIP-42 auth handshake, tampered
  signature rejection, expired-timestamp rejection.

### Reference
- Buzz relay binary at `/home/waynec/buzz/target/release/buzz-relay`
  (reference impl for interop testing; 5-min MinIO port binding fix
  pending per `docs/handoffs/buzz-spike-runbook.md`).

## [1.1.2] — 2026-07-06

### Added
- Structured audit log: `audit_log` table (`id`, `actor`, `action`,
  `target_type`, `target_id`, `metadata`, `at`) with indices on `at`,
  `actor`, `action`, and `(target_type, target_id)`.
- `agentchat.audit_log(action, actor, target_type, target_id, metadata)` —
  best-effort insert; failures log a warning and never block the action.
- `agentchat.audit_list(actor, action, target_type, target_id, since_iso,
  until_iso, limit)` — filtered, newest-first read.
- `GET /v1/audit` endpoint with query params `actor`, `action`,
  `target_type`, `target_id`, `since`, `until`, `limit` (max 500).
- Audit instrumentation wired into: `register`, `login`, `logout`,
  `webhook_subscribe`, `webhook_unsubscribe`, `file_upload`.
- Old admin threads view moved from `/v1/audit` to `/v1/threads/all`
  (with `/v1/audit_threads` alias for backwards compat).
- 10 new audit tests (`tests/test_audit.py`) covering register/login
  trails, file/webhook audit, filter-by-actor + filter-by-since, unauth
  401, helper unit tests, and forward-compat with unknown actions.
  Full suite: 66/66 green.

### Changed
- `valid_actions` is a soft contract (`VALID_AUDIT_ACTIONS`); unknown
  actions are still logged (so we can detect drift), but tooling can warn.

## [1.1.1] — 2026-07-06

### Added
- File storage endpoints (`POST /v1/files`, `GET /v1/files/<id>`,
  `GET /v1/files/<id>/download`, `DELETE /v1/files/<id>`). Multipart upload,
  sha256 dedupe with `ref_count`, refcount-aware delete, ownership checks.
  Local disk backend by default; S3 swap-in via `AGENTCHAT_FILE_BACKEND=s3`
  plus standard `S3_*` env vars.
- Defaults: `AGENTCHAT_MAX_UPLOAD_BYTES=25 MiB`,
  `AGENTCHAT_ALLOWED_MIME=image/*,application/pdf,text/*,application/json,application/octet-stream`.
  Both env-overridable.
- Tiny stdlib-only multipart parser (`BaseHTTPRequestHandler._read_multipart`)
  that bypasses the 64 KiB JSON body limit.
- 11 new file tests (`tests/test_files.py`) covering upload, meta, download,
  dedupe (same content → same id, ref_count++; different content → new id),
  ownership (only owner can delete), refcount lifecycle (decrement → wipe),
  allowed mime glob (`image/png` accepted), blocked mime (exe rejected),
  unauth (401), and size cap (in-process). Full suite: 56/56 green.

### Changed
- Test fixture `server` is now function-scoped (was session-scoped). Adds
  ~40 s to the suite but eliminates cross-file socket-disconnect flakes.
- Test client `Client.upload()` for multipart POSTs; urllib timeouts 10 s → 30 s.

## [1.1.0] — 2026-07-05

### Added
- Webhook ingress (`POST /v1/webhooks/subscribe`, `GET /v1/webhooks/subscriptions`,
  `GET /v1/webhooks/deliveries`, `DELETE /v1/webhooks/subscriptions/<id>`).
  Subscriptions get an HMAC-SHA256 signing secret returned once on creation.
  Events fan out: `thread.created`, `message.posted`, `reaction.added`.
  Delivery uses an in-process background drain on a 2s tick with exponential
  backoff (1s, 5s, 30s, 5m, 30m) and a 5-attempt cap, then marks `failed_at`.
  `event_id` is a UUID v4 dedupe key (UNIQUE constraint).
- `pyproject.toml` — `pip install agentchat` from GitHub or sdist. Stdlib-only
  runtime. Optional extras: `dev`, `s3`, `postgres`.
- 11 new webhook tests (`tests/test_webhooks.py`) covering subscribe/sign,
  signature verification, retry+backoff, dedupe, and secret non-disclosure.
  Full suite: 45/45 green.

## [1.0.0] — 2026-07-05

### Added
- LTS promise — `SERVER_VERSION` bumped to `1.0.0`. Single-namespace-per-server
  model locked in (one server = one trust domain). Isolation model documented
  in `SECURITY.md`.

### Added
- `tests/` — pytest suite driving a real `agentchat serve` process over HTTP.
  Covers auth (register/login/logout/forgot/reset), threads + membership
  gating, messages + ack, reactions, cross-thread search, the SSE event
  stream, the MCP stdio server, and the auth rate limiter (34 tests).
- `requirements-dev.txt` (referenced by `requirements.txt` but previously
  missing) and `pytest.ini`.
- `openapi.yaml` — OpenAPI 3.1 spec covering every `/v1` endpoint.
- CI now runs the pytest suite in addition to `verify-roundtrip.sh`.

### Changed
- Rate limit is now configurable via `LOGIN_RATE_LIMIT` (default 10, `0`
  disables) instead of a hardcoded constant. Token lifetimes are configurable
  via `TOKEN_TTL_SECONDS` / `REFRESH_TTL_SECONDS`. These env vars were
  documented in `.env.example` but not previously honored by the code.

### Docs
- `.env.example` rewritten to match the code (scrypt not bcrypt, real
  defaults, only variables the process actually reads).
- Clarified the isolation model in `SECURITY.md`, the schema comment, and
  `ROADMAP.md`: content access is gated by thread membership, not by a
  per-workspace `workspace_id` (removed the overstated claim).
- `ROADMAP.md` refreshed to v0.3.0 with shipped items checked off.

## [0.2.0] — 2026-06-29

### Security
- Per-IP rate limit on `/v1/auth/login` and `/v1/auth/register`
  (10 requests / 60s, in-memory token bucket, returns 429 + `Retry-After: 60`)
- CORS / Origin allowlist on `/v1/*` (empty set = same-origin only; add
  prod origins to `AgentChatHandler._ALLOWED_ORIGINS`)
- Graceful shutdown on SIGTERM/SIGINT (drains SSE clients, closes socket,
  exits cleanly — replaces the bare `KeyboardInterrupt` handler)
- Log scrubber strips `Bearer <token>` and `"password":"...","token":"..."`
  fields before any line is written to stderr or `server.log`

### Performance
- SQLite WAL + `PRAGMA synchronous=NORMAL` + `busy_timeout=5000ms`
  (5-10× write throughput; safe with WAL)
- Connection-pool-style DB connection reused across the SSE polling
  loop (was opening a fresh connection per poll iteration)

### Reliability
- `verify-roundtrip.sh` extended from 6 → 7 steps: now includes
  `register` (step 0) and `logout` (step 7, confirms token is revoked
  with a follow-up 401 check)

### Deploy
- `Dockerfile` — slim Python 3.11 image, runs as non-root, healthcheck
- `docker-compose.yml` — API + WebUI on a shared network with a named
  volume for the SQLite DB
- `Caddyfile.example` — auto-TLS via Let's Encrypt, SSE-friendly
  streaming config (`flush_interval -1`, `read_timeout 0`), defense-in-
  depth rate limit on `/v1/auth/*`
- `.github/workflows/ci.yml` — runs `verify-roundtrip.sh` on every
  push to `main` and on every PR

### Docs
- `README.md` rewritten with install / quick-start / TLS deploy /
  API reference / security notes
- `ROADMAP.md` updated with v0.2.0 in-progress items + a releases table

## [1.3.0] — 2026-06-29 (unreleased → included in 0.2.0)

### Added
- Server-Sent Events endpoint `/v1/threads/<id>/events` for live message +
  reaction updates (`?since=<msg_id>` cursor support)
- Web UI v1.3.0: SSE consumer, mobile-responsive layout (single-pane on
  phones with back-arrow), PWA-installable (manifest, service worker,
  192/512 icons)
- Second `agentchat-respond` systemd unit for running multiple daemons on
  different threads

### Changed
- Web UI now subscribes to SSE on thread open instead of polling the
  messages endpoint every 1.5s (was causing visible flash)
- `SERVER_VERSION` bumped to `1.3.0`

## [1.2.0] — 2026-06-28

### Added
- CLI: `thread messages X --limit N [--oldest]` flips default to DESC
  (newest first); `--oldest` restores ASC for legacy callers
- CLI: `search <query> [--thread] [--from] [--limit]` cross-thread FTS5
  search
- CLI: `react <msg_id> <emoji> [--remove|--list]`
- API: `GET /v1/search?q=` for the same
- API: `POST|DELETE|GET /v1/messages/<id>/reactions`
- `message_reactions` table (idempotent insert: same emoji twice = no-op)

### Fixed
- `thread_messages` previously returned the OLDEST N when `--limit N` was
  passed; now returns the NEWEST N by default (matches Slack/Mattermost
  UX). See the multi-agent-messaging skill → python-pitfalls.md #11.

## [1.0.0] — 2026-06-11

### Added
- Initial release: HTTP API + Python CLI + tiny Web UI + respond daemon
- Threads with multiple members, per-recipient read state
- v1 endpoints: `/v1/threads`, `/v1/threads/<id>/messages`,
  `/v1/threads/<id>/events` (later), `/v1/search`
- v0.1 pairwise-message endpoints kept for backward compatibility
- Bearer-token auth via `tokens.json`
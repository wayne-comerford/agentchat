# Changelog

All notable changes to agentchat are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/) and the project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
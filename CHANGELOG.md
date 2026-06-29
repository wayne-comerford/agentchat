# Changelog

All notable changes to agentchat are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/) and the project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
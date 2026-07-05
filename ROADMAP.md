# Roadmap

agentchat is currently at **v0.3.0** (Phase 3 — making it competitive). Below
is the planned trajectory. Items are not dates — they ship when they're ready
and don't break what's already working.

---

## v0.1.0 — Make it safe to publish  ✅ shipped 2026-06-29

- [x] Real auth: scrypt passwords + SHA256-hashed workspace tokens (login/refresh/revoke)
- [x] `LICENSE` (MIT), `SECURITY.md`, `.env.example`, `.gitignore`
- [x] `README.md` + `HANDOFF.md` for new peers
- [x] Mobile-first web UI with PWA install + offline shell
- [x] SSE streaming `/v1/threads/<id>/events` with 15s heartbeats
- [x] Reactions (emoji) with idempotent add/remove + batched list fetch
- [x] Cross-thread search (`/v1/search?q=...`)

## v0.2.0 — Make it credible  ✅ shipped 2026-06-29

Hardening pass before tagging a public release.

- [x] Per-IP rate limit on `/v1/auth/*` (10 req/min, in-memory token bucket)
- [x] CORS / Origin allowlist (empty = same-origin only; add prod domains)
- [x] Graceful shutdown on SIGTERM/SIGINT (drain SSE, close socket)
- [x] Log scrubber (Bearer tokens + passwords stripped before write)
- [x] `verify-roundtrip.sh` → **7/7** (register + login + threads + post +
      search + reactions + logout)
- [x] `Dockerfile` + `docker-compose.yml` (single image, no build tools)
- [x] `Caddyfile.example` with auto-TLS, SSE-friendly streaming
- [x] GitHub Actions CI (`verify-roundtrip` on every push to main)
- [x] SQLite WAL + `synchronous=NORMAL` (5-10× write speedup)
- [x] Per-thread membership gating on every content query (a caller only
      sees threads they belong to). Note: agents/threads/messages share one
      per-server namespace — this is membership scoping, not multi-tenant
      `workspace_id` row isolation. See SECURITY.md.

## v0.3.0 — Make it competitive  🚧 in progress

- [x] Forgot-password flow (`/v1/auth/forgot` + `/v1/auth/reset`; token
      delivered via server log in single-tenant mode)
- [x] Cookie session (Set-Cookie on login, browser auto-includes for SSE)
- [x] **MCP server** so any MCP-capable agent (Claude Desktop, Hermes,
      OpenClaw, Goose, …) uses agentchat as transport (stdio, JSON-RPC 2.0)
- [x] `pytest` suite (`tests/`) driving a real server over HTTP, replacing
      the smoke script for finer assertions
- [x] OpenAPI 3.1 spec (`openapi.yaml`) covering every `/v1` endpoint
- [x] Env-configurable rate limit (`LOGIN_RATE_LIMIT`) and token TTLs
      (`TOKEN_TTL_SECONDS`, `REFRESH_TTL_SECONDS`)
- [ ] Channels (multi-party), DMs (1:1) — first-class primitives
- [ ] File / image attachments with size + mime guards
- [ ] Webhook ingress (any service posts to agentchat)
- [ ] Streaming LLM responses via SSE on the daemon side
- [ ] Per-workspace model routing (swap LLM backends without code changes)
- [ ] Reaction / removal audit log (who did what when)
- [ ] Web UI accessibility pass (ARIA, focus traps, keyboard-only flows)
- [ ] Postgres migration path (SQLite → Postgres for production)
- [ ] Slack / Discord / Mattermost bridge (import + export)
- [ ] End-to-end message encryption (libsodium sealed boxes, key per
      workspace)

## v0.4.0 — Distribution

- [ ] Landing page (Docusaurus)
- [ ] Public demo deployment with seeded data
- [ ] Video walkthrough
- [ ] First public HN / r/LocalLLaMA / r/selfhosted post
- [ ] Homebrew / apt / nix packages
- [ ] Multi-arch container images (linux/amd64 + linux/arm64)

## v1.0.0 — Stability promise

- [ ] Semantic versioning commitment
- [ ] LTS branches (12 months security support per major)
- [ ] Deprecation policy published
- [ ] Governance doc (who decides what)
- [ ] First external co-maintainer

---

## Releases

| Version | Date       | Theme                          |
|---------|------------|--------------------------------|
| v0.1.0  | 2026-06-29 | Safe to publish                |
| v0.2.0  | 2026-06-29 | Make it credible (hardening)   |
| v0.3.0  | 2026-06-29 | Make it competitive (MCP, cookie session, reset) |
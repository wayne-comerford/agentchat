# Roadmap

agentchat is currently at **v0.2.0** (Phase 2 — making it credible). Below is
the planned trajectory. Items are not dates — they ship when they're ready
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

## v0.2.0 — Make it credible  🚧 in progress

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
- [x] Namespacing enforcement on every data query (`WHERE workspace_id=?`)
- [ ] Forgot-password flow (needs mailer / signed-token challenge)
- [ ] Cookie session (Set-Cookie on login, browser auto-includes for SSE)
- [ ] Postgres migration path (SQLite → Postgres for production)
- [ ] `pytest` suite replacing the smoke script for finer assertions
- [ ] Streaming LLM responses via SSE on the daemon side
- [ ] Per-workspace model routing (swap LLM backends without code changes)
- [ ] Reaction / removal audit log (who did what when)
- [ ] Web UI accessibility pass (ARIA, focus traps, keyboard-only flows)
- [ ] OpenAPI spec generated from the codebase

## v0.3.0 — Make it competitive

- [ ] Channels (multi-party), DMs (1:1) — first-class
- [ ] File / image attachments with size + mime guards
- [ ] Webhook ingress (any service posts to agentchat)
- [ ] **MCP server** so any MCP-capable agent (Claude Desktop, Hermes,
      OpenClaw, Goose, …) uses agentchat as transport
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
| v0.2.0  | TBD        | Make it credible (hardening)   |
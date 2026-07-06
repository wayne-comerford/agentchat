# Roadmap

agentchat is currently at **v1.1.1** (Phase 3 — making it competitive). Below
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

## v1.0.0 — Stability promise  ✅ shipped 2026-07-05

- [x] Semantic versioning commitment (1.x = stable)
- [x] LTS promise — single-namespace-per-server model locked in
- [x] `pyproject.toml` — `pip install agentchat` works
- [x] OpenAPI 3.1 spec complete and versioned with code
- [x] 34-test pytest suite passing + CI runs it on push

## v1.1.0 — Real-world integration  ✅ shipped 2026-07-05

- [x] **Webhook subscriptions** — external systems subscribe to
  `thread_create`, `message_post`, `react`; receive HMAC-SHA256-signed
  HTTP POSTs with exponential backoff (1s/5s/30s/5m/30m, 5 max)
- [x] Dedupe via `event_id` UUID v4 with UNIQUE constraint
- [x] 11 new webhook tests (full suite: 45/45)

## v1.1.1 — Storage  ✅ shipped 2026-07-06

- [x] **File uploads** with `multipart/form-data`, `sha256` dedupe,
  refcount-aware deletion
- [x] Local disk default; S3 swap-in via `AGENTCHAT_FILE_BACKEND=s3`
- [x] Size cap (25 MiB) + mime allowlist, both env-overridable
- [x] 11 new file tests (full suite: 56/56)

## v1.1.x — Next up (in progress)

- [ ] Audit log upgrade — structured viewer endpoint
- [ ] Channels (multi-party) and DMs (1:1) — first-class primitives
- [ ] Postgres migration path (`AGENTCHAT_DB_URL`)
- [ ] Multi-thread SSE event channel backpressure (1 consumer per thread)

## v1.2.x — Ecosystem

- [ ] Slack / Discord / Mattermost bridge (import + export)
- [ ] Streaming LLM responses via SSE on the daemon side
- [ ] Per-workspace model routing (swap LLM backends without code changes)
- [ ] End-to-end message encryption (libsodium sealed boxes, key per workspace)

## v1.3.x — Distribution & growth

- [ ] Landing page (Docusaurus)
- [ ] Public demo deployment with seeded data
- [ ] Video walkthrough
- [ ] First public HN / r/LocalLLaMA / r/selfhosted post
- [ ] Homebrew / apt / nix packages
- [ ] Multi-arch container images (linux/amd64 + linux/arm64)

## v2.0 — Multi-tenant
- [ ] Multi-tenant workspace model (cross-namespace isolation)
- [ ] First external co-maintainer
- [ ] Deprecation policy published
- [ ] Governance doc (who decides what)

---

## Releases

| Version | Date       | Theme                          |
|---------|------------|--------------------------------|
| v0.1.0  | 2026-06-29 | Safe to publish                |
| v0.2.0  | 2026-06-29 | Make it credible (hardening)   |
| v1.0.0  | 2026-07-05 | LTS promise — single-namespace-per-server locked |
| v1.1.0  | 2026-07-05 | Webhook ingress (HMAC-signed, retry+backoff, dedupe) + pip-installable |
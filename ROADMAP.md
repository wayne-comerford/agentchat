# Roadmap

agentchat is currently **pre-0.1.0** (Phase 1 in flight). Below is what we
plan to ship after that.

## v0.1.0 — Make it safe to publish  *(in progress)*
- [ ] Real auth: bcrypt passwords + scoped tokens (login/refresh/revoke)
- [ ] Namespacing: every query scoped to `workspace_id`
- [ ] `LICENSE` (MIT), `SECURITY.md`, `.env.example`, `.gitignore`
- [ ] Docker Compose deployment with Caddy for TLS
- [ ] `verify-roundtrip.sh` extended to 7/7 (auth roundtrip)
- [ ] GitHub Actions CI

## v0.2.0 — Make it credible
- [ ] Postgres migration path (SQLite → Postgres for production)
- [ ] `pytest` suite replacing the smoke script for finer assertions
- [ ] Streaming LLM responses via Server-Sent Events on the daemon side
- [ ] Per-workspace model routing (swap LLM backends without code changes)
- [ ] Reaction / removal audit log (who did what when)
- [ ] Per-IP login rate limiting (token bucket)
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

## Things we will NOT add

- Native mobile apps (web PWA covers phones; we're not Apple/Google)
- Federated protocol (Matrix / ActivityPub) — too much surface for the
  current maintainer capacity
- Group voice/video — use a dedicated tool (Whereby, Jitsi, etc.)

This list is a **direction**, not a contract. Priorities shift with
real-world feedback.
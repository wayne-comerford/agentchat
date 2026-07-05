# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.3.x   | :white_check_mark: |
| < 0.3   | :x:                |

## Reporting a Vulnerability

**DO NOT** open a public GitHub issue for security bugs.

Email: **wayne@comerford.dev** (or open a private security advisory via
GitHub → Security → Advisories → "New draft security advisory").

Please include:
- Description of the vulnerability
- Steps to reproduce (proof-of-concept preferred)
- Impact assessment (what an attacker gains)
- Your environment (agentchat version, Python version, deployment mode)

## Response Timeline

- **Initial response:** within 72 hours
- **Triage + impact assessment:** within 7 days
- **Patch release:** targeted within 30 days for high/critical; next minor
  release for medium/low

## Isolation model (what "scoping" means here)

agentchat is designed for a **single self-hosted server per trust domain**.
Users and workspaces exist for login and ownership, but agents, threads, and
messages live in **one namespace per server** — they do not carry a
`workspace_id`. Access to a thread's messages, reactions, and event stream is
gated by **membership** (`thread_members`): a caller only sees threads they
belong to, and non-members get 403/404.

Two consequences to be aware of when self-hosting:

- `GET /v1/peers` and `GET /v1/audit` are **server-wide** views (all agents /
  all thread metadata) available to any authenticated user. Run one server
  per group you want isolated rather than relying on multi-tenant separation.
- Thread membership is by agent name; anyone who can create a thread can add
  any known agent name to it.

## Scope

In scope:
- Authentication / authorization bypass (thread-membership gating, token forgery)
- SQL injection / path traversal
- Cross-site scripting (XSS) in the web UI
- Server-side request forgery (SSRF) in the daemon
- Information disclosure (tokens, passwords, message content)

Out of scope:
- Denial-of-service against the local server (it's self-hosted; the user
  can firewall)
- Issues in dependencies (report upstream)
- Social engineering

## Hardening Notes for Self-Hosters

- **Always run behind TLS** (Caddy / nginx / Traefik — see `docs/deployment/`).
  agentchat itself serves plain HTTP only.
- **Rotate tokens** after any suspected exposure. Tokens live in `tokens.json`
  for legacy `name:secret` peers; users created via `/v1/auth/register` get
  opaque tokens stored SHA-256-hashed in the `api_tokens` table, with
  passwords hashed via `hashlib.scrypt`.
- **Restrict AGENTCHAT_BIND** to `127.0.0.1` if you only intend to access via
  a reverse proxy or local tunnel. The default `0.0.0.0` binds on all
  interfaces.
- **Back up the SQLite DB** regularly; it's the only state.
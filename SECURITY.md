# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

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

## Scope

In scope:
- Authentication / authorization bypass (workspace isolation, token forgery)
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
  by default in v1.x; in v0.1+ they move to `api_tokens` table with bcrypt
  password auth.
- **Restrict AGENTCHAT_BIND** to `127.0.0.1` if you only intend to access via
  a reverse proxy or local tunnel. The default `0.0.0.0` binds on all
  interfaces.
- **Back up the SQLite DB** regularly; it's the only state.
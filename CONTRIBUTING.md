# Contributing to agentchat

Thanks for your interest. agentchat is a small, opinionated A2A chat tool;
the surface area is intentionally narrow so we can keep it correct.

## Development setup

```bash
git clone https://github.com/wayne-comerford/agentchat.git
cd agentchat
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run the API
AGENTCHAT_BIND=127.0.0.1 AGENTCHAT_PORT=7878 \
  python3 -m agentchat serve

# In another terminal: run the web UI
python3 web/server.py --port 7879 --api http://127.0.0.1:7878

# Open http://127.0.0.1:7879
```

## Tests

```bash
bash scripts/verify-roundtrip.sh   # end-to-end smoke (no deps)
pytest tests/                       # unit + integration tests
```

The smoke script exercises auth, threads, messages, search, and reactions
against a live server in 7 steps. It must stay green.

## Coding style

- Python 3.11+ stdlib-first; minimize third-party deps
- Black formatting, ruff lint (config coming)
- Type hints on all new public functions
- Docstrings on every module

## Commit format

[Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add workspace-scoped search
fix(api): 401 on missing bearer token now returns JSON, not 500
docs: document the Caddy deployment
refactor(db): split schema migration into per-table files
test: add workspace-isolation test
```

## Pull request process

1. Fork + branch from `main`
2. Keep PRs small (< 400 lines diff where possible)
3. Run `bash scripts/verify-roundtrip.sh` locally — must be 7/7 green
4. Run `pytest tests/` — must be green
5. Fill in the PR template (auto-loaded)
6. One approval from `@wayne-comerford` before merge (single-maintainer project)

## Reporting bugs

Use the **Bug Report** issue template. Include steps to reproduce + your
`agentchat --version` output and OS / Python version.

## Security issues

See `SECURITY.md` — **do not** open a public issue.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md) v2.1.

## License

By contributing, you agree your contributions are licensed under the
project's MIT license.
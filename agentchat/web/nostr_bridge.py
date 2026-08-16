"""
agentchat v1.2 — Nostr bridge server.

A small aiohttp HTTP/SSE server that:

- Loads a Nostr keypair (Hermes by default) for signing outbound messages
- Connects to one or more Nostr relays via our RelayPool
- Exposes a Slack-style UI shell (HTML) and JSON endpoints for the frontend
- Streams kind:9 (channel message) events to the browser via Server-Sent Events

Endpoints:
    GET  /                            — base HTML shell
    GET  /static/<path>               — CSS / JS / images
    GET  /v1/ui/channels              — JSON list of channels (from config)
    GET  /v1/ui/agents                — JSON list of agents (from registry)
    GET  /v1/ui/stream?channel=<id>   — SSE: kind:9 events for that channel
    POST /v1/ui/post                  — sign + publish kind:9
    GET  /health                      — liveness

Config:
    ~/.hermes/nostr/agentchat-bridge.yaml
        listen:    { host, port }
        relays:    [ "ws://...", ... ]
        identity:  { key_path, name }
        channels:  [ { id, name }, ... ]    # known channels to surface in UI

Run:
    .venv/bin/python -m agentchat.web.nostr_bridge
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from aiohttp import web
import aiohttp

# Make sibling agentchat package importable
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from agentchat.nostr.client import RelayPool  # noqa: E402
from agentchat.nostr.events import build_channel_message  # noqa: E402
from agentchat.nostr.keys import NostrKeys, load_keys  # noqa: E402
from agentchat import memory as memory_store  # noqa: E402

# Per-agent keypair registry (loaded lazily).
# Path is overridable via AGENTCHAT_NOSTR_DIR for tests.
def _identity_dir() -> Path:
    override = os.environ.get("AGENTCHAT_NOSTR_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes" / "nostr"


def _registry_path() -> Path:
    return _identity_dir() / "registry.json"


# Keep module-level aliases for backward compatibility — but resolve through
# the helper so tests can override AGENTCHAT_NOSTR_DIR.
IDENTITY_DIR = _identity_dir()
REGISTRY_PATH = _registry_path()


def _identity_path(name: str) -> Path:
    """Map agent short name -> nsec.json file path."""
    return _identity_dir() / f"{name}.nsec.json"


def load_identity(name: str) -> NostrKeys:
    """Load a keypair by short name (hermes/chappy/wayne-observer)."""
    p = _identity_path(name)
    if not p.exists():
        raise FileNotFoundError(f"identity not found: {p}")
    return load_keys(p)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nostr-bridge")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG = {
    "listen": {"host": "0.0.0.0", "port": 9877},
    "relays": ["ws://127.0.0.1:9876"],
    "identity": {
        "key_path": str(Path.home() / ".hermes" / "nostr" / "hermes.nsec.json"),
        "name": "hermes",
    },
    "channels": [
        {"id": "dinner-channel", "name": "# dinner"},
        {"id": "general", "name": "# general"},
    ],
}

CONFIG_PATH = Path.home() / ".hermes" / "nostr" / "agentchat-bridge.yaml"


def load_config() -> dict:
    """Load config from YAML if present, else return defaults."""
    if not CONFIG_PATH.exists():
        log.info("No config at %s — using defaults", CONFIG_PATH)
        return DEFAULT_CONFIG
    try:
        import yaml
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        log.info("Loaded config from %s", CONFIG_PATH)
        return {**DEFAULT_CONFIG, **(cfg or {})}
    except Exception as e:
        log.warning("Config load failed (%s) — using defaults", e)
        return DEFAULT_CONFIG


# --------------------------------------------------------------------------- #
# App state (per-process)
# --------------------------------------------------------------------------- #

class BridgeState:
    """Shared mutable state across requests."""

    def __init__(self, config: dict):
        self.config = config
        self.keys: NostrKeys | None = None
        self.pool: RelayPool | None = None
        self.registry: dict[str, dict] = {}  # name -> {npub, public_key_hex}

    async def startup(self) -> None:
        # Load identity
        # Config key_path first; fall back to AGENTCHAT_NOSTR_DIR override (tests).
        kp_path = Path(self.config["identity"]["key_path"]).expanduser()
        if not kp_path.exists():
            override = _identity_dir() / kp_path.name
            if override.exists():
                kp_path = override
        else:
            # If AGENTCHAT_NOSTR_DIR is set and points at a directory that has
            # the same key filename, prefer that path (lets tests inject
            # keys without monkeypatching the config).
            override_dir = _identity_dir()
            if override_dir != Path.home() / ".hermes" / "nostr":
                candidate = override_dir / kp_path.name
                if candidate.exists():
                    kp_path = candidate
        try:
            self.keys = load_keys(kp_path)
            log.info("Identity loaded: %s", self.keys.npub)
        except Exception as e:
            log.error("Failed to load keypair from %s: %s", kp_path, e)
            raise

        # Load agent registry
        registry_path = _registry_path()
        if registry_path.exists():
            try:
                with open(registry_path) as f:
                    self.registry = json.load(f)
                log.info("Loaded %d agents from registry", len(self.registry))
            except Exception as e:
                log.warning("Registry load failed: %s", e)

        # Build relay pool — eagerly construct so publishing works
        # without needing start_listen(). The WebSocket listener (Tornado
        # IOLoop) is opened on demand via /v1/ui/stream which falls back
        # to HTTP polling against the relay's /events endpoint.
        self.pool = RelayPool(
            relays=self.config["relays"],
            keys=self.keys,
        )
        log.info(
            "Pool initialized for %d relay(s) (publish ready; WS listen disabled "
            "due to aiohttp/Tornado loop conflict — Slice 1 polls /events instead)",
            len(self.config["relays"]),
        )

    async def shutdown(self) -> None:
        if self.pool is not None:
            self.pool.stop_listen()


STATE: BridgeState | None = None


def get_state(app: web.Application) -> BridgeState:
    return app["state"]  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

async def handle_index(request: web.Request) -> web.Response:
    """Render the base shell — Tailwind + HTMX + Alpine.js + our SSE wiring."""
    html_path = Path(__file__).parent / "templates" / "base.html"
    if not html_path.exists():
        return web.Response(text="base.html not found", status=500)
    return web.Response(
        text=html_path.read_text(),
        content_type="text/html",
    )


async def handle_settings(request: web.Request) -> web.Response:
    """Render the settings page (system config + agent management)."""
    html_path = Path(__file__).parent / "templates" / "settings.html"
    if not html_path.exists():
        return web.Response(text="settings.html not found", status=500)
    return web.Response(
        text=html_path.read_text(),
        content_type="text/html",
    )


async def handle_memory_list_sources(request: web.Request) -> web.Response:
    """GET /v1/ui/memory/sources — list recent snapshot dirs available to import.

    Returns one entry per snapshot under ``memory_store.archive_dir()`` so
    the /settings UI can populate a <select> for the "Import memories"
    dropdown.  Limited to the 50 most recent (newest first).
    """
    try:
        snaps = memory_store.list_snapshots()
    except Exception as e:
        log.warning("memory/sources failed: %s", e)
        return web.json_response({"error": str(e)}, status=500)

    out = []
    for p in reversed(snaps[-50:]):
        if not p.is_dir():
            continue
        # Heuristic: count how many MEMORY.md files are inside (one per agent).
        try:
            agent_count = sum(1 for _ in p.glob("agents/*/MEMORY.md"))
        except Exception:
            agent_count = 0
        out.append({
            "path": str(p),
            "name": p.name,
            "agents": agent_count,
            "mtime": p.stat().st_mtime,
        })
    return web.json_response({"sources": out, "root": str(memory_store.memory_root())})


async def handle_memory_import(request: web.Request) -> web.Response:
    """POST /v1/ui/memory/import — bootstrap a new agent by importing memories.

    Body: {
      "agent": "<target agent name>",
      "source": "<absolute path to a snapshot or export dir>",
      "mode": "merge" | "replace",        # default "merge"
      "create_if_missing": true|false,    # default false
      "no_archive": true|false            # default false (we snapshot first)
    }

    Auth: requires a logged-in session (any local agent) — same rule as
    /v1/ui/post.  We never let an anonymous request mutate memory state.
    """
    session_name = request.cookies.get(COOKIE_NAME)
    if not session_name:
        return web.json_response(
            {"error": "login required"}, status=401
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    agent = (body.get("agent") or "").strip()
    source = (body.get("source") or "").strip()
    mode = body.get("mode", "merge")
    create_if_missing = bool(body.get("create_if_missing", False))
    no_archive = bool(body.get("no_archive", False))

    if not agent:
        return web.json_response({"error": "agent required"}, status=400)
    if not source:
        return web.json_response({"error": "source required"}, status=400)
    if mode not in ("merge", "replace"):
        return web.json_response({"error": "mode must be merge or replace"}, status=400)

    src_path = Path(source)
    if not src_path.is_absolute():
        return web.json_response(
            {"error": "source must be an absolute path"}, status=400
        )
    if not src_path.exists() or not src_path.is_dir():
        return web.json_response(
            {"error": f"source not found: {source}"}, status=400
        )

    # Validate agent name (mirrors memory._validate_agent rules).
    if not agent.replace("-", "").replace("_", "").isalnum() or not agent:
        return web.json_response(
            {"error": f"invalid agent name: {agent!r}"}, status=400
        )

    target_path = memory_store.agent_memory_path(agent)
    archive_path = None
    try:
        if not target_path.exists():
            if not create_if_missing:
                return web.json_response(
                    {"error": f"target agent '{agent}' has no MEMORY.md — pass create_if_missing=true"},
                    status=400,
                )
            memory_store.write_agent(agent, f"# {agent} — Agent Memory\n")
        if not no_archive:
            archive_path = memory_store.snapshot(
                label=f"pre-import-{agent}"
            )
        summary = memory_store.import_memory(
            src_path, target_agent=agent, mode=mode
        )
    except Exception as e:
        log.warning("memory/import failed: agent=%s source=%s err=%s", agent, source, e)
        return web.json_response({"error": str(e)}, status=500)

    summary["target_agent"] = agent
    summary["mode"] = mode
    if archive_path is not None:
        summary["archive"] = str(archive_path)
    summary["imported_by"] = session_name
    log.info(
        "memory/import ok: agent=%s source=%s mode=%s by=%s files=%d",
        agent, source, mode, session_name, len(summary.get("files_imported", [])),
    )
    return web.json_response(summary)


async def handle_static(request: web.Request) -> web.Response:
    """Serve static files from agentchat/web/static/.

    Cache policy: never cache. JS/CSS change at every dev iteration and we
    never want a stale `app.js` to mask a fix (e.g. dev10's channel default).
    """
    path = request.match_info["path"]
    static_dir = Path(__file__).parent / "static"
    file_path = (static_dir / path).resolve()
    # Path traversal guard
    if not str(file_path).startswith(str(static_dir.resolve())):
        return web.Response(text="forbidden", status=403)
    if not file_path.is_file():
        return web.Response(text="not found", status=404)
    # Pick a content type based on extension
    ext = file_path.suffix.lower()
    if ext == ".js":
        ctype = "application/javascript"
    elif ext == ".css":
        ctype = "text/css"
    elif ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        ctype = f"image/{ext.lstrip('.')}"
    elif ext == ".svg":
        ctype = "image/svg+xml"
    else:
        ctype = "text/plain"
    return web.Response(
        body=file_path.read_bytes(),
        content_type=ctype,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


async def handle_channels(request: web.Request) -> web.Response:
    """JSON list of channels configured for the UI."""
    state = get_state(request.app)
    return web.json_response(state.config.get("channels", []))


async def handle_agents(request: web.Request) -> web.Response:
    """JSON list of agents from the registry, optionally filtered by prefix."""
    state = get_state(request.app)
    prefix = request.query.get("prefix", "").lower()
    out = []
    for name, info in state.registry.items():
        if prefix and prefix not in name.lower() and prefix not in info.get("npub", "").lower():
            continue
        out.append({
            "name": name,
            "npub": info.get("npub"),
            "public_key_hex": info.get("public_key_hex"),
        })
    return web.json_response(out)


async def handle_health(request: web.Request) -> web.Response:
    state = get_state(request.app)
    return web.json_response({
        "status": "ok",
        "identity": state.keys.npub if state.keys else None,
        "relays": state.config["relays"],
        "channels": [c["id"] for c in state.config.get("channels", [])],
        "agents_loaded": len(state.registry),
    })


async def handle_post(request: web.Request) -> web.Response:
    """
    Sign and publish a kind:9 message as the LOGGED-IN identity.

    Body: { "channel": "<id>", "content": "<text>", "mentions": ["<pubkey-hex>", ...] }

    The session cookie determines which keypair signs the event.  If no
    session is present the bridge falls back to its default identity (kept
    for backwards compat with the smoke tests).
    """
    state = get_state(request.app)
    if state.pool is None:
        return web.json_response({"error": "bridge not started"}, status=503)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    channel = body.get("channel", "").strip()
    content = body.get("content", "").strip()
    mentions = body.get("mentions") or []

    if not channel or not content:
        return web.json_response(
            {"error": "channel and content required"}, status=400
        )

    # Resolve signer from session.  No session → 401, never fall back to
    # bridge default identity (was a security hole: anyone on LAN could
    # post as Hermes by hitting /v1/ui/post without a cookie).
    session_name = request.cookies.get("agentchat_session")
    if not session_name:
        return web.json_response(
            {"error": "login required — POST /v1/auth/login first"},
            status=401,
        )

    try:
        signer = load_identity(session_name)
        log.info("POST signing as session=%s (npub=%s)", session_name, signer.npub)
    except Exception as e:
        log.warning("session=%s invalid (%s); rejecting POST", session_name, e)
        return web.json_response(
            {"error": "session invalid — POST /v1/auth/login first"},
            status=401,
        )

    if signer is None:
        return web.json_response({"error": "no signer available"}, status=503)

    # Build a per-signer pool so the right key signs the event.
    pool = RelayPool(relays=state.config["relays"], keys=signer)

    try:
        event_id = pool.publish_channel_message(
            channel_id=channel,
            content=content,
            mentions=mentions,
        )
        return web.json_response({
            "ok": True,
            "event_id": event_id,
            "channel": channel,
            "signed_by": signer.npub,
        })
    except Exception as e:
        log.warning("publish failed: %s", e)
        return web.json_response({"error": str(e)}, status=500)


# --------------------------------------------------------------------------- #
# Auth (login / logout / whoami)
# --------------------------------------------------------------------------- #

COOKIE_NAME = "agentchat_session"
COOKIE_MAX_AGE = 60 * 60 * 8  # 8 hours


async def handle_login(request: web.Request) -> web.Response:
    """
    POST /v1/auth/login   { "name": "hermes" | "chappy" | "wayne-observer" }

    Loads the keypair, sets a session cookie, returns the npub.  Anyone on
    the LAN can log in as any local agent — this is a developer-facing
    bridge, not a production auth system.  Add a real auth layer before
    exposing this publicly.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    name = (body.get("name") or "").strip()
    if not name:
        return web.json_response({"error": "name required"}, status=400)

    try:
        kp = load_identity(name)
    except FileNotFoundError as e:
        return web.json_response({"error": str(e)}, status=404)

    resp = web.json_response({
        "ok": True,
        "name": name,
        "npub": kp.npub,
        "public_key_hex": kp.public_key_hex,
    })
    resp.set_cookie(
        COOKIE_NAME,
        name,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    log.info("login: %s -> %s", name, kp.npub)
    return resp


async def handle_logout(request: web.Request) -> web.Response:
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE_NAME, path="/")
    return resp


async def handle_whoami(request: web.Request) -> web.Response:
    """GET /v1/auth/whoami — name + npub from session cookie (or anonymous)."""
    state = get_state(request.app)
    name = request.cookies.get(COOKIE_NAME)
    if not name:
        return web.json_response({
            "logged_in": False,
            "default_identity": state.keys.npub if state.keys else None,
        })
    try:
        kp = load_identity(name)
        return web.json_response({
            "logged_in": True,
            "name": name,
            "npub": kp.npub,
            "public_key_hex": kp.public_key_hex,
        })
    except Exception as e:
        return web.json_response({"logged_in": False, "error": str(e)})


async def handle_identities(request: web.Request) -> web.Response:
    """GET /v1/auth/identities — list of available identities for the login UI."""
    state = get_state(request.app)
    out = []
    for name, info in state.registry.items():
        out.append({
            "name": name,
            "npub": info.get("npub"),
            "public_key_hex": info.get("public_key_hex"),
        })
    return web.json_response(out)


# --------------------------------------------------------------------------- #
# SSE bridge
# --------------------------------------------------------------------------- #

@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Permissive CORS for local dev (Slice 1 only — tighten before deploy)."""
    if request.method == "OPTIONS":
        return web.Response(status=204)
    resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


async def handle_stream(request: web.Request) -> web.StreamResponse:
    """
    SSE stream: pushes kind:9 events for one channel.

    GET /v1/ui/stream?channel=<id>&since=<unix-ts>

    Implementation note: we poll the relay's /events HTTP endpoint rather than
    opening a pynostr WebSocket subscription. Reason: pynostr's Tornado IOLoop
    conflicts with aiohttp's asyncio loop, causing open_connections() to fail.
    Polling works against our own echo relay perfectly; for real Buzz relays
    we'll need a proper WS bridge (Slice 1.5 work).

    Filters: only events where kind==9 AND the #h tag equals the channel id.
    """
    state = get_state(request.app)
    channel = request.query.get("channel", "").strip()
    if not channel:
        return web.json_response({"error": "channel required"}, status=400)

    relay_http = _relay_http_from_ws(state.config["relays"][0])
    if relay_http is None:
        return web.json_response({"error": "relay not configured"}, status=503)

    resp = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)

    # Start from current time (only show new events, not history)
    seen_ids: set[str] = set()
    last_id: str | None = None

    await resp.write(b"event: connected\ndata: " + json.dumps({
        "channel": channel,
        "poll_interval_ms": POLL_INTERVAL_MS,
    }).encode() + b"\n\n")

    async with aiohttp.ClientSession() as session:
        try:
            while True:
                try:
                    async with session.get(
                        f"{relay_http}/events", timeout=aiohttp.ClientTimeout(total=5)
                    ) as r:
                        r.raise_for_status()
                        events = await r.json(content_type=None)
                except Exception as e:
                    log.debug("poll failed: %s", e)
                    events = []

                # Filter + emit new events
                for ev in events:
                    eid = ev.get("id", "")
                    if eid in seen_ids:
                        continue
                    if ev.get("kind") != 9:
                        continue
                    # Check #h tag matches channel
                    if not _event_in_channel(ev, channel):
                        continue
                    seen_ids.add(eid)
                    last_id = eid
                    payload = {
                        "event_id": eid,
                        "kind": ev.get("kind"),
                        "pubkey": ev.get("pubkey"),
                        "created_at": ev.get("created_at"),
                        "content": ev.get("content", ""),
                        "tags": ev.get("tags", []),
                    }
                    await resp.write(
                        b"event: message\ndata: "
                        + json.dumps(payload).encode()
                        + b"\n\n"
                    )

                # Heartbeat to keep the connection alive
                await resp.write(b": heartbeat\n\n")
                await asyncio.sleep(POLL_INTERVAL_MS / 1000)
        except (ConnectionResetError, asyncio.CancelledError):
            pass

    return resp


def _relay_http_from_ws(ws_url: str) -> str | None:
    """Convert ws://host:port or wss://host:port to http://host:port."""
    if ws_url.startswith("ws://"):
        return "http://" + ws_url[len("ws://"):]
    if ws_url.startswith("wss://"):
        return "https://" + ws_url[len("wss://"):]
    return None


def _event_in_channel(ev: dict, channel_id: str) -> bool:
    """Check if a Nostr event has #h=channel_id tag."""
    for tag in ev.get("tags") or []:
        if isinstance(tag, list) and len(tag) >= 2:
            if tag[0] == "h" and tag[1] == channel_id:
                return True
    return False


# Polling cadence for the SSE bridge
POLL_INTERVAL_MS = 1500


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #

def make_app(config: dict) -> web.Application:
    state = BridgeState(config)

    async def on_startup(app: web.Application):
        await state.startup()
        app["state"] = state

    async def on_cleanup(app: web.Application):
        await state.shutdown()

    app = web.Application(
        middlewares=[cors_middleware],
        client_max_size=1024 * 1024,
    )
    # Attach state eagerly so tests can patch it before the first request.
    app["state"] = state
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/", handle_index)
    app.router.add_get("/settings", handle_settings)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/static/{path:.*}", handle_static)
    app.router.add_get("/v1/ui/channels", handle_channels)
    app.router.add_get("/v1/ui/agents", handle_agents)
    app.router.add_get("/v1/ui/stream", handle_stream)
    app.router.add_get("/v1/ui/memory/sources", handle_memory_list_sources)
    app.router.add_post("/v1/ui/memory/import", handle_memory_import)
    app.router.add_post("/v1/ui/post", handle_post)
    app.router.add_post("/v1/auth/login", handle_login)
    app.router.add_post("/v1/auth/logout", handle_logout)
    app.router.add_get("/v1/auth/whoami", handle_whoami)
    app.router.add_get("/v1/auth/identities", handle_identities)
    return app


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="agentchat v1.2 Nostr bridge")
    parser.add_argument("--host", default=None, help="override listen host")
    parser.add_argument("--port", type=int, default=None, help="override listen port")
    parser.add_argument("--config", default=None, help="path to YAML config")
    args = parser.parse_args()

    config = load_config()
    if args.host:
        config["listen"]["host"] = args.host
    if args.port:
        config["listen"]["port"] = args.port
    if args.config:
        global CONFIG_PATH
        CONFIG_PATH = Path(args.config)

    host = config["listen"]["host"]
    port = config["listen"]["port"]

    app = make_app(config)

    log.info("Bridge listening on http://%s:%d", host, port)
    log.info("Health:  http://%s:%d/health", host, port)
    log.info("UI:      http://%s:%d/", host, port)

    web.run_app(app, host=host, port=port, print=lambda *a, **kw: None)


if __name__ == "__main__":
    main()
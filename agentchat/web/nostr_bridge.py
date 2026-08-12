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
    "listen": {"host": "127.0.0.1", "port": 9877},
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
        kp_path = Path(self.config["identity"]["key_path"]).expanduser()
        try:
            self.keys = load_keys(kp_path)
            log.info("Identity loaded: %s", self.keys.npub)
        except Exception as e:
            log.error("Failed to load keypair from %s: %s", kp_path, e)
            raise

        # Load agent registry
        registry_path = Path.home() / ".hermes" / "nostr" / "registry.json"
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


async def handle_static(request: web.Request) -> web.Response:
    """Serve static files from agentchat/web/static/."""
    path = request.match_info["path"]
    static_dir = Path(__file__).parent / "static"
    file_path = (static_dir / path).resolve()
    # Path traversal guard
    if not str(file_path).startswith(str(static_dir.resolve())):
        return web.Response(text="forbidden", status=403)
    if not file_path.is_file():
        return web.Response(text="not found", status=404)
    return web.Response(text=file_path.read_text(), content_type="text/plain")


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
    Sign and publish a kind:9 message.

    Body: { "channel": "<id>", "content": "<text>", "mentions": ["<pubkey-hex>", ...] }
    """
    state = get_state(request.app)
    if state.pool is None or state.keys is None:
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

    try:
        event_id = state.pool.publish_channel_message(
            channel_id=channel,
            content=content,
            mentions=mentions,
        )
        return web.json_response({
            "ok": True,
            "event_id": event_id,
            "channel": channel,
        })
    except Exception as e:
        log.warning("publish failed: %s", e)
        return web.json_response({"error": str(e)}, status=500)


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
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/static/{path:.*}", handle_static)
    app.router.add_get("/v1/ui/channels", handle_channels)
    app.router.add_get("/v1/ui/agents", handle_agents)
    app.router.add_get("/v1/ui/stream", handle_stream)
    app.router.add_post("/v1/ui/post", handle_post)
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
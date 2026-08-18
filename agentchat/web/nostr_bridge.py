"""
agentchat v1.2 — Nostr bridge server.

A small aiohttp HTTP/SSE server that:

- Loads a Nostr keypair (Hermes by default) for signing outbound messages
- Connects to one or more Nostr relays via our RelayPool
- Exposes a Slack-style UI shell (HTML) and JSON endpoints for the frontend
- Streams kind:9 (channel message) events to the browser via Server-Sent Events

Endpoints:
    GET  /                            — base HTML shell
    GET  /settings                    — settings page
    GET  /static/<path>               — CSS / JS / images
    GET  /v1/ui/channels              — JSON list of channels (from config)
    GET  /v1/ui/agents                — JSON list of agents (with status_entry)
    GET  /v1/ui/focus                 — JSON focus_map (agent -> channel)
    POST /v1/ui/focus                 — pin/clear focused channel (session-auth)
    GET  /v1/ui/stream?channel=<id>   — SSE: kind:9 events for that channel
                                       (special: channel=agent_status → liveness stream)
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
import time
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
from agentchat import memory_import  # noqa: E402

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

    # SSE subscribers for the agent_status channel.
    # Each entry is an asyncio.Queue; subscribers consume from it and emit SSE.
    agent_status_subs: list[asyncio.Queue] = []
    # Per-agent focus: name -> {"channel": str, "since": float}
    focus_map: dict[str, dict] = {}
    # Per-agent liveness: name -> {"status": "active"|"idle"|"disconnected",
    #                              "last_activity_ts": float,
    #                              "focused_channel": str | None,
    #                              "last_message": str | None}
    agent_status: dict[str, dict] = {}
    IDLE_AFTER_SECONDS = 120  # if no activity for this long → idle
    DISCONNECTED_AFTER_SECONDS = 600  # if no activity → disconnected

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


# --------------------------------------------------------------------------- #
# Agent liveness + focus helpers (shared by routes)
# --------------------------------------------------------------------------- #


def record_activity(agent_name: str, channel: str | None = None,
                    last_message: str | None = None) -> None:
    """Mark an agent as active right now; bumps last_activity_ts and last_message."""
    truncated = last_message[:120] if last_message is not None else None
    if agent_name not in BridgeState.agent_status:
        BridgeState.agent_status[agent_name] = {
            "status": "active",
            "last_activity_ts": time.time(),
            "focused_channel": channel,
            "last_message": truncated,
        }
    else:
        s = BridgeState.agent_status[agent_name]
        s["status"] = "active"
        s["last_activity_ts"] = time.time()
        if channel is not None:
            s["focused_channel"] = channel
        if last_message is not None:
            s["last_message"] = truncated
    # Broadcast to SSE subscribers (best-effort).
    payload = {
        "type": "agent_status",
        "agent": agent_name,
        "state": BridgeState.agent_status[agent_name],
    }
    for q in list(BridgeState.agent_status_subs):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


def set_focus(agent_name: str, channel: str | None) -> None:
    """Pin or clear an agent's focused channel. None → clear."""
    if channel is None:
        BridgeState.focus_map.pop(agent_name, None)
        if agent_name in BridgeState.agent_status:
            BridgeState.agent_status[agent_name]["focused_channel"] = None
    else:
        BridgeState.focus_map[agent_name] = {
            "channel": channel,
            "since": time.time(),
        }
        if agent_name not in BridgeState.agent_status:
            BridgeState.agent_status[agent_name] = {
                "status": "idle",
                "last_activity_ts": 0.0,
                "focused_channel": channel,
                "last_message": None,
            }
        else:
            BridgeState.agent_status[agent_name]["focused_channel"] = channel
    payload = {
        "type": "focus",
        "agent": agent_name,
        "channel": channel,
    }
    for q in list(BridgeState.agent_status_subs):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


def compute_status(agent_state: dict) -> str:
    """Recompute status string from last_activity_ts."""
    age = time.time() - agent_state.get("last_activity_ts", 0.0)
    if age > BridgeState.DISCONNECTED_AFTER_SECONDS:
        return "disconnected"
    if age > BridgeState.IDLE_AFTER_SECONDS:
        return "idle"
    return "active"


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


# --------------------------------------------------------------------------- #
# Memory import — single-file (paste / upload) + atomic agent create
# (Powers the Add Agent wizard. See kanban t_20f29edb.)
# --------------------------------------------------------------------------- #

def _save_registry(state: "BridgeState") -> None:
    """Atomically persist the in-memory agent registry to disk.

    Writes to ``<registry>.tmp`` then ``os.replace`` so a crash mid-write
    can't corrupt the live registry.
    """
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state.registry, f, indent=2, sort_keys=True)
    os.replace(tmp, p)


async def handle_memory_preview(request: web.Request) -> web.Response:
    """GET /v1/ui/memory/preview — parse a memory.md body and return counts.

    Read-only, no auth required (parsing is a pure function on the input
    text; nothing is written). Used by the Add Agent modal to show
    live preview as the user types.

    Body: {"memory_md": "..."}    OR    raw text body (Content-Type:
    text/markdown).

    Response: 200 with :func:`memory_import.parse_memory_md` result.
    """
    ctype = (request.headers.get("Content-Type") or "").split(";")[0].strip()
    if ctype == "application/json":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        text = (body or {}).get("memory_md", "")
    else:
        # text/markdown or application/octet-stream
        text = await request.text()

    if not isinstance(text, str):
        return web.json_response({"error": "memory_md must be a string"}, status=400)
    if len(text.encode("utf-8")) > memory_import.MAX_UPLOAD_BYTES:
        return web.json_response(
            {"error": f"input exceeds {memory_import.MAX_UPLOAD_BYTES} bytes"},
            status=413,
        )

    parsed = memory_import.parse_memory_md(text)
    return web.json_response(parsed.to_dict())


async def handle_agent_import_memory(request: web.Request) -> web.Response:
    """POST /v1/ui/agents/import-memory — replace an existing agent's memory.

    Two content types:

      * ``application/json`` — body has ``{"agent": "<name>", "memory_md": "..."}``.
        Capped at 64 KiB. Used by the inline paste in the standalone
        "Import memory" button on each agent card.
      * ``multipart/form-data`` — fields ``agent`` (str) + ``file``
        (uploaded .md). Capped at 256 KiB. Used by the upload flow.

    Auth: session required. Any logged-in local user can import memory
    into any agent — workspace ACLs (v1.3.0) will tighten this.

    Response: 200 with :func:`memory_import.import_text` /
    :func:`memory_import.import_file` result.
    """
    session_name = request.cookies.get(COOKIE_NAME)
    if not session_name:
        return web.json_response({"error": "login required"}, status=401)

    ctype = (request.headers.get("Content-Type") or "").split(";")[0].strip()
    state = get_state(request.app)

    if ctype == "application/json":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        agent = (body or {}).get("agent", "").strip()
        text = (body or {}).get("memory_md", "")
        if not agent:
            return web.json_response({"error": "agent required"}, status=400)
        if not text:
            return web.json_response({"error": "memory_md required"}, status=400)
        try:
            result = memory_import.import_text(agent, text)
        except memory_import.OversizeInput as e:
            return web.json_response(
                {"error": str(e), "size": e.size, "cap": e.cap}, status=413
            )
        except memory_import.InvalidAgentName as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            log.warning("agent/import-memory failed: agent=%s err=%s", agent, e)
            return web.json_response({"error": str(e)}, status=500)
    elif ctype.startswith("multipart/"):
        try:
            form = await request.post()
        except Exception as e:
            return web.json_response({"error": f"invalid multipart: {e}"}, status=400)
        # aiohttp returns MultiDict; .get returns str | FileField.
        raw_agent = form.get("agent") if form is not None else None
        agent = (str(raw_agent) if raw_agent is not None else "").strip()
        if not agent:
            return web.json_response({"error": "agent required"}, status=400)
        file_field = form.get("file") if form is not None else None
        # aiohttp's FileField has a .file attribute; plain str fields don't.
        if file_field is None or not hasattr(file_field, "file"):
            return web.json_response(
                {"error": "file field required (multipart)"}, status=400
            )
        # Read the upload into a temp file so we can stream it through
        # import_file() with the size cap check.
        import tempfile
        with tempfile.NamedTemporaryFile(
            prefix="agentchat-memimport-",
            suffix=".md",
            delete=False,
        ) as tmp:
            tmp.write(file_field.file.read())
            tmp_path = Path(tmp.name)
        try:
            result = memory_import.import_file(agent, tmp_path)
        except memory_import.OversizeInput as e:
            return web.json_response(
                {"error": str(e), "size": e.size, "cap": e.cap}, status=413
            )
        except memory_import.InvalidAgentName as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            log.warning("agent/import-memory upload failed: agent=%s err=%s", agent, e)
            return web.json_response({"error": str(e)}, status=500)
        finally:
            tmp_path.unlink(missing_ok=True)
    else:
        return web.json_response(
            {"error": "Content-Type must be application/json or multipart/form-data"},
            status=415,
        )

    log.info(
        "agent/import-memory ok: agent=%s sections=%d lines=%d by=%s",
        result.agent, result.sections_imported, result.lines_imported, session_name,
    )
    return web.json_response(result.to_dict())


async def handle_create_agent(request: web.Request) -> web.Response:
    """POST /v1/ui/agents — create a new agent (and optionally import memory).

    Body: {
      "name": "<agent name>",                # required, alphanumeric + -_
      "npub": "npub1...",                    # optional
      "public_key_hex": "<64 hex chars>",    # optional
      "color": "#a78bfa",                    # optional
      "role": "member" | "admin",            # optional, default member
      "memory_md": "## Identity\\n- ...",    # optional; atomic with create
      "source_ecosystem": "local" | "..."    # optional; metadata for v1.3
    }

    Auth: session required. Atomic — if memory import fails, the agent
    is NOT created and the registry is unchanged.

    Response 200: {"ok": true, "agent": {...}, "memory": {...}}
    Response 400: validation
    Response 401: no session
    Response 409: name already exists
    Response 413: memory_md too large
    """
    session_name = request.cookies.get(COOKIE_NAME)
    if not session_name:
        return web.json_response({"error": "login required"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    name = (body.get("name") or "").strip()
    npub = (body.get("npub") or "").strip() or None
    public_key_hex = (body.get("public_key_hex") or "").strip() or None
    color = (body.get("color") or "").strip() or None
    role = (body.get("role") or "member").strip()
    memory_md = body.get("memory_md")
    source_ecosystem = (body.get("source_ecosystem") or "local").strip()

    if not name:
        return web.json_response({"error": "name required"}, status=400)
    if role not in ("member", "admin", "observer"):
        return web.json_response(
            {"error": "role must be member|admin|observer"}, status=400
        )

    state = get_state(request.app)
    if name in state.registry:
        return web.json_response(
            {"error": f"agent {name!r} already exists"}, status=409
        )

    # Validate the memory before mutating anything so we can fail fast
    # without rolling back. Memory text is optional — None means "no
    # memory yet" (the Add Agent modal's first radio option).
    import_result: dict | None = None
    if memory_md is not None:
        if not isinstance(memory_md, str):
            return web.json_response(
                {"error": "memory_md must be a string"}, status=400
            )
        try:
            import_result = memory_import.import_text(name, memory_md).to_dict()
        except memory_import.OversizeInput as e:
            return web.json_response(
                {"error": str(e), "size": e.size, "cap": e.cap}, status=413
            )
        except memory_import.InvalidAgentName as e:
            return web.json_response({"error": str(e)}, status=400)

    # Build the registry entry. We don't have the agent's real
    # secret key on this side (the agent's own daemon does), so we
    # only persist what's discoverable from the public side: name,
    # public npub, public key hex, color, role, and the metadata
    # tags needed for the federation vision.
    entry: dict[str, Any] = {
        "npub": npub,
        "public_key_hex": public_key_hex,
        "color": color,
        "role": role,
        "source_ecosystem": source_ecosystem,
        "added_at": int(time.time()),
        "added_by": session_name,
    }
    # Drop None values so the on-disk registry stays minimal.
    entry = {k: v for k, v in entry.items() if v is not None}

    # Atomic: write the registry entry + (optionally) the memory.
    # Memory has already been written by import_text() above if it
    # was provided. Registry is the only remaining mutation.
    state.registry[name] = entry
    try:
        _save_registry(state)
    except Exception as e:
        # Roll back memory if we wrote it.
        if import_result is not None:
            try:
                target = memory_store.agent_memory_path(name)
                if target.exists():
                    target.unlink()
            except Exception:
                pass
        state.registry.pop(name, None)
        log.error("agent create: registry save failed: %s", e)
        return web.json_response(
            {"error": f"failed to persist registry: {e}"}, status=500
        )

    log.info(
        "agent create ok: name=%s role=%s by=%s memory=%s",
        name, role, session_name, "yes" if import_result else "no",
    )
    # Build the response in the same shape as GET /v1/ui/agents.
    response_entry = {
        "name": name,
        "npub": entry.get("npub"),
        "public_key_hex": entry.get("public_key_hex"),
        "status_entry": None,
        "role": entry.get("role"),
        "color": entry.get("color"),
        "source_ecosystem": entry.get("source_ecosystem"),
        "added_at": entry.get("added_at"),
    }
    return web.json_response({
        "ok": True,
        "agent": response_entry,
        "memory": import_result,
    })


# --------------------------------------------------------------------------- #
# Memory Transparency — per-agent structured read/edit endpoints
# (powers the right-side Memories drawer in the chat UI)
# --------------------------------------------------------------------------- #

async def handle_memory_list_agents(request: web.Request) -> web.Response:
    """GET /v1/ui/memory/agents — all agents + structured sections.

    Returns::

        {
          "agents": [
            {"name": "hermes", "sections": [{"title": "Prefs", "lines": [...], "index": 1}, ...]},
            ...
          ]
        }

    Public read access — anyone can see what an agent remembers.  This
    matches the existing chat model (channels + messages are also public).
    """
    agents_out: list[dict[str, Any]] = []
    for name in memory_store.list_agents():
        try:
            sections = memory_store.list_agent_sections(name)
        except Exception as e:
            log.warning("memory sections read failed for %s: %s", name, e)
            sections = []
        agents_out.append({"name": name, "sections": sections})
    return web.json_response({"agents": agents_out})


async def handle_memory_get_agent(request: web.Request) -> web.Response:
    """GET /v1/ui/memory/agents/{name} — single agent's structured sections."""
    name = request.match_info["name"]
    try:
        memory_store._validate_agent(name)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    try:
        sections = memory_store.list_agent_sections(name)
    except FileNotFoundError:
        return web.json_response({"error": f"agent not found: {name}"}, status=404)
    return web.json_response({"name": name, "sections": sections})


async def handle_memory_append_line(request: web.Request) -> web.Response:
    """POST /v1/ui/memory/agents/{name}/sections/{section}/lines

    Body: ``{"line": "<text>"}``.  Appends one line to the named section
    (creates the section if missing).  Session-required.
    """
    name = request.match_info["name"]
    section = request.match_info["section"]
    if not request.cookies.get(COOKIE_NAME):
        return web.json_response({"error": "login required"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    line = (body.get("line") or "").strip()
    if not line:
        return web.json_response({"error": "line required"}, status=400)
    try:
        memory_store._validate_agent(name)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    try:
        memory_store.append_agent(name, section, line)
    except FileNotFoundError:
        memory_store.write_agent(name, f"# {name} — Agent Memory\n")
        memory_store.append_agent(name, section, line)
    return web.json_response({"ok": True, "name": name, "section": section, "line": line})


async def handle_memory_delete_line(request: web.Request) -> web.Response:
    """DELETE /v1/ui/memory/agents/{name}/sections/{section}/lines/{idx}

    Removes one line by 0-based index.  Session-required.
    """
    name = request.match_info["name"]
    section = request.match_info["section"]
    idx_str = request.match_info["idx"]
    if not request.cookies.get(COOKIE_NAME):
        return web.json_response({"error": "login required"}, status=401)
    try:
        idx = int(idx_str)
    except ValueError:
        return web.json_response({"error": "idx must be integer"}, status=400)
    try:
        memory_store._validate_agent(name)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    removed = memory_store.remove_agent_line(name, section, idx)
    if not removed:
        return web.json_response({"error": "section/line not found"}, status=404)
    return web.json_response({"ok": True, "name": name, "section": section, "removed_index": idx})


async def handle_memory_replace_section(request: web.Request) -> web.Response:
    """PUT /v1/ui/memory/agents/{name}/sections/{section}

    Body: ``{"lines": ["<line1>", "<line2>", ...]}``.  Replaces the entire
    body of a section.  Creates the section if missing.  Session-required.
    """
    name = request.match_info["name"]
    section = request.match_info["section"]
    if not request.cookies.get(COOKIE_NAME):
        return web.json_response({"error": "login required"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    lines = body.get("lines")
    if not isinstance(lines, list):
        return web.json_response({"error": "lines must be a list"}, status=400)
    lines = [str(ln) for ln in lines]
    try:
        memory_store._validate_agent(name)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    try:
        memory_store.replace_agent_section(name, section, lines)
    except FileNotFoundError:
        memory_store.write_agent(name, f"# {name} — Agent Memory\n")
        memory_store.replace_agent_section(name, section, lines)
    return web.json_response({"ok": True, "name": name, "section": section, "lines": lines})


# --------------------------------------------------------------------------- #
# Shared team memory — used by all agents; supports R/W from any session
# --------------------------------------------------------------------------- #


async def handle_memory_shared_get(request: web.Request) -> web.Response:
    """GET /v1/ui/memory/shared

    Returns the team shared memory as a list of sections (same shape as
    ``/v1/ui/memory/agents``). Read is unauthenticated; write is session-gated.
    """
    sections = memory_store.list_team_sections()
    raw = memory_store.read_team()
    return web.json_response({
        "sections": sections,
        "raw": raw,
        "path": str(memory_store.team_shared_path()),
    })


async def handle_memory_shared_replace(request: web.Request) -> web.Response:
    """PUT /v1/ui/memory/shared/sections/{section}

    Body: ``{"lines": ["<line1>", "<line2>", ...]}``. Replaces the body
    of a shared section. Creates the section if missing. Session-required
    (any logged-in agent can write — there is no per-agent ownership on
    shared memory; conflicts are resolved by flock + last-writer-wins on
    a per-section basis).
    """
    section = request.match_info["section"]
    session_name = request.cookies.get(COOKIE_NAME)
    if not session_name:
        return web.json_response({"error": "login required"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    lines = body.get("lines")
    if not isinstance(lines, list):
        return web.json_response({"error": "lines must be a list"}, status=400)
    lines = [str(ln) for ln in lines]
    try:
        memory_store.replace_team_section(section, lines)
    except Exception as e:
        return web.json_response({"error": f"write failed: {e}"}, status=500)
    return web.json_response({
        "ok": True, "section": section, "lines": lines, "by": session_name,
    })


async def handle_memory_shared_append(request: web.Request) -> web.Response:
    """POST /v1/ui/memory/shared/sections/{section}/lines

    Body: ``{"line": "<text>"}``. Appends a single attributed line under
    a shared section. Session-required.
    """
    section = request.match_info["section"]
    session_name = request.cookies.get(COOKIE_NAME)
    if not session_name:
        return web.json_response({"error": "login required"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    line = (body.get("line") or "").strip()
    if not line:
        return web.json_response({"error": "line required"}, status=400)
    try:
        memory_store.append_team_line(section, line, author=session_name)
    except Exception as e:
        return web.json_response({"error": f"append failed: {e}"}, status=500)
    return web.json_response({
        "ok": True, "section": section, "line": line, "by": session_name,
    })


async def handle_memory_shared_delete_line(request: web.Request) -> web.Response:
    """DELETE /v1/ui/memory/shared/sections/{section}/lines/{idx}

    Removes one line (by 0-based index) from a shared section. Returns
    404 if the section/line doesn't exist, 401 if no session.
    """
    section = request.match_info["section"]
    idx = int(request.match_info["idx"])
    session_name = request.cookies.get(COOKIE_NAME)
    if not session_name:
        return web.json_response({"error": "login required"}, status=401)
    try:
        removed = memory_store.remove_team_line(section, idx)
    except Exception as e:
        return web.json_response({"error": f"delete failed: {e}"}, status=500)
    if not removed:
        return web.json_response(
            {"error": f"line {idx} not found in section '{section}'"},
            status=404,
        )
    return web.json_response({"ok": True, "section": section, "idx": idx, "by": session_name})


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
    """JSON list of agents from the registry, optionally filtered by prefix.

    Each entry includes the live status (active/idle/disconnected) and the
    currently focused channel (if any). Status is recomputed from
    last_activity_ts on every read so the sidebar is always fresh.
    """
    state = get_state(request.app)
    prefix = request.query.get("prefix", "").lower()
    out = []
    for name, info in state.registry.items():
        if prefix and prefix not in name.lower() and prefix not in info.get("npub", "").lower():
            continue
        status_entry = BridgeState.agent_status.get(name)
        if status_entry:
            computed = compute_status(status_entry)
            status_entry = {
                **status_entry,
                "status": computed,
                "age_seconds": round(time.time() - status_entry["last_activity_ts"], 1),
            }
        out.append({
            "name": name,
            "npub": info.get("npub"),
            "public_key_hex": info.get("public_key_hex"),
            "status_entry": status_entry,
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


async def handle_focus_get(request: web.Request) -> web.Response:
    """Return the current focus_map (agent -> channel)."""
    return web.json_response(BridgeState.focus_map)


async def handle_focus_post(request: web.Request) -> web.Response:
    """Pin or clear an agent's focused channel.

    Body: {"agent": "<name>", "channel": "<id>" | null}

    Returns 400 on missing/invalid fields, 401 if no session (so the
    focus can't be hijacked over LAN).
    """
    session_name = request.cookies.get("agentchat_session")
    if not session_name:
        return web.json_response(
            {"error": "login required — POST /v1/auth/login first"},
            status=401,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    agent = (body.get("agent") or "").strip()
    channel = body.get("channel")
    if channel is not None:
        channel = str(channel).strip() or None
    if not agent:
        return web.json_response({"error": "agent required"}, status=400)
    state = get_state(request.app)
    if agent not in state.registry and agent != session_name:
        return web.json_response(
            {"error": f"unknown agent: {agent}"}, status=400
        )
    set_focus(agent, channel)
    return web.json_response({"ok": True, "agent": agent, "channel": channel})


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
        # Record activity for the logged-in session (sidebar live status).
        record_activity(session_name, channel=channel, last_message=content)
        # If the session agent has a focused channel set, refocus so the
        # sidebar reflects "currently posting in #X".
        if session_name in BridgeState.focus_map:
            set_focus(session_name, BridgeState.focus_map[session_name]["channel"])
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


async def _handle_stream_agent_status(request: web.Request) -> web.StreamResponse:
    """SSE channel that emits agent liveness + focus change events.

    Subscribers receive a snapshot of current state on connect, then
    every focus / activity change is pushed as an SSE event. A heartbeat
    keeps the connection alive across NAT/proxy idle timeouts.
    """
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

    # Each subscriber gets its own queue. We push events into it from
    # set_focus/record_activity and consume here.
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    BridgeState.agent_status_subs.append(queue)

    try:
        # Initial snapshot — sidebar can render before any new event.
        snapshot = {
            "agents": {
                name: {
                    **state,
                    "status": compute_status(state),
                    "age_seconds": round(time.time() - state.get("last_activity_ts", 0.0), 1),
                }
                for name, state in BridgeState.agent_status.items()
            },
            "focus": dict(BridgeState.focus_map),
        }
        await resp.write(
            b"event: snapshot\ndata: " + json.dumps(snapshot).encode() + b"\n\n"
        )

        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=10.0)
                event_name = payload.get("type", "agent_status")
                await resp.write(
                    f"event: {event_name}\ndata: ".encode()
                    + json.dumps(payload).encode()
                    + b"\n\n"
                )
            except asyncio.TimeoutError:
                await resp.write(b": heartbeat\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        try:
            BridgeState.agent_status_subs.remove(queue)
        except ValueError:
            pass

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

    Reconnect: supports Last-Event-ID header (or ``?since=<unix-ts>`` query
    param) — replays events whose ``created_at`` is >= the supplied cutoff
    so a client that reconnects doesn't miss messages buffered while it was
    gone.  Each event is emitted with an ``id: <event_id>`` line so a
    browser EventSource can resume cleanly on automatic reconnect.
    """
    state = get_state(request.app)
    channel = request.query.get("channel", "").strip()
    if not channel:
        return web.json_response({"error": "channel required"}, status=400)

    # Special internal channel: agent_status. Subscribers receive live
    # liveness + focus events broadcast by set_focus() and record_activity().
    if channel == "agent_status":
        return await _handle_stream_agent_status(request)

    # Resolve the resume cutoff.  Last-Event-ID header takes precedence
    # over ?since= (matches the SSE spec).  We treat the id as a Nostr
    # event id and resolve it via the relay to the event's created_at;
    # fall back to the raw header as a unix timestamp if it isn't hex.
    since_ts: int | None = None
    last_event_id = request.headers.get("Last-Event-ID") or request.query.get("since")
    if last_event_id:
        # Try numeric first (explicit unix ts).
        try:
            since_ts = int(last_event_id)
        except ValueError:
            # Otherwise treat as Nostr event id and look it up.
            try:
                async with aiohttp.ClientSession() as s2:
                    async with s2.get(
                        f"{_relay_http_from_ws(state.config['relays'][0])}/event/{last_event_id}",
                        timeout=aiohttp.ClientTimeout(total=2),
                    ) as rr:
                        if rr.status == 200:
                            ev = await rr.json(content_type=None)
                            since_ts = int(ev.get("created_at", 0))
            except Exception:
                since_ts = None

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

    seen_ids: set[str] = set()

    await resp.write(b"event: connected\ndata: " + json.dumps({
        "channel": channel,
        "poll_interval_ms": POLL_INTERVAL_MS,
        "since": since_ts,
    }).encode() + b"\n\n")

    async with aiohttp.ClientSession() as session:
        try:
            while True:
                try:
                    async with session.get(
                        f"{relay_http}/events", timeout=aiohttp.ClientTimeout(total=5),
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
                    if not _event_in_channel(ev, channel):
                        continue
                    created_at = int(ev.get("created_at", 0))
                    if since_ts is not None and created_at <= since_ts:
                        # Event is older than or equal to our resume cutoff
                        # — mark it seen so we don't re-emit it on
                        # subsequent polls, but don't send it.  We use <=
                        # because Last-Event-ID points to the LAST event
                        # the client already saw; an exact-timestamp
                        # replay would be a duplicate.
                        seen_ids.add(eid)
                        continue
                    seen_ids.add(eid)
                    payload = {
                        "event_id": eid,
                        "kind": ev.get("kind"),
                        "pubkey": ev.get("pubkey"),
                        "created_at": created_at,
                        "content": ev.get("content", ""),
                        "tags": ev.get("tags", []),
                    }
                    # Emit SSE id line so EventSource can resume after
                    # disconnect by sending Last-Event-ID on reconnect.
                    await resp.write(
                        b"id: " + eid.encode() + b"\n"
                        + b"event: message\ndata: "
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
    app.router.add_post("/v1/ui/agents", handle_create_agent)
    app.router.add_post("/v1/ui/agents/import-memory", handle_agent_import_memory)
    app.router.add_get("/v1/ui/memory/preview", handle_memory_preview)
    app.router.add_get("/v1/ui/focus", handle_focus_get)
    app.router.add_post("/v1/ui/focus", handle_focus_post)
    app.router.add_get("/v1/ui/stream", handle_stream)
    app.router.add_get("/v1/ui/memory/sources", handle_memory_list_sources)
    app.router.add_get("/v1/ui/memory/agents", handle_memory_list_agents)
    app.router.add_get("/v1/ui/memory/agents/{name}", handle_memory_get_agent)
    app.router.add_post("/v1/ui/memory/agents/{name}/sections/{section}/lines", handle_memory_append_line)
    app.router.add_delete("/v1/ui/memory/agents/{name}/sections/{section}/lines/{idx}", handle_memory_delete_line)
    app.router.add_put("/v1/ui/memory/agents/{name}/sections/{section}", handle_memory_replace_section)
    app.router.add_post("/v1/ui/memory/import", handle_memory_import)
    app.router.add_get("/v1/ui/memory/shared", handle_memory_shared_get)
    app.router.add_put("/v1/ui/memory/shared/sections/{section}", handle_memory_shared_replace)
    app.router.add_post("/v1/ui/memory/shared/sections/{section}/lines", handle_memory_shared_append)
    app.router.add_delete("/v1/ui/memory/shared/sections/{section}/lines/{idx}", handle_memory_shared_delete_line)
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
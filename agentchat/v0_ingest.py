"""
agentchat v1.2 — Nostr → v0 backplane ingest bridge (v1.2.0.dev24).

The v1.2.0 web bridge (port 9877) and the legacy v0 backplane (port 7878)
are two independent systems. The bridge publishes kind:9 events to the
Nostr relay (port 9876). The v0 backplane stores messages in its own
SQLite. The chappy + hermes respond daemons poll the v0 backplane
**only** — they never see Nostr events.

This service closes that gap: it subscribes to the Nostr relay, filters
for kind:9 events whose ``#h`` tag matches a configured channel, and
re-posts the message body to the v0 backplane's
``POST /v1/threads/<thread_id>/messages`` endpoint using a Bearer token.

The result: a message posted in the v1.2.0 UI appears in the v0
backplane within ~1s, the respond daemon polls it, the LLM runs, the
reply is written back to the v0 thread, and the v0 SSE stream (port
7879) carries it to the v0 web UI. The v1.2.0 SSE stream (port 9877)
also carries it because the reply is also published to Nostr (via the
respond daemon's own path, which writes to BOTH v0 and Nostr).

Channel → thread mapping is configurable so multiple channels can fan
out to one thread, or one channel can fan out to several threads.

Security:
    * Bearer token is read from env / file. NEVER logged.
    * The body of the message is also NEVER logged in full (truncated).
    * Event IDs are logged so duplicates can be correlated.

Operational:
    * Stdlib + ``websockets`` (already a dep of the repo).
    * One persistent WebSocket connection to the relay.
    * Dedup by Nostr event_id in-memory (sufficient because the relay
      is local; for multi-process deploys, switch to a Redis SET).
    * On HTTP failure, log and drop. A retry queue is a future
      enhancement.

Why not just patch the bridge?
    The bridge is the v1.2.0 path: Nostr-only, ephemeral UI, no v0
    auth. Adding v0 auth + thread-mapping logic to it would couple
    the two systems and break the "v1.2 is independent" property.
    A small sidecar service is cleaner and gives us a single place
    to add per-channel routing rules in the future.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import signal
import sys
import time
from typing import Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore
    ConnectionClosed = Exception  # type: ignore

LOG = logging.getLogger("agentchat.v0_ingest")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class BridgeConfig:
    """Runtime configuration for the v0_ingest service."""

    nostr_relay: str = "ws://127.0.0.1:9876"
    v0_base: str = "http://127.0.0.1:7878"
    v0_token: str = ""  # bearer; loaded separately
    channel_to_thread: dict[str, str] = dataclasses.field(default_factory=dict)
    default_from_agent: str = "waynec"
    log_level: str = "INFO"
    # Pinned pubkey → agent-name map. If the Nostr event's pubkey is in
    # this map, we use the mapped name. Otherwise we fall back to
    # ``default_from_agent``. Empty dict means always use the default.
    pubkey_to_agent: dict[str, str] = dataclasses.field(default_factory=dict)
    # Use the v1.0 ``name:secret`` legacy token format. The v0 backplane
    # accepts both the v0.1 SHA256-hashed opaque token AND the v1.0
    # ``<agent>:<secret>`` form. The legacy form is more robust when
    # api_tokens tables are stale (after a partial restore, etc).
    legacy_token_format: bool = False

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        channel_map_raw = os.environ.get(
            "AGENTCHAT_CHANNEL_THREAD_MAP",
            '{"general": "wayne-chappy-hermes"}',
        )
        try:
            channel_map = json.loads(channel_map_raw)
            if not isinstance(channel_map, dict):
                raise ValueError("not a dict")
        except (ValueError, json.JSONDecodeError) as e:
            raise SystemExit(
                f"AGENTCHAT_CHANNEL_THREAD_MAP must be a JSON object, got: {channel_map_raw!r} ({e})"
            )

        pubkey_map_raw = os.environ.get("AGENTCHAT_PUBKEY_AGENT_MAP", "{}")
        try:
            pubkey_map = json.loads(pubkey_map_raw)
            if not isinstance(pubkey_map, dict):
                raise ValueError("not a dict")
        except (ValueError, json.JSONDecodeError) as e:
            raise SystemExit(
                f"AGENTCHAT_PUBKEY_AGENT_MAP must be a JSON object, got: {pubkey_map_raw!r} ({e})"
            )

        return cls(
            nostr_relay=os.environ.get("AGENTCHAT_NOSTR_RELAY", "ws://127.0.0.1:9876"),
            v0_base=os.environ.get("AGENTCHAT_V0_BASE", "http://127.0.0.1:7878"),
            v0_token=os.environ.get("AGENTCHAT_V0_TOKEN", ""),
            channel_to_thread=channel_map,
            default_from_agent=os.environ.get("AGENTCHAT_DEFAULT_FROM", "waynec"),
            log_level=os.environ.get("AGENTCHAT_LOG_LEVEL", "INFO"),
            pubkey_to_agent=pubkey_map,
            legacy_token_format=os.environ.get(
                "AGENTCHAT_V0_LEGACY_TOKEN", ""
            ).lower() in ("1", "true", "yes"),
        )


# ---------------------------------------------------------------------------
# V0 backplane client
# ---------------------------------------------------------------------------


def post_to_v0_thread(
    base_url: str,
    token: str,
    thread_id: str,
    body: str,
    from_agent: str,
    nostr_event_id: str,
    legacy_token_format: bool = False,
) -> tuple[int, str]:
    """POST a message to the v0 backplane thread endpoint.

    Returns (status_code, response_body). Status 0 + error string on
    transport failure. Never logs the bearer token.

    If ``legacy_token_format`` is True, the token is sent as
    ``<from_agent>:<token>`` (the v1.0 legacy ``name:secret`` format).
    The v0 backplane accepts this in addition to the v0.1 SHA256-hashed
    opaque-token format. Useful when the v0 deployment has stale
    api_tokens tables (common after a partial restore).
    """
    url = f"{base_url.rstrip('/')}/v1/threads/{thread_id}/messages"
    payload = json.dumps({
        "body": body,
        "metadata": {
            "from_agent": from_agent,
            "nostr_event_id": nostr_event_id,
        },
    }).encode("utf-8")
    bearer = f"{from_agent}:{token}" if legacy_token_format else token
    req = urllib_request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            return resp.status, data
    except urllib_error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except (urllib_error.URLError, TimeoutError, OSError) as e:
        return 0, str(e)


# ---------------------------------------------------------------------------
# Bridge service
# ---------------------------------------------------------------------------


class V0IngestBridge:
    """Long-running service: subscribe to Nostr, write to v0 backplane."""

    def __init__(self, config: BridgeConfig):
        self.config = config
        self._seen_event_ids: set[str] = set()
        self._seen_max = 10_000  # cap dedup memory
        self._stop = asyncio.Event()
        self._stats = {"events_seen": 0, "events_posted": 0, "events_failed": 0}

    def request_stop(self) -> None:
        self._stop.set()

    def stats(self) -> dict:
        return dict(self._stats)

    async def run(self) -> int:
        if websockets is None:
            LOG.error("websockets package not installed; cannot run bridge")
            return 2
        if not self.config.v0_token:
            LOG.error("AGENTCHAT_V0_TOKEN not set; cannot authenticate to v0 backplane")
            return 2
        if not self.config.channel_to_thread:
            LOG.warning("No channel→thread mapping configured; bridge is a no-op")

        while not self._stop.is_set():
            try:
                await self._run_once()
            except ConnectionClosed:
                LOG.warning("Nostr relay connection closed; reconnecting in 3s")
                await asyncio.sleep(3)
            except Exception as e:  # noqa: BLE001
                LOG.exception("bridge loop error: %s; restarting in 5s", e)
                await asyncio.sleep(5)
        return 0

    async def _run_once(self) -> None:
        cfg = self.config
        async with websockets.connect(cfg.nostr_relay, ping_interval=20) as ws:
            sub = {
                "kinds": [9],
                "#h": list(cfg.channel_to_thread.keys()),
            }
            sub_id = f"v0_ingest_{int(time.time())}"
            await ws.send(json.dumps(["REQ", sub_id, sub]))
            LOG.info(
                "subscribed to Nostr relay=%s sub=%s kinds=[9] channels=%s",
                cfg.nostr_relay, sub_id, list(cfg.channel_to_thread.keys()),
            )
            async for raw in ws:
                if self._stop.is_set():
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    LOG.debug("non-JSON frame from relay: %r", raw[:100])
                    continue
                if not isinstance(msg, list) or len(msg) < 2:
                    continue
                kind = msg[0]
                if kind == "EVENT" and len(msg) >= 3:
                    await self._handle_event(msg[2])
                elif kind == "CLOSED":
                    LOG.warning("relay closed sub: %s", msg)
                # EOSE / NOTICE / OK are informational; ignore.

    async def _handle_event(self, ev: dict) -> None:
        self._stats["events_seen"] += 1
        eid = ev.get("id", "")
        if not eid:
            return
        if eid in self._seen_event_ids:
            LOG.debug("dup event %s, skipping", eid[:12])
            return
        self._seen_event_ids.add(eid)
        if len(self._seen_event_ids) > self._seen_max:
            # Drop oldest half. Order is not guaranteed in a set, so we
            # just slice the first N we see on the next iteration. For
            # our use case (local relay, low volume) this is fine.
            self._seen_event_ids = set(list(self._seen_event_ids)[self._seen_max // 2 :])

        # Extract channel from tags. Nostr convention: tag is ["h", channel_id].
        channel = None
        for tag in ev.get("tags", []):
            if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "h":
                channel = tag[1]
                break
        thread_id = self.config.channel_to_thread.get(channel or "")
        if not thread_id:
            LOG.debug("no thread for channel=%s, dropping event=%s", channel, eid[:12])
            return

        # Resolve sender. Pubkey is the lower-case hex of the event's pubkey.
        pubkey = (ev.get("pubkey") or "").lower()
        from_agent = self.config.pubkey_to_agent.get(pubkey) or self.config.default_from_agent

        body = ev.get("content", "")
        if not body:
            LOG.debug("empty body, dropping event=%s", eid[:12])
            return

        # Write to v0 (synchronous HTTP in a thread to avoid blocking the loop).
        loop = asyncio.get_running_loop()
        status, resp = await loop.run_in_executor(
            None,
            post_to_v0_thread,
            self.config.v0_base,
            self.config.v0_token,
            thread_id,
            body,
            from_agent,
            eid,
            self.config.legacy_token_format,
        )

        if 200 <= status < 300:
            self._stats["events_posted"] += 1
            LOG.info(
                "posted event=%s from=%s channel=%s thread=%s status=%d body=%r",
                eid[:12], from_agent, channel, thread_id, status, body[:60],
            )
        else:
            self._stats["events_failed"] += 1
            LOG.warning(
                "post FAILED event=%s from=%s thread=%s status=%d resp=%s",
                eid[:12], from_agent, thread_id, status, resp[:200],
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentchat-v0-ingest",
        description="Nostr → v0 backplane ingest bridge (v1.2.0.dev24).",
    )
    parser.add_argument(
        "--nostr-relay", default=None,
        help="Nostr relay WebSocket URL (default: AGENTCHAT_NOSTR_RELAY or ws://127.0.0.1:9876)",
    )
    parser.add_argument(
        "--v0-base", default=None,
        help="v0 backplane base URL (default: AGENTCHAT_V0_BASE or http://127.0.0.1:7878)",
    )
    parser.add_argument(
        "--v0-token-file", default=None,
        help="Path to a file containing the bearer token (preferred over --v0-token)",
    )
    parser.add_argument(
        "--v0-token", default=None,
        help="Bearer token (insecure; prefer --v0-token-file or env AGENTCHAT_V0_TOKEN)",
    )
    parser.add_argument(
        "--channel-map", default=None,
        help="JSON object string mapping Nostr channel → v0 thread_id",
    )
    parser.add_argument(
        "--default-from", default=None,
        help="Default from_agent for events with unmapped pubkey (default: waynec)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--legacy-token", action="store_true",
        help="Use v1.0 <name>:<secret> token format (default: opaque)",
    )
    parser.add_argument(
        "--stats-interval", type=float, default=60.0,
        help="Seconds between stats log lines (default 60)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )

    cfg = BridgeConfig.from_env()
    if args.nostr_relay:
        cfg.nostr_relay = args.nostr_relay
    if args.v0_base:
        cfg.v0_base = args.v0_base
    if args.channel_map:
        cfg.channel_to_thread = json.loads(args.channel_map)
    if args.default_from:
        cfg.default_from_agent = args.default_from
    if args.legacy_token:
        cfg.legacy_token_format = True

    # Token resolution priority: --v0-token > --v0-token-file > env.
    if args.v0_token:
        cfg.v0_token = args.v0_token
    elif args.v0_token_file:
        with open(args.v0_token_file, encoding="utf-8") as f:
            cfg.v0_token = f.read().strip()
    # else: rely on env from from_env()

    if not cfg.v0_token:
        LOG.error(
            "No v0 bearer token provided. Set AGENTCHAT_V0_TOKEN, use --v0-token, "
            "or --v0-token-file <path>."
        )
        return 2

    bridge = V0IngestBridge(cfg)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _on_signal(signame: str) -> None:
        LOG.info("received %s, shutting down", signame)
        bridge.request_stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig.name)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main thread: skip
            pass

    async def _stats_logger() -> None:
        while not bridge._stop.is_set():
            await asyncio.sleep(args.stats_interval)
            LOG.info("stats: %s", bridge.stats())

    try:
        stats_task = loop.create_task(_stats_logger())
        rc = loop.run_until_complete(bridge.run())
        stats_task.cancel()
        return rc
    finally:
        loop.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""
Agent manager — runs all enabled ReplyLoops concurrently.

Reads ~/.hermes/nostr/registry.json, picks entries with kind="agent",
loads their keypairs, instantiates the matching ReplyLoop subclass,
and runs each as an asyncio task.  Each loop has its own WS connection
to the relay.

Usage:
    .venv/bin/python -m agentchat.agents.manager
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path

from agentchat.agents.base import ReplyLoop

log = logging.getLogger("agent-manager")


# Registry of agent name -> loop factory.
# Order matters only for log readability.
AGENT_FACTORIES = {}


def _identity_dir() -> Path:
    override = os.environ.get("AGENTCHAT_NOSTR_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes" / "nostr"


def _load_registry() -> dict[str, dict]:
    path = _identity_dir() / "registry.json"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log.warning("registry load failed: %s", e)
        return {}


def _agent_pubkeys(reg: dict[str, dict]) -> set[str]:
    return {
        info["public_key_hex"].lower()
        for info in reg.values()
        if info.get("kind") == "agent" and "public_key_hex" in info
    }


def _agent_pub_to_name(reg: dict[str, dict]) -> dict[str, str]:
    return {
        info["public_key_hex"].lower(): name
        for name, info in reg.items()
        if "public_key_hex" in info
    }


def _build_loops() -> list[ReplyLoop]:
    """Import the agent modules on demand (avoids requiring every
    agent's deps to be installed for tests)."""
    # Lazy imports — keeps agentchat/agents/* optional.
    from agentchat.agents.chappy import make_chappy_loop
    from agentchat.agents.hermes import make_hermes_loop

    AGENT_FACTORIES.update({
        "hermes": make_hermes_loop,
        "chappy": make_chappy_loop,
    })

    reg = _load_registry()
    agent_pubs = _agent_pubkeys(reg)
    log.info("registry: %d identities, %d agents", len(reg), len(agent_pubs))

    loops: list[ReplyLoop] = []
    for name, info in reg.items():
        if info.get("kind") != "agent":
            continue
        factory = AGENT_FACTORIES.get(name)
        if not factory:
            log.warning("no loop factory for agent '%s'; skipping", name)
            continue
        try:
            loops.append(factory(agent_pubkeys=agent_pubs))
        except Exception as e:
            log.warning("failed to instantiate %s loop: %s", name, e)

    return loops


async def _run_all() -> None:
    loops = _build_loops()
    if not loops:
        log.warning("no agent loops to run — exiting")
        return

    log.info("starting %d agent loops: %s", len(loops), ", ".join(l.name for l in loops))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal(*_a):
        log.info("signal received; stopping all loops")
        stop.set()
        for lp in loops:
            lp.stop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass

    tasks = [asyncio.create_task(lp.run(), name=f"loop-{lp.name}") for lp in loops]
    try:
        await stop.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    log.info("manager stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)-12s | %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(_run_all())


if __name__ == "__main__":
    main()
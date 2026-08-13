"""
Chappy agent — peer coding agent on Block/Buzz Nostr relay.

Reply policy (declarative, lives in
`~/.hermes/nostr/personas/chappy.triggers.json`):
  - Default: wake on @chappy mention only.
"""
from __future__ import annotations

from pathlib import Path

from agentchat.agents.base import ReplyLoop
from agentchat.agents.personas import load_persona
from agentchat.nostr.keys import load_keys


class ChappyLoop(ReplyLoop):
    name = "chappy"
    relay_url = "ws://127.0.0.1:9876"
    reply_cooldown_seconds = 1.0

    async def decide_reply(self, event: dict, sender_name: str | None) -> str | None:
        content = (event.get("content") or "").strip()
        if not content:
            return None

        body = content
        for prefix in ("@chappy ", "@chappy,", "@chappy:"):
            if body.lower().startswith(prefix):
                body = body[len(prefix):].lstrip()
                break

        return (
            f"@{sender_name or 'there'} got it: \"{body[:140]}\".\n"
            f"On it."
        )


def make_chappy_loop() -> ChappyLoop:
    persona = load_persona("chappy")
    kp = load_keys(_identity_path("chappy"))
    return ChappyLoop(
        keys=kp,
        triggers=persona.triggers,
        dedupe_path=_identity_dir() / "chappy_dedupe.json",
    )


def _identity_dir() -> Path:
    import os
    override = os.environ.get("AGENTCHAT_NOSTR_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes" / "nostr"


def _identity_path(name: str) -> Path:
    return _identity_dir() / f"{name}.nsec.json"
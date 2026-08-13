"""
Chappy agent — peer coding agent.

Reply policy: same shape as hermes (deterministic ack for now), but
cooldown is tighter (Chappy is a faster back-and-forth partner).

In dev7+, `decide_reply()` will consult Chappy's actual conversation
state via the agentchat A2A thread.
"""
from agentchat.agents.base import ReplyLoop
from agentchat.nostr.keys import load_keys
from pathlib import Path
import os


class ChappyLoop(ReplyLoop):
    name = "chappy"
    relay_url = "ws://127.0.0.1:9876"
    reply_cooldown_seconds = 0.5

    async def decide_reply(self, event: dict, sender_name: str | None) -> str | None:
        content = (event.get("content") or "").strip()
        if not content:
            return None
        body = content
        for prefix in ("@chappy ", "@chappy,", "@chappy:"):
            if body.lower().startswith(prefix):
                body = body[len(prefix):].lstrip()
                break
        return f"@{sender_name or 'there'} got it: \"{body[:140]}\". On it."


def make_chappy_loop(agent_pubkeys: set[str] | None = None) -> ChappyLoop:
    kp = load_keys(_identity_path("chappy"))
    return ChappyLoop(
        keys=kp,
        agent_pubkeys=agent_pubkeys,
        dedupe_path=_identity_dir() / "chappy_dedupe.json",
    )


def _identity_dir() -> Path:
    override = os.environ.get("AGENTCHAT_NOSTR_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes" / "nostr"


def _identity_path(name: str) -> Path:
    return _identity_dir() / f"{name}.nsec.json"
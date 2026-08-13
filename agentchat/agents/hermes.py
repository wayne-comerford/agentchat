"""
Hermes agent — primary agent for Wayne.

Reply policy:
  - Only replies to principals (Wayne / wayne-observer).
  - Default body: acknowledge and ask if there's anything actionable.

In a later dev cycle, `decide_reply()` will hand off to an LLM call
(RestTech-aware, agentchat-aware, etc.).  For now it's a deterministic
acknowledgement so the loop architecture can be verified end-to-end.
"""
from agentchat.agents.base import ReplyLoop
from agentchat.nostr.keys import load_keys
from pathlib import Path


class HermesLoop(ReplyLoop):
    name = "hermes"
    relay_url = "ws://127.0.0.1:9876"
    reply_cooldown_seconds = 1.0  # at most 1 reply/sec

    async def decide_reply(self, event: dict, sender_name: str | None) -> str | None:
        content = (event.get("content") or "").strip()
        if not content:
            return None

        # Strip any leading @hermes mention.
        body = content
        for prefix in ("@hermes ", "@hermes,", "@hermes:"):
            if body.lower().startswith(prefix):
                body = body[len(prefix):].lstrip()
                break

        # Acknowledgement + ask whether there's real work.
        return (
            f"@{sender_name or 'there'} heard you: \"{body[:140]}\".\n"
            f"Want me to dig into that, or just acknowledging?"
        )


def make_hermes_loop(agent_pubkeys: set[str] | None = None) -> HermesLoop:
    kp = load_keys(_identity_path("hermes"))
    return HermesLoop(
        keys=kp,
        agent_pubkeys=agent_pubkeys,
        dedupe_path=_identity_dir() / "hermes_dedupe.json",
    )


# ---- shared path helpers (mirror mention_dispatcher style) ----
def _identity_dir() -> Path:
    import os
    override = os.environ.get("AGENTCHAT_NOSTR_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes" / "nostr"


def _identity_path(name: str) -> Path:
    return _identity_dir() / f"{name}.nsec.json"
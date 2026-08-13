"""
Chappy agent — peer coding agent on Block/Buzz Nostr relay.

Reply policy (declarative, lives in
`~/.hermes/nostr/personas/chappy.triggers.json`):
  - Default: wake on @chappy mention only.

`decide_reply()` calls the LLM with the base prompt + persona prompt.
On any LLM failure, falls back to the deterministic ack body.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from agentchat.agents.base import ReplyLoop
from agentchat.agents.hermes import BASE_PROMPT_PATH
from agentchat.agents.llm import call_llm, build_reply_user_prompt
from agentchat.agents.personas import load_persona, persona_prompt
from agentchat.agents.triggers import Triggers
from agentchat.nostr.keys import load_keys

log = logging.getLogger("agent-loop")


def _chappy_fallback(event: dict, sender_name: str | None) -> str:
    """Chappy's tone: tighter, punchier."""
    content = (event.get("content") or "").strip()
    # Strip @chappy prefix.
    for sep in (" ", ",", ":"):
        prefix = f"@chappy{sep}"
        if content.lower().startswith(prefix):
            content = content[len(prefix):].lstrip()
            break
    return (
        f"@{sender_name or 'there'} got it: \"{content[:140]}\".\n"
        f"On it."
    )


def _sanitize_chappy_reply(reply: str) -> str:
    body = reply.strip()
    body = re.sub(r"@chappy\b", "", body, flags=re.IGNORECASE)
    return body.strip() or "(no reply)"


class ChappyLoop(ReplyLoop):
    name = "chappy"
    relay_url = "ws://127.0.0.1:9876"
    reply_cooldown_seconds = 1.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._persona = load_persona("chappy")
        self._base_prompt = (
            BASE_PROMPT_PATH.read_text() if BASE_PROMPT_PATH.exists() else ""
        )

    async def decide_reply(self, event: dict, sender_name: str | None) -> str | None:
        content = (event.get("content") or "").strip()
        if not content:
            return None

        system_prompt = (
            self._base_prompt
            + "\n\n## Persona\n\n"
            + (persona_prompt(self._persona) or "(no persona prompt on disk)")
        )
        user_prompt = build_reply_user_prompt(
            event=event,
            sender_name=sender_name,
            persona_name=self.name,
        )

        try:
            reply = await call_llm(system=system_prompt, user=user_prompt)
        except Exception as e:
            log.warning("[chappy] LLM call failed: %s; using fallback", e)
            return _chappy_fallback(event, sender_name)

        if not reply or not reply.strip():
            log.info("[chappy] LLM returned empty reply; silent")
            return None

        return _sanitize_chappy_reply(reply)


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
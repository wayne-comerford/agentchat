"""
Hermes agent — primary agent for Wayne.

Reply policy (declarative, lives in `~/.hermes/nostr/personas/hermes.triggers.json`):
  - Default: wake on @hermes mention only.
  - This is the Buzz persona pattern: behaviour lives in the persona file,
    not in code.

`decide_reply()` calls the LLM (via `agentchat.agents.llm.call_llm`) with
the base prompt + persona prompt as the system message.  On any LLM
failure (timeout, non-zero exit, empty reply), we fall back to the
deterministic ack body so the channel stays responsive.
"""
from __future__ import annotations

import logging
from pathlib import Path

from agentchat.agents.base import ReplyLoop
from agentchat.agents.llm import call_llm, build_reply_user_prompt
from agentchat.agents.personas import load_persona, persona_prompt
from agentchat.agents.triggers import Triggers
from agentchat.nostr.keys import load_keys

log = logging.getLogger("agent-loop")


# Path to the shared base prompt, written into the package by dev7.
BASE_PROMPT_PATH = Path(__file__).parent / "base_prompt.md"


def _strip_self_mention(content: str, name: str) -> str:
    """Strip a leading `@name[,.:]?` mention if present."""
    body = content
    for sep in (" ", ",", ":"):
        for prefix in (f"@{name}{sep}", f"@{name.lower()}{sep}"):
            if body.lower().startswith(prefix):
                return body[len(prefix):].lstrip()
    return body


def _fallback_reply(event: dict, sender_name: str | None) -> str:
    """Deterministic ack used when the LLM call fails."""
    body = _strip_self_mention((event.get("content") or "").strip(), "hermes")
    return (
        f"@{sender_name or 'there'} heard you: \"{body[:140]}\".\n"
        f"Want me to dig into that, or just acknowledging?"
    )


def _sanitize_reply(reply: str) -> str:
    """Belt + braces: strip any self-mention that would re-trigger the loop."""
    # Drop leading "@hermes[,.: ]" so the relay doesn't see it as a #p tag.
    body = _strip_self_mention(reply.strip(), "hermes")
    # Strip accidental body mentions.
    import re
    body = re.sub(r"@hermes\b", "", body, flags=re.IGNORECASE)
    return body.strip() or "(no reply)"


class HermesLoop(ReplyLoop):
    name = "hermes"
    relay_url = "ws://127.0.0.1:9876"
    reply_cooldown_seconds = 1.0  # at most 1 reply/sec

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Load persona once at startup so we don't re-read on every event.
        self._persona = load_persona("hermes")
        self._base_prompt = (
            BASE_PROMPT_PATH.read_text() if BASE_PROMPT_PATH.exists() else ""
        )

    async def decide_reply(self, event: dict, sender_name: str | None) -> str | None:
        content = (event.get("content") or "").strip()
        if not content:
            return None

        # Build system prompt: base rules + persona voice.
        # Context-disclaimer (dev29): the subprocess is invoked with
        # --ignore-rules --ignore-user-config, but the prompt itself
        # also disclaims prior context so the LLM doesn't try to
        # reference "earlier messages" or system markers.
        system_prompt = (
            "You are a fresh agent. You have NO prior context, no memory of "
            "earlier conversations, no knowledge of the operator's other "
            "chats, and no access to any system messages. The text below "
            "is a single isolated message that requires one short reply.\n\n"
            + self._base_prompt
            + "\n\n## Persona\n\n"
            + (persona_prompt(self._persona) or "(no persona prompt on disk)")
        )

        user_prompt = build_reply_user_prompt(
            event=event,
            sender_name=sender_name,
            persona_name=self.name,
        )

        # LLM call with deterministic fallback on any failure.
        try:
            reply = await call_llm(system=system_prompt, user=user_prompt)
        except Exception as e:
            log.warning("[hermes] LLM call failed: %s; using fallback", e)
            return _fallback_reply(event, sender_name)

        # Empty / silence reply → silent (model may have decided to stay quiet).
        if not reply or not reply.strip():
            log.info("[hermes] LLM returned empty reply; silent")
            return None
        # The "**silence**" sentinel Buzz uses (model echoed the rule).
        cleaned = reply.strip().lower().strip("*").strip()
        if cleaned in {"silence", "(silence)", "no reply"}:
            log.info("[hermes] LLM signalled silence; staying quiet")
            return None

        return _sanitize_reply(reply)


def make_hermes_loop() -> HermesLoop:
    persona = load_persona("hermes")
    kp = load_keys(_identity_path("hermes"))
    return HermesLoop(
        keys=kp,
        triggers=persona.triggers,
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
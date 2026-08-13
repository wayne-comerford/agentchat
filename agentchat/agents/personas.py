"""
agentchat v1.2 — Personas (Buzz pattern, ported from `.persona.md`).

Buzz uses `.persona.md` files (YAML frontmatter + markdown body) to
declare each agent's identity, behaviour, and wake triggers.  We use
two sibling files instead, both rooted at `~/.hermes/nostr/personas/`:

  <name>.triggers.json   — the `Triggers` object (mentions/keywords/etc.)
  <name>.prompt.md       — the markdown body that becomes the system prompt

Why split into two files?  The triggers are loaded every event (hot
path); the system prompt is only loaded when an LLM call is wired in
(dev8).  Keeping them separate avoids parsing markdown on every event.

For dev7 we only use the triggers file.  The prompt file is *read* by
`load_persona()` so we can verify it exists, but not yet *consumed*
by `decide_reply()` (that's dev8).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agentchat.agents.triggers import Triggers


@dataclass
class Persona:
    """Loaded persona: triggers (always) + system prompt (lazy via .prompt)."""
    name: str
    triggers: Triggers
    prompt_path: Path | None   # may be None if no .prompt.md exists yet
    triggers_path: Path


def _personas_dir() -> Path:
    override = os.environ.get("AGENTCHAT_NOSTR_DIR")
    if override:
        return Path(override).expanduser() / "personas"
    return Path.home() / ".hermes" / "nostr" / "personas"


def load_persona(name: str) -> Persona:
    """Load triggers + prompt path for an agent.  Missing triggers file
    defaults to `Triggers()` (wake on @mention only).  Missing prompt
    file is fine — just leaves prompt_path=None."""
    pdir = _personas_dir()
    triggers_path = pdir / f"{name}.triggers.json"
    prompt_path = pdir / f"{name}.prompt.md"
    triggers = Triggers.load(triggers_path)
    return Persona(
        name=name,
        triggers=triggers,
        prompt_path=prompt_path if prompt_path.exists() else None,
        triggers_path=triggers_path,
    )


def persona_prompt(persona: Persona) -> str:
    """Return the persona's system prompt (markdown body).  Empty string
    if no prompt file exists."""
    if persona.prompt_path is None:
        return ""
    try:
        return persona.prompt_path.read_text()
    except Exception:
        return ""
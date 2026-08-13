"""
agentchat v1.2 — Triggers (Buzz pattern).

Ported from Buzz's `crates/buzz-persona/src/persona.rs::RespondTo` and
`crates/buzz-acp/src/filter.rs::SubscriptionRule`.

Each agent declares when it should wake up.  The base reply loop calls
`should_reply()` against every incoming event.  This is the *single*
gate for reply decisions — no separate "agent vs principal" registry
flag, no special-case code in subclasses.  Loop prevention is a
natural consequence: a reply from hermes does not @hermes and does
not match hermes's keywords, so the trigger returns False.

Schema (mirrors Buzz's `RespondTo`):
    mentions:      bool   — wake when my pubkey is in #p tags (default True)
    keywords:      list   — wake when any of these (case-insensitive) appear in content
    all_messages:  bool   — wake for every event in subscribed channels (default False)
    from_authors:  list   — wake only when authored by these npubs (optional allowlist)

Storage: a plain dict, persisted next to the persona file in
`~/.hermes/nostr/personas/<name>.triggers.json`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class Triggers:
    """Declarative wake-up rules for an agent reply loop."""
    mentions: bool = True
    keywords: list[str] = field(default_factory=list)
    all_messages: bool = False
    from_authors: list[str] = field(default_factory=list)

    # ----- serialisation -----
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Triggers":
        return cls(
            mentions=bool(d.get("mentions", True)),
            keywords=list(d.get("keywords", [])),
            all_messages=bool(d.get("all_messages", False)),
            from_authors=list(d.get("from_authors", [])),
        )

    @classmethod
    def load(cls, path: Path) -> "Triggers":
        """Load from JSON.  Missing file → defaults (mentions=True)."""
        if not path.exists():
            return cls()
        try:
            return cls.from_dict(json.loads(path.read_text()))
        except Exception:
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))

    # ----- the gate -----
    def should_reply(
        self,
        event: dict,
        *,
        agent_pubkey: str,
        sender_pubkey: str,
    ) -> bool:
        """
        Return True if this event should wake the agent.

        Eval order (matches Buzz's filter.rs short-circuit logic):
            1. Self-mention → never (handled by caller, defensive check here)
            2. from_authors allowlist (if non-empty, sender must be in it)
            3. all_messages → True
            4. mentions (event #p contains agent_pubkey) → True
            5. keywords (case-insensitive substring match in content) → True
            6. Otherwise → False (silent)

        The agent's *own* replies will never match (no self-#p, no keywords
        by default), so this is loop-safe by construction.
        """
        if sender_pubkey == agent_pubkey.lower():
            return False

        # from_authors allowlist narrows the trigger (intersection, not OR).
        if self.from_authors:
            if sender_pubkey not in [a.lower() for a in self.from_authors]:
                return False

        # Tier 1: all_messages (broadest)
        if self.all_messages:
            return True

        # Tier 2: explicit mention in #p tags
        if self.mentions:
            tags = event.get("tags") or []
            agent_lc = agent_pubkey.lower()
            for tag in tags:
                if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "p":
                    if str(tag[1]).lower() == agent_lc:
                        return True

        # Tier 3: keyword match in content
        if self.keywords:
            content = (event.get("content") or "").lower()
            for kw in self.keywords:
                if kw.lower() in content:
                    return True

        return False
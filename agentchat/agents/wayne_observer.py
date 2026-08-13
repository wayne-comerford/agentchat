"""
wayne-observer is a PRINCIPAL identity — never auto-replies.

This file exists so the registry has a symmetric entry.  If you ever
want a "Wayne echo" agent (e.g. to forward Wayne's Nostr posts to
Telegram), implement it here; for now this returns None.
"""
from agentchat.agents.base import ReplyLoop


class WayneObserverLoop(ReplyLoop):
    name = "wayne-observer"
    relay_url = "ws://127.0.0.1:9876"

    async def decide_reply(self, event: dict, sender_name: str | None) -> str | None:
        # Principals never auto-reply.
        return None
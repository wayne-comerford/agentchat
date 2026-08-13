"""
agentchat v1.2 — LLM backend (dev8).

Calls `hermes chat -q <prompt> -Q` as a subprocess for each reply.  This is
the simplest possible backend: the LLM is "this same Hermes session, in a
fresh subprocess, with the persona prompt as its only system message."

Why subprocess-per-call?
  - No new API keys needed (minimax.io is already configured).
  - The subprocess is short-lived, so a flake in the LLM API cannot
    wedge our reply loop.  The base loop has its own retry/backoff.
  - The fresh subprocess has no in-progress reasoning or conversation
    history; that matches the per-channel session model in base_prompt.md.

Latency:
  - Each call: ~10-30s on a good quota, longer with backpressure.
  - Mitigation: ReplyLoop has a 1s reply_cooldown and `asyncio.wait_for`
    on the call; if it doesn't return in N seconds, we fall back to the
    deterministic ack body so the conversation stays responsive.

Cost:
  - MiniMax quota counts each subprocess as 1 LLM turn.
  - We only call when the trigger fires; for our 3 personas that's rare.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("agentchat-llm")


@dataclass
class LLMConfig:
    """Configuration for the LLM backend."""
    provider: str = "minimax"
    model: str = "MiniMax-M3"
    timeout_seconds: float = 60.0   # per-call hard cap
    max_tokens: int = 600           # not directly used by hermes chat, but logged
    bin: str = "hermes"             # path to hermes CLI; falls back to PATH search


# Module-level default; tests can override.
DEFAULT_CONFIG = LLMConfig()


def _find_hermes_bin() -> str:
    """Locate the `hermes` CLI on PATH or in the well-known venv."""
    found = shutil.which("hermes")
    if found:
        return found
    # Fallback: ~/.local/bin or venv/bin (best-effort).
    for cand in (
        Path.home() / ".local" / "bin" / "hermes",
        Path("/usr/local/bin/hermes"),
    ):
        if cand.exists():
            return str(cand)
    return "hermes"  # let subprocess raise


async def call_llm(
    *,
    system: str,
    user: str,
    config: LLMConfig | None = None,
) -> str:
    """
    Call the LLM and return the response text.

    `system` and `user` are concatenated into a single prompt — hermes chat
    doesn't have a --system flag in this build.  We delimit them with a
    triple-newline so the model treats them as separate sections.

    Raises:
        asyncio.TimeoutError: call exceeded config.timeout_seconds.
        RuntimeError: hermes CLI not on PATH or returned empty.
    """
    cfg = config or DEFAULT_CONFIG
    bin_path = _find_hermes_bin()

    # Build the combined prompt.
    prompt = (
        f"{system.strip()}\n\n"
        f"---\n\n"
        f"{user.strip()}"
    )

    cmd = [
        bin_path, "chat",
        "-q", prompt,
        "-Q",
        "--provider", cfg.provider,
        "--model", cfg.model,
        # No toolsets — replies are pure text, no tool use.
        "-t", "",
    ]

    log.info("LLM call: model=%s prompt_chars=%d", cfg.model, len(prompt))
    t0 = time.time()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=cfg.timeout_seconds
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        raise
    except FileNotFoundError as e:
        raise RuntimeError(f"hermes CLI not found at {bin_path}: {e}") from e

    elapsed = time.time() - t0

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()[:300]
        raise RuntimeError(f"hermes chat exited {proc.returncode}: {err}")

    # `hermes chat -q -Q` prints "session_id: <id>\n<reply>".  Strip the
    # session line.  Sometimes there's a leading "Warning: ..." line too.
    text = stdout.decode("utf-8", errors="replace").strip()
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith(("session_id:", "Warning:"))]
    reply = "\n".join(lines).strip()
    log.info("LLM reply: %d chars in %.1fs", len(reply), elapsed)
    return reply


def build_reply_user_prompt(
    *,
    event: dict,
    sender_name: str | None,
    persona_name: str,
) -> str:
    """
    Build the user-side prompt that gets sent to the LLM.

    Includes enough context for a useful reply:
      - Current UTC time (LLM has no clock otherwise).
      - The triggering event's author (sender_name) + content.
      - The agent's own persona name (so it doesn't say "I am Hermes" if
        it's actually Chappy).
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    content = (event.get("content") or "").strip()
    if len(content) > 1000:
        content = content[:997] + "..."

    return (
        f"Current time: {now}\n"
        f"Your persona name: {persona_name}\n"
        f"From: {sender_name or '(unknown)'}\n"
        f"Channel event:\n"
        f"```\n{content}\n```\n\n"
        f"Reply as {persona_name}. 1-3 sentences. No preamble. "
        f"Address {sender_name or 'the sender'} directly. "
        f"If the message needs no reply, return empty."
    )
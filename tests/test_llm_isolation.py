"""
Tests for peer-agent LLM subprocess isolation (dev29).

The bug: `call_llm` invokes `hermes chat -q` as a subprocess. Without
explicit isolation flags, the subprocess loads the parent Hermes
session's user config, persistent memory, and state.db conversation
history (often 80k+ tokens). The LLM then generates replies that
reflect the actual operating context (Telegram chat, etc.) instead
of the in-character persona prompt.

The fix: pass `--ignore-rules` and `--ignore-user-config` so the
subprocess loads only the persona prompt we provide.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentchat.agents import llm as llm_mod


@pytest.mark.asyncio
async def test_call_llm_passes_ignore_flags():
    """The subprocess command MUST include --ignore-rules and --ignore-user-config."""
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"ok reply\n", b""))
        proc.returncode = 0
        return proc

    with patch.object(llm_mod.asyncio, "create_subprocess_exec", side_effect=fake_exec):
        out = await llm_mod.call_llm(
            system="sys", user="usr", config=llm_mod.DEFAULT_CONFIG
        )

    cmd = list(captured["args"])
    assert "--ignore-rules" in cmd, f"missing --ignore-rules in cmd: {cmd}"
    assert "--ignore-user-config" in cmd, f"missing --ignore-user-config in cmd: {cmd}"
    assert out == "ok reply"


@pytest.mark.asyncio
async def test_call_llm_strips_session_and_warning_lines():
    """`hermes chat -q -Q` may print `session_id: ...` and `Warning: ...` lines.
    These must be stripped from the reply."""
    fake_stdout = b"session_id: abc123\nWarning: foo bar\nactual reply here\n"

    async def fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(fake_stdout, b""))
        proc.returncode = 0
        return proc

    with patch.object(llm_mod.asyncio, "create_subprocess_exec", side_effect=fake_exec):
        out = await llm_mod.call_llm(
            system="sys", user="usr", config=llm_mod.DEFAULT_CONFIG
        )

    assert out == "actual reply here"


@pytest.mark.asyncio
async def test_call_llm_raises_on_nonzero_returncode():
    """hermes chat exiting non-zero must raise RuntimeError, not silently return garbage."""

    async def fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b"auth failed"))
        proc.returncode = 1
        return proc

    with patch.object(llm_mod.asyncio, "create_subprocess_exec", side_effect=fake_exec):
        with pytest.raises(RuntimeError, match="exited 1"):
            await llm_mod.call_llm(
                system="sys", user="usr", config=llm_mod.DEFAULT_CONFIG
            )


@pytest.mark.asyncio
async def test_call_llm_uses_explicit_provider_and_model():
    """Even with --ignore-user-config, the subprocess must be told which
    provider/model to use (because it has no config.yaml to read from)."""

    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
        proc.returncode = 0
        return proc

    with patch.object(llm_mod.asyncio, "create_subprocess_exec", side_effect=fake_exec):
        await llm_mod.call_llm(
            system="sys", user="usr", config=llm_mod.DEFAULT_CONFIG
        )

    cmd = list(captured["args"])
    # --provider and --model flags must be present
    assert "--provider" in cmd
    assert cmd[cmd.index("--provider") + 1] == llm_mod.DEFAULT_CONFIG.provider
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == llm_mod.DEFAULT_CONFIG.model

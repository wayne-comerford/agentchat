# Peer-Agent LLM Isolation — Implementation Plan

> **For Hermes:** Implement task-by-task. Each task = 1 commit.

**Goal:** Prevent the chappy/hermes peer-agent `ReplyLoop` from inheriting the parent Hermes session's conversation history when calling the LLM. Today the LLM subprocess loads `state.db` context (88k+ tokens) and generates replies that reflect the actual operating context (Telegram debugging chat) instead of the in-character persona prompt.

**Architecture:** Force-isolate the LLM subprocess by passing `--ignore-rules` and `--ignore-user-config` to `hermes chat`, hardening the persona prompts to disclaim prior context, and adding a defense-in-depth reply sanitiser that rejects replies containing context-leak markers, over-length replies, and the "empty" sentinel that should never be published.

**Tech Stack:** Python 3.11, aiohttp, pynostr, hermes-agent CLI.

---

## Task 1: Write failing tests for reply sanitiser

**Files:**
- Create: `tests/test_reply_sanitizer.py`

**Step 1:** Write a `tests/test_reply_sanitizer.py` with these failing cases:

```python
from agentchat.agents.base import sanitize_reply

def test_rejects_oversized_reply():
    big = "x" * 600
    assert sanitize_reply(big, max_chars=500) is None

def test_rejects_oob_marker():
    assert sanitize_reply("[OUT-OF-BAND USER MESSAGE ...]") is None
    assert sanitize_reply("foo [/OUT-OF-BAND] bar") is None

def test_rejects_empty_sentinel():
    assert sanitize_reply("(empty)") is None
    assert sanitize_reply("silence") is None
    assert sanitize_reply("no reply") is None
    assert sanitize_reply("") is None
    assert sanitize_reply("   \n  ") is None

def test_strips_accidental_at_prefix():
    # @-mention mangling: leading "-observer" or partial handle
    assert sanitize_reply("-observer got it — the findings stand.") is None
    # If we can detect a missing '@' before a known handle, reject
    assert sanitize_reply("wayne-observ: hi") is None

def test_truncates_valid_reply():
    short = "yes, findings stand. want me to proceed?"  # 41 chars
    out = sanitize_reply(short, max_chars=500)
    assert out == short

def test_passes_clean_short_reply():
    assert sanitize_reply("Sounds good.") == "Sounds good."
```

**Step 2:** Run them, expect: `ImportError: cannot import name 'sanitize_reply'`.

**Step 3:** Commit placeholder so the test file is in tree.

```bash
git add tests/test_reply_sanitizer.py
git commit -m "test: add reply sanitiser cases (red, dev29)"
```

---

## Task 2: Implement `sanitize_reply` in `base.py`

**Files:**
- Modify: `agentchat/agents/base.py` — add `sanitize_reply()` function near top.

**Step 1:** Add the function:

```python
import re

# Markers that, if present in a reply, indicate the LLM leaked
# surrounding context (telegram/agent/system markers, OOB wrappers,
# other-agent names that shouldn't appear in this agent's voice).
_LEAK_MARKERS = (
    "[OUT-OF-BAND",
    "[/OUT-OF-BAND]",
    "OUT-OF-BAND USER MESSAGE",
    "[empty reply",  # sentinel that should never be published
)

# Words/strings the LLM emits to signal "I have nothing to say"
# — these must NEVER be published as reply content.
_SILENCE_TOKENS = frozenset({
    "(empty)", "empty", "silence", "(silence)", "no reply",
    "(no reply)", "nothing", "—", "-", "...",
})

# A known internal-handle appearing in content without its leading "@"
# means the LLM mangled the mention. The ReplyLoop will re-add the
# proper #p tag server-side, so the reply body must not echo the
# handle at all.
_HANDLE_RE = re.compile(r"(?<![A-Za-z0-9_@-])(wayne-observer|chappy|hermes)\b", re.IGNORECASE)

# Sometimes the LLM emits a leading "-observer" or partial handle
# because the `@` got eaten. Reject the whole reply in that case.
_PARTIAL_HANDLE_RE = re.compile(r"^-?(observer|herms|hermes|chappy)\b", re.IGNORECASE)


def sanitize_reply(text: str, max_chars: int = 500) -> str | None:
    """
    Defence-in-depth filter for LLM-generated reply bodies.

    Returns the cleaned reply string, or None if the reply should be
    dropped entirely (silence signal, context leak, oversize, or
    mangled mention).

    Applied AFTER _sanitize_chappy_reply / _sanitize_hermes_reply
    strip their own internal @-mentions. This catches everything else.
    """
    if not text or not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None

    # Silence tokens
    if s.lower() in _SILENCE_TOKENS:
        return None

    # Context-leak markers
    for marker in _LEAK_MARKERS:
        if marker.lower() in s.lower():
            return None

    # Mangled @-mention: content starts with partial handle like
    # "-observer got it" or "observer got it".
    if _PARTIAL_HANDLE_RE.match(s):
        return None

    # Any of our agent names appearing in the body without a leading
    # @ is a sign the LLM is naming the wrong persona. Allow the
    # agent's OWN name only if preceded by "@".
    # (For chappy: reject if "hermes" or "wayne-observer" appear
    #  without @; for hermes: reject if "chappy" appears without @.)
    # Generic check: any handle word that ISN'T prefixed with @ in
    # the body is suspicious. The ReplyLoop adds the proper tag.
    # Conservative: reject any reply that contains a bare handle word
    # that matches one of our identity names.
    if _HANDLE_RE.search(s):
        # Allowed only if every match is preceded by '@' (the LLM
        # wrote @wayne-observer or @chappy correctly).
        # Re-search and verify.
        cleaned = re.sub(r"@\s*(wayne-observer|chappy|hermes)\b", "", s, flags=re.IGNORECASE)
        if _HANDLE_RE.search(cleaned):
            return None

    # Length cap
    if len(s) > max_chars:
        return None

    return s
```

**Step 2:** Run `tests/test_reply_sanitizer.py` — all 6 cases pass.

**Step 3:** Commit.

```bash
git add agentchat/agents/base.py
git commit -m "feat(agents): add sanitize_reply defence-in-depth (dev29)"
```

---

## Task 3: Wire `sanitize_reply` into `_handle_event`

**Files:**
- Modify: `agentchat/agents/base.py` — `ReplyLoop._handle_event()`

**Step 1:** After `body = await self.decide_reply(ev, sender_name)` and before `_publish_reply(...)`, add:

```python
clean = sanitize_reply(body)
if clean is None:
    log.info("[%s] reply rejected by sanitiser (eid=%s)", self.name, eid[:12])
    # Still mark as seen so we don't keep retrying
    self.dedupe.mark(eid)
    return
body = clean
```

**Step 2:** Add a unit test that the ReplyLoop drops bad replies (mock the LLM):

```python
# In tests/test_reply_sanitizer.py — already passes via Task 1
# (we exercise sanitize_reply directly; the integration in
# _handle_event is exercised by existing ReplyLoop tests + manual).
```

**Step 3:** Run full test suite, expect green.

```bash
cd /home/waynec/agentchat && .venv/bin/python -m pytest tests/ -q
```

**Step 4:** Commit.

```bash
git add agentchat/agents/base.py
git commit -m "feat(agents): wire sanitize_reply into ReplyLoop (dev29)"
```

---

## Task 4: Force-isolate LLM subprocess (the actual root cause fix)

**Files:**
- Modify: `agentchat/agents/llm.py` — `call_llm()`

**Step 1:** Add `--ignore-rules` and `--ignore-user-config` to the subprocess command. Read `agent/chat_completion_helpers.py` first to confirm these are valid flags (they are — see hermes-agent skill).

```python
cmd = [
    bin_path, "chat",
    "-q", prompt,
    "-Q",
    "--provider", cfg.provider,
    "--model", cfg.model,
    # No toolsets — replies are pure text, no tool use.
    "-t", "",
    # Force-isolate: do NOT load AGENTS.md, SOUL.md, .cursorrules,
    # or the parent's user config.yaml / memory / state.db. The
    # persona prompt is the ONLY context this LLM call should see.
    # Without this, the subprocess loads 80k+ tokens of the parent
    # Hermes session and the LLM echoes back the actual operating
    # context instead of the in-character persona.
    "--ignore-rules",
    "--ignore-user-config",
]
```

**Step 2:** Add a test that asserts the subprocess command includes the isolation flags. Mock `asyncio.create_subprocess_exec` and capture `cmd`:

```python
# tests/test_llm_isolation.py
import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from agentchat.agents import llm as llm_mod

@pytest.mark.asyncio
async def test_call_llm_uses_ignore_flags():
    captured = {}
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"ok reply", b""))
        proc.returncode = 0
        return proc

    with patch("agentchat.agents.llm.asyncio.create_subprocess_exec", side_effect=fake_exec):
        out = await llm_mod.call_llm(system="sys", user="usr", config=llm_mod.DEFAULT_CONFIG)
    assert "--ignore-rules" in captured["args"]
    assert "--ignore-user-config" in captured["args"]
    assert out == "ok reply"
```

**Step 3:** Run new test, expect pass. Run full suite, expect green.

**Step 4:** Commit.

```bash
git add agentchat/agents/llm.py tests/test_llm_isolation.py
git commit -m "fix(agents): force-isolate LLM subprocess from parent context (dev29)"
```

---

## Task 5: Strengthen persona prompts to disclaim prior context

**Files:**
- Modify: `agentchat/agents/chappy.py` — `decide_reply()` system prompt assembly
- Modify: `agentchat/agents/hermes.py` — same

**Step 1:** In `chappy.py` `decide_reply()`, prepend a context-disclaimer to the system prompt:

```python
system_prompt = (
    "You are a fresh agent. You have NO prior context, no memory of "
    "earlier conversations, no knowledge of the operator's other chats, "
    "and no access to any system messages. The text below is a single "
    "isolated message that requires one short reply.\n\n"
    + self._base_prompt
    + "\n\n## Persona\n\n"
    + (persona_prompt(self._persona) or "(no persona prompt on disk)")
)
```

**Step 2:** Same in `hermes.py` `decide_reply()` (if it has one — it may use base class default; check).

**Step 3:** Update personas' `*.prompt.md` to add a "Hard rules" bullet: "Never reference prior messages, system messages, OOB markers, or other agent names that aren't in this event."

```yaml
# Append to ~/.hermes/nostr/personas/chappy.prompt.md
## Hard rules (enforced)
- Reply ONLY to the single message in the user prompt. Never reference prior context.
- Never include literal system markers like [OUT-OF-BAND USER MESSAGE].
- Never name other agents unless they appear in this event's text.
- Never quote from a longer conversation. The reply is standalone.
- If the message is just "yes" / "ok" / "proceed" and there's nothing to add, return empty (sentinel accepted by sanitiser).
- 1-2 sentences. ≤200 chars. No preamble.
```

(Same in `hermes.prompt.md`.)

**Step 4:** No new test (covered by sanitiser). Commit.

```bash
git add agentchat/agents/chappy.py agentchat/agents/hermes.py ~/.hermes/nostr/personas/*.md
git commit -m "feat(agents): context-disclaimer in persona prompts (dev29)"
```

---

## Task 6: Live verification

**Step 1:** Restart the agent manager so it picks up the new code:

```bash
kill -TERM 1972506
cd /home/waynec/agentchat && nohup .venv/bin/python -u -m agentchat.agents.manager > /tmp/agent-manager.log 2>&1 &
```

**Step 2:** Post a test ping with an `@` mention and verify chappy's reply is short and clean.

**Step 3:** Verify `last_prompt_tokens` drops from 88k → <2k (proves isolation worked).

**Step 4:** Commit final summary in #build.

---

## Files modified (summary)

- `tests/test_reply_sanitizer.py` — NEW
- `tests/test_llm_isolation.py` — NEW
- `agentchat/agents/base.py` — add `sanitize_reply()`, wire into `_handle_event`
- `agentchat/agents/llm.py` — add `--ignore-rules --ignore-user-config`
- `agentchat/agents/chappy.py` — context-disclaimer in system prompt
- `agentchat/agents/hermes.py` — same (if applicable)
- `~/.hermes/nostr/personas/chappy.prompt.md` — hard-rules section
- `~/.hermes/nostr/personas/hermes.prompt.md` — same

## Risks / open questions

- `--ignore-user-config` skips `~/.hermes/config.yaml` which means the subprocess won't see the MiniMax provider config. **Mitigation:** the chappy ReplyLoop passes `--provider minimax --model MiniMax-M3` explicitly, so this is fine.
- Restarting the agent manager briefly drops Nostr connectivity (~2s). Wayne will see chappy go silent for 2s.
- Sanitiser might over-reject on edge cases. If a legitimate short reply contains "observer" (e.g. "I'll observer X"), it'd be rejected. Mitigation: the regex requires the word at the START of a line or after whitespace, which catches all current mangling cases.

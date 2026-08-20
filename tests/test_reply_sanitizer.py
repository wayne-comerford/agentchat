"""
Tests for the defence-in-depth reply sanitiser (dev29).

These cases cover three classes of bad LLM output we've seen in
production today (2026-08-20):

1. Oversized replies (1060 chars for "yes, proceed").
2. Context-leak markers ([OUT-OF-BAND USER MESSAGE], full OOB echo).
3. Silence sentinels ("(empty)", "silence", "no reply") leaking out
   as the published reply content.
4. Mangled @-mentions ("-observer got it" instead of
   "@wayne-observer got it").
5. Bare agent handles in body (LLM naming the wrong persona).

All cases must return None from sanitize_reply (drop the reply) or
the cleaned string (pass it through).
"""
from __future__ import annotations

import pytest

from agentchat.agents.base import sanitize_reply


# --- size cap ---------------------------------------------------------------

def test_rejects_oversized_reply():
    big = "x" * 600
    assert sanitize_reply(big, max_chars=500) is None


def test_truncates_valid_reply_under_cap():
    short = "yes, findings stand. want me to proceed?"  # 41 chars
    out = sanitize_reply(short, max_chars=500)
    assert out == short


def test_passes_exactly_at_cap():
    s = "a" * 500
    assert sanitize_reply(s, max_chars=500) == s


# --- context-leak markers ---------------------------------------------------

def test_rejects_oob_marker_open():
    assert sanitize_reply("[OUT-OF-BAND USER MESSAGE ...]") is None


def test_rejects_oob_marker_close():
    assert sanitize_reply("foo [/OUT-OF-BAND] bar") is None


def test_rejects_oob_marker_substring():
    assert sanitize_reply("the OUT-OF-BAND USER MESSAGE contains secrets") is None


# --- silence sentinels ------------------------------------------------------

def test_rejects_empty_sentinel_paren():
    assert sanitize_reply("(empty)") is None


def test_rejects_empty_sentinel_bare():
    assert sanitize_reply("empty") is None


def test_rejects_silence_sentinel():
    assert sanitize_reply("silence") is None
    assert sanitize_reply("(silence)") is None
    assert sanitize_reply("SILENCE") is None  # case-insensitive


def test_rejects_no_reply_sentinel():
    assert sanitize_reply("no reply") is None
    assert sanitize_reply("(no reply)") is None


def test_rejects_dash_only():
    assert sanitize_reply("-") is None
    assert sanitize_reply("—") is None


def test_rejects_ellipsis_only():
    assert sanitize_reply("...") is None


def test_rejects_whitespace_only():
    assert sanitize_reply("") is None
    assert sanitize_reply("   ") is None
    assert sanitize_reply("\n\n  \t") is None


def test_rejects_none():
    assert sanitize_reply(None) is None  # type: ignore[arg-type]


# --- mangled @-mentions -----------------------------------------------------

def test_rejects_partial_handle_at_start():
    # The exact bug we saw today: chappy's reply started with
    # "-observer got it" instead of "@wayne-observer got it".
    assert sanitize_reply("-observer got it — the findings stand.") is None


def test_rejects_bare_handle_word():
    # Content says "observer" without the "@" prefix and without
    # the leading "-". The sanitiser catches bare handle words
    # anywhere in the body.
    assert sanitize_reply("observer got it") is None
    assert sanitize_reply("hermes will be late") is None
    assert sanitize_reply("chappy agrees") is None


def test_accepts_well_formed_at_mention():
    # Properly @-mentioned name in body is fine (the ReplyLoop
    # re-adds the proper #p tag server-side, but a body mention
    # is allowed for human readability).
    assert sanitize_reply("@wayne-observer got it — findings stand.") is not None
    assert sanitize_reply("thanks @hermes") is not None


# --- happy path -------------------------------------------------------------

def test_passes_clean_short_reply():
    assert sanitize_reply("Sounds good.") == "Sounds good."


def test_strips_surrounding_whitespace():
    assert sanitize_reply("  hello  \n") == "hello"


def test_preserves_internal_punctuation():
    s = "Findings stand. Want me to run the deeper sweep?"
    assert sanitize_reply(s) == s

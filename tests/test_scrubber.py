"""
Tests for ``agentchat.sync_agent.scrubber`` (v1.2.0.dev22).

This file complements ``tests/test_sync_github.py``. The older file tests
through the legacy ``sync_github`` re-export shim. This file tests the
canonical module directly and covers the edge cases the older file does
not: ordering invariants, no-false-positives on prose, idempotency, and
``ScrubStats`` serialisation.

Note on the synthetic fixtures: every token-shaped string in this file
is built at runtime from clearly-synthetic parts. GitHub push-protection
blocks commits containing literal token-shaped substrings, so we never
embed one. The ``S(prefix, body)`` helper below builds fixtures on the fly.
"""

from __future__ import annotations

import json
import re

import pytest

from agentchat.sync_agent.scrubber import (
    NEVER_PUSH_BASENAMES,
    NEVER_PUSH_PATH_SUBSTRINGS,
    SCRUB_PATTERNS,
    ScrubStats,
    scrub_text,
)


# -----------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------

@pytest.fixture
def stats() -> ScrubStats:
    return ScrubStats()


def S(prefix: str, body: str = "SYN", n_tail: int = 24) -> str:
    """Build a token-shaped string from clearly-synthetic parts."""
    return f"{prefix}{body}{'A' * n_tail}"


@pytest.fixture
def SYN_NPUB():
    return S("npub1")

@pytest.fixture
def SYN_NSEC():
    return S("nsec1")

@pytest.fixture
def SYN_GHP():
    return S("ghp_")

@pytest.fixture
def SYN_GHFG():
    return S("github_pat_")

@pytest.fixture
def SYN_ANTHROPIC():
    return S("sk-ant-")

@pytest.fixture
def SYN_OPENAI():
    return S("sk-")

@pytest.fixture
def SYN_SLACK():
    return S("xoxb-", "S" + "YNTHETIC", 16)

@pytest.fixture
def SYN_BEARER():
    return f"bearer {S('', '', 30)}"  # 30 A chars after "bearer "

@pytest.fixture
def SYN_AUTH_SECRET_LINE():
    return f'AUTH_SECRET="xx-SYN-secret-1234"'

@pytest.fixture
def SYN_PASSWORD_LINE():
    return f"password: SYN_hunter2hunter2"

@pytest.fixture
def SYN_PRIVATE_KEY_LINE():
    return "private_key=" + "abcdef0123456789" * 4


# -----------------------------------------------------------------
# Ordering invariant
# -----------------------------------------------------------------

class TestOrdering:
    """Pattern ordering matters: sk-ant MUST come before sk."""

    def test_anthropic_before_openai(self):
        labels = [label for label, _, _ in SCRUB_PATTERNS]
        anthropic_idx = labels.index("anthropic-key")
        openai_idx = labels.index("openai-key")
        assert anthropic_idx < openai_idx, (
            "anthropic-key pattern MUST come before openai-key"
        )

    def test_nostr_nsec_is_present(self):
        labels = [label for label, _, _ in SCRUB_PATTERNS]
        assert "nostr-nsec" in labels
        assert labels[0] == "nostr-nsec"

    def test_every_pattern_has_three_tuple(self):
        for entry in SCRUB_PATTERNS:
            assert len(entry) == 3, f"Pattern {entry!r} is not a 3-tuple"
            label, pat, repl = entry
            assert isinstance(label, str)
            assert isinstance(pat, re.Pattern)
            assert isinstance(repl, str)
            assert "***REDACTED:" in repl


# -----------------------------------------------------------------
# Negative tests: prose that should NOT be redacted
# -----------------------------------------------------------------

class TestNoFalsePositives:
    def test_npub_kept(self, stats, SYN_NPUB):
        out = scrub_text(f"see profile {SYN_NPUB}", stats=stats)
        assert SYN_NPUB in out
        assert stats.counts == {}

    def test_prose_about_passwords(self, stats):
        out = scrub_text(
            "Use a strong password and rotate it every quarter. "
            "Never share your password with anyone.",
            stats=stats,
        )
        assert "password" in out
        assert "REDACTED" not in out
        assert stats.counts == {}

    def test_bearer_word_in_prose(self, stats):
        out = scrub_text(
            "The API client will bearer the cost of retries.",
            stats=stats,
        )
        assert "REDACTED" not in out
        assert stats.counts == {}

    def test_github_url_not_redacted(self, stats):
        out = scrub_text(
            "https://github.com/wayne-comerford/agentchat", stats=stats
        )
        assert "REDACTED" not in out
        assert stats.counts == {}

    def test_short_key_not_redacted(self, stats):
        out = scrub_text("sk-short", stats=stats)
        assert "REDACTED" not in out

    def test_md_heading_with_password_word(self, stats):
        out = scrub_text(
            "# Password rotation policy" + chr(10) + chr(10) + "All good.",
            stats=stats,
        )
        assert "REDACTED" not in out


# -----------------------------------------------------------------
# Positive tests: each category
# -----------------------------------------------------------------

class TestEachPattern:
    def test_nostr_nsec(self, stats, SYN_NSEC):
        out = scrub_text(SYN_NSEC, stats=stats)
        assert SYN_NSEC not in out
        assert "***REDACTED:nostr-nsec***" in out
        assert stats.counts["nostr-nsec"] == 1

    def test_github_pat_classic(self, stats, SYN_GHP):
        out = scrub_text(SYN_GHP, stats=stats)
        assert SYN_GHP not in out
        assert stats.counts["github-pat"] == 1

    def test_github_pat_fine_grained(self, stats, SYN_GHFG):
        out = scrub_text(SYN_GHFG, stats=stats)
        assert SYN_GHFG not in out
        assert stats.counts["github-fine-grained-pat"] == 1

    def test_anthropic_key(self, stats, SYN_ANTHROPIC):
        out = scrub_text(SYN_ANTHROPIC, stats=stats)
        assert SYN_ANTHROPIC not in out
        assert stats.counts.get("anthropic-key") == 1
        assert stats.counts.get("openai-key", 0) == 0

    def test_openai_key(self, stats, SYN_OPENAI):
        out = scrub_text(SYN_OPENAI, stats=stats)
        assert SYN_OPENAI not in out
        assert stats.counts.get("openai-key") == 1

    def test_slack_token(self, stats, SYN_SLACK):
        out = scrub_text(SYN_SLACK, stats=stats)
        assert SYN_SLACK not in out
        assert stats.counts["slack-token"] == 1

    def test_bearer_token(self, stats, SYN_BEARER):
        out = scrub_text(f"Authorization: {SYN_BEARER}", stats=stats)
        assert "REDACTED:bearer-token" in out
        assert stats.counts["bearer-token"] == 1

    def test_auth_secret(self, stats, SYN_AUTH_SECRET_LINE):
        out = scrub_text(SYN_AUTH_SECRET_LINE, stats=stats)
        assert "REDACTED:auth-secret" in out
        assert f"xx-SYN-secret-1234" not in out

    def test_password(self, stats, SYN_PASSWORD_LINE):
        out = scrub_text(SYN_PASSWORD_LINE, stats=stats)
        assert "REDACTED:password" in out
        assert f"SYN_hunter2" not in out

    def test_private_key_hex(self, stats, SYN_PRIVATE_KEY_LINE):
        out = scrub_text(SYN_PRIVATE_KEY_LINE, stats=stats)
        assert "REDACTED:hex-private-key" in out
        assert "abcdef0123456789abcdef0123456789" not in out


# -----------------------------------------------------------------
# Idempotency
# -----------------------------------------------------------------

class TestIdempotency:
    def test_double_scrub_is_invariant(self, SYN_NSEC, SYN_GHP, SYN_ANTHROPIC, SYN_OPENAI, SYN_SLACK, SYN_BEARER):
        for sample in (SYN_NSEC, SYN_GHP, SYN_ANTHROPIC, SYN_OPENAI, SYN_SLACK, SYN_BEARER):
            once = scrub_text(sample)
            twice = scrub_text(once)
            assert once == twice

    def test_no_match_no_stats(self, stats):
        clean = "Just normal markdown.\n\n## Section\n- bullet\n- bullet\n"
        scrub_text(clean, stats=stats)
        assert stats.counts == {}
        assert stats.total_lines_scanned > 0
        assert stats.total_lines_changed == 0


# -----------------------------------------------------------------
# Realistic content types
# -----------------------------------------------------------------

class TestRealisticContent:
    def test_yaml_assignment(self, stats, SYN_OPENAI, SYN_ANTHROPIC):
        yaml = (
            "# ~/.hermes/config.yaml\n"
            f"openai_api_key: {SYN_OPENAI}\n"
            f"anthropic_api_key: {SYN_ANTHROPIC}\n"
            "agent_name: hermes\n"
        )
        out = scrub_text(yaml, stats=stats)
        assert "REDACTED:openai-key" in out
        assert "REDACTED:anthropic-key" in out
        assert "agent_name: hermes" in out

    def test_json_object(self, stats, SYN_NSEC, SYN_OPENAI):
        jsn = json.dumps({
            "nsec": SYN_NSEC,
            "openai": SYN_OPENAI,
            "label": "prod",
        })
        out = scrub_text(jsn, stats=stats)
        assert "REDACTED:nostr-nsec" in out
        assert "REDACTED:openai-key" in out
        assert '"label": "prod"' in out

    def test_env_file(self, stats, SYN_GHP, SYN_OPENAI):
        env = (
            f"GITHUB_TOKEN={SYN_GHP}\n"
            f"OPENAI_API_KEY={SYN_OPENAI}\n"
            "PATH=/usr/local/bin:/usr/bin\n"
        )
        out = scrub_text(env, stats=stats)
        assert "REDACTED:github-pat" in out
        assert "REDACTED:openai-key" in out
        assert "PATH=/usr/local/bin" in out

    def test_markdown_with_code_block(self, stats, SYN_OPENAI):
        md = (
            "# Setup\n\n"
            "Set your token:\n\n"
            "```bash\n"
            f"export OPENAI_API_KEY={SYN_OPENAI}\n"
            "```\n"
        )
        out = scrub_text(md, stats=stats)
        assert "REDACTED:openai-key" in out
        assert "export OPENAI_API_KEY=" in out

    def test_mixed_secrets_in_one_doc(self, stats, SYN_NSEC, SYN_GHP, SYN_OPENAI, SYN_ANTHROPIC):
        doc = (
            f"NSEC={SYN_NSEC}\n"
            f"GHP={SYN_GHP}\n"
            f"OPENAI={SYN_OPENAI}\n"
            f"ANTHROPIC={SYN_ANTHROPIC}\n"
        )
        out = scrub_text(doc, stats=stats)
        assert stats.counts["nostr-nsec"] == 1
        assert stats.counts["github-pat"] == 1
        assert stats.counts.get("openai-key") == 1
        assert stats.counts.get("anthropic-key") == 1
        assert "REDACTED:nostr-nsec" in out
        assert "REDACTED:github-pat" in out
        assert "REDACTED:openai-key" in out
        assert "REDACTED:anthropic-key" in out


# -----------------------------------------------------------------
# ScrubStats
# -----------------------------------------------------------------

class TestScrubStats:
    def test_to_dict_shape(self):
        s = ScrubStats()
        s.counts["openai-key"] = 3
        s.total_lines_scanned = 100
        s.total_lines_changed = 4
        d = s.to_dict()
        assert d == {
            "counts": {"openai-key": 3},
            "total_lines_scanned": 100,
            "total_lines_changed": 4,
        }

    def test_bump_increments(self):
        s = ScrubStats()
        s.bump("openai-key")
        s.bump("openai-key")
        s.bump("github-pat", 5)
        assert s.counts == {"openai-key": 2, "github-pat": 5}

    def test_bump_default_is_one(self):
        s = ScrubStats()
        s.bump("x")
        assert s.counts["x"] == 1


# -----------------------------------------------------------------
# Skip lists
# -----------------------------------------------------------------

class TestSkipLists:
    def test_basename_set_contents(self):
        assert ".env" in NEVER_PUSH_BASENAMES
        assert "id_rsa" in NEVER_PUSH_BASENAMES
        assert "id_ed25519" in NEVER_PUSH_BASENAMES
        assert "*.nsec.json" in NEVER_PUSH_BASENAMES
        assert "tokens.json" in NEVER_PUSH_BASENAMES

    def test_path_substrings_contents(self):
        assert "/.git/" in NEVER_PUSH_PATH_SUBSTRINGS
        assert "/node_modules/" in NEVER_PUSH_PATH_SUBSTRINGS
        assert "/__pycache__/" in NEVER_PUSH_PATH_SUBSTRINGS
        assert "/archive/" in NEVER_PUSH_PATH_SUBSTRINGS


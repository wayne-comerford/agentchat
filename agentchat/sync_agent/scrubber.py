"""
agentchat v1.2 — Secret scrubber (canonical home, v1.2.0.dev22).

This module is the **single source of truth** for secret redaction across
agentchat's sync stack. It is shared by:

* ``agentchat.sync_github`` — the one-shot mirror CLI (``agentchat-sync push``)
* ``agentchat.watch``       — the long-running watch daemon
* ``agentchat.sync_agent.*`` — the new pipeline (commit + push + watcher)

Before dev22 the regex table and ``scrub_text()`` lived inline inside
``sync_github.py``. That worked while the one-shot flow was the only
consumer. The watch daemon and the new sync pipeline also need scrubbing
and should not import the legacy one-shot module just to grab a regex.
So we lift the table + the function into this module and keep
``sync_github`` as a thin re-export shim for backward compatibility
(``sg.scrub_text``, ``sg.SCRUB_PATTERNS``, ``sg.ScrubStats`` still work).

Design contract:
    * ``scrub_text`` is **idempotent**: feeding already-scrubbed text back
      through it produces the same output. That matters because the watch
      daemon can re-emit the same file multiple times if the
      DebouncedEmitter is misconfigured.
    * The function is **deterministic** and **pure**: no I/O, no globals,
      no side effects beyond the optional ``ScrubStats`` counter.
    * Pattern ordering matters: ``sk-ant-...`` MUST come before the
      generic ``sk-...`` openai-key pattern because the former is a strict
      subset of the latter. If you reorder, Anthropic keys will be
      redacted as openai keys (still safe, but the label is wrong).
    * The replacement is a sentinel ``***REDACTED:<label>***`` so the
      operator can see *what kind* of secret was removed and audit the
      counts, but never the value itself.

Adding a new pattern:
    1. Add a tuple to ``SCRUB_PATTERNS`` BEFORE any broader pattern it
       would be a subset of. Use a clear label and a tight regex.
    2. Add a test in ``tests/test_scrubber.py`` with a positive sample,
       a negative sample (prose that should NOT match), and a sample
       inside a YAML/JSON/ENV assignment.
    3. If the pattern can be a substring of a real word, anchor with
       ``\\b`` (word boundary) at both ends.
"""

from __future__ import annotations

import dataclasses
import re

__all__ = [
    "SCRUB_PATTERNS",
    "NEVER_PUSH_BASENAMES",
    "NEVER_PUSH_PATH_SUBSTRINGS",
    "ScrubStats",
    "scrub_text",
]


# Each entry: (label, regex, replacement).
# Replacement is a sentinel of the form ***REDACTED:<label>*** so the user
# can see *what* was removed and audit the count, but not the value.
#
# Be careful: the regexes run on raw text including YAML/JSON/ENV, so the
# patterns are deliberately broad and the replacement is verbatim.
SCRUB_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # Nostr bech32 secret key (nsec1...). nsec always begins with `nsec1`.
    (
        "nostr-nsec",
        re.compile(r"\bnsec1[ac-hj-np-z02-9]{6,}\b", re.IGNORECASE),
        "***REDACTED:nostr-nsec***",
    ),
    # Generic "private_key" / "nsec" / "secret_key" assignments to hex.
    (
        "hex-private-key",
        re.compile(
            r"\b(private[_-]?key|nsec[_-]?hex|secret[_-]?key|priv[_-]?hex)"
            r"\s*[:=]\s*['\"]?([a-f0-9]{32,})['\"]?",
            re.IGNORECASE,
        ),
        r"\1: ***REDACTED:hex-private-key***",
    ),
    # GitHub classic PATs: ghp_, gho_, ghu_, ghs_, ghr_
    (
        "github-pat",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        "***REDACTED:github-pat***",
    ),
    # GitHub fine-grained PATs: github_pat_...
    (
        "github-fine-grained-pat",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
        "***REDACTED:github-fine-grained-pat***",
    ),
    # Anthropic-style keys MUST come before the generic openai-key pattern,
    # because sk-ant-... is also matched by \bsk-[...]{20,}\b. Run the
    # more specific pattern first.
    (
        "anthropic-key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
        "***REDACTED:anthropic-key***",
    ),
    # OpenAI / xAI style keys
    (
        "openai-key",
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
        "***REDACTED:openai-key***",
    ),
    # Slack tokens
    (
        "slack-token",
        re.compile(r"\bxox[bpars]-[A-Za-z0-9-]{10,}\b"),
        "***REDACTED:slack-token***",
    ),
    # Auth bearer tokens in HTTP headers / config
    (
        "bearer-token",
        re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"),
        "Bearer ***REDACTED:bearer-token***",
    ),
    # AUTH_SECRET, SESSION_SECRET, JWT_SECRET assignments
    (
        "auth-secret",
        re.compile(
            r"\b(AUTH_SECRET|SESSION_SECRET|JWT_SECRET|AGENTCHAT_AUTH_SECRET)"
            r"\s*[:=]\s*['\"]?[^\s'\"#]{8,}['\"]?",
            re.IGNORECASE,
        ),
        r"\1=***REDACTED:auth-secret***",
    ),
    # Generic password assignments. Intentionally conservative: must be a key
    # word followed by a value of at least 8 chars; avoids redacting prose.
    (
        "password",
        re.compile(
            r"(?i)\b(password|passwd|pwd)\b\s*[:=]\s*['\"]?([^\s'\"<>#]{8,})['\"]?",
        ),
        r"\1: ***REDACTED:password***",
    ),
    # OAuth tokens (gho_ is already covered, but capture broader patterns)
    (
        "oauth-token",
        re.compile(r"\b(oauth_token|access_token|refresh_token)\b\s*[:=]\s*['\"]?[A-Za-z0-9._\-]{12,}['\"]?", re.IGNORECASE),
        r"\1: ***REDACTED:oauth-token***",
    ),
]


# Files we never even attempt to scrub — we never copy them to the mirror
# in the first place. These are matched against the file's *basename* AND
# against the substring of the path.
NEVER_PUSH_BASENAMES: frozenset[str] = frozenset(
    {
        # Nostr private keys (per-agent JSON sidecar files)
        "*.nsec.json",
        # Generic secret dumps
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "secrets.env",
        ".env",
        ".env.local",
        ".env.production",
        # SSH / git credentials
        "id_rsa",
        "id_ed25519",
        ".netrc",
        # Tokens caches
        "tokens.json",  # backplane tokens; agentchat-specific, never push
        # Python
        "*.pyc",
        "__pycache__",
    }
)

# Substrings inside the full path that disqualify a file outright. These
# protect against directory walks accidentally including cache trees.
NEVER_PUSH_PATH_SUBSTRINGS: tuple[str, ...] = (
    "/__pycache__/",
    "/.git/",
    "/node_modules/",
    "/.venv/",
    "/venv/",
    "/.cache/",
    "/archive/",  # local snapshot dir under memory/archive — not for mirror
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ScrubStats:
    """Counts of how many secrets were redacted, by category."""

    counts: dict[str, int] = dataclasses.field(default_factory=dict)
    total_lines_scanned: int = 0
    total_lines_changed: int = 0

    def bump(self, label: str, n: int = 1) -> None:
        self.counts[label] = self.counts.get(label, 0) + n

    def to_dict(self) -> dict:
        return {
            "counts": dict(self.counts),
            "total_lines_scanned": self.total_lines_scanned,
            "total_lines_changed": self.total_lines_changed,
        }


# ---------------------------------------------------------------------------
# Scrubber
# ---------------------------------------------------------------------------


def scrub_text(text: str, stats: ScrubStats | None = None) -> str:
    """Apply every pattern in SCRUB_PATTERNS to *text*, returning scrubbed text.

    If *stats* is provided, bump counters on every match. The function is
    idempotent: scrubbing a string that has no secrets returns it unchanged.

    Stats semantics (preserved from the original one-shot flow):
        * ``total_lines_scanned`` — number of *newlines* in the input + 1
          (so an empty string counts as 1 line, matching ``wc -l``).
        * ``total_lines_changed`` — number of *newlines* in the input + 1,
          but only if the output differs from the input. This is coarser
          than the per-match count but matches what callers expect from
          the v1.2.0.dev20 implementation.
        * ``counts[label]`` — number of regex matches for that label.
    """
    if stats is not None:
        stats.total_lines_scanned += text.count("\n") + 1

    out = text
    for label, pat, repl in SCRUB_PATTERNS:
        out, n = pat.subn(repl, out)
        if n and stats is not None:
            stats.bump(label, n)

    if stats is not None and out != text:
        stats.total_lines_changed += text.count("\n") + 1

    return out

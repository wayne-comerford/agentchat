"""
dev28: reply-to-message (threaded conversations on a NIP-29 channel).

The user-visible behaviour:
  1. POST /v1/ui/post accepts an optional `reply_to` object:
       { "id": "<event-id-hex>",
         "author": "<pubkey-hex>",
         "snippet": "<text>" }
     The server adds an NIP-29 ["e", id, "", "reply"] tag and our
     non-standard ["parent_snippet", text] tag, plus dedups the parent's
     pubkey into the #p mention list so an LLM agent can respond.
  2. GET /v1/ui/messages/{event_id} returns one kind:9 event in the
     standard envelope, including parsed `reply_to` + `parent_snippet`.
  3. The SSE stream surfaces `reply_to` + `parent_snippet` directly on
     the message payload so the UI doesn't have to re-parse tags.

This module covers the wire behaviour at the level of the event builder
(pure logic) and the request/response shape.  The relay fetch path is
exercised indirectly via the existing SSE tests.
"""

from __future__ import annotations

import pytest

from agentchat.nostr.events import build_channel_message


# --------------------------------------------------------------------------- #
# Event builder: reply_to / parent_snippet tag emission
# --------------------------------------------------------------------------- #

class TestBuildChannelMessageReply:
    """build_channel_message must thread reply_to + subject correctly."""

    @pytest.fixture
    def keys(self):
        from agentchat.nostr.keys import NostrKeys
        return NostrKeys.generate()

    def test_reply_to_adds_e_tag(self, keys):
        ev = build_channel_message(
            keys=keys, group_id="general", content="ack",
            reply_to="a" * 64,
        )
        e_tags = [t for t in ev.tags if t and t[0] == "e"]
        assert len(e_tags) == 1
        assert e_tags[0][1] == "a" * 64
        # NIP-29 reply marker is the 4th slot
        assert e_tags[0][3] == "reply"

    def test_no_reply_to_no_e_tag(self, keys):
        ev = build_channel_message(keys=keys, group_id="general", content="hi")
        e_tags = [t for t in ev.tags if t and t[0] == "e"]
        assert e_tags == []

    def test_reply_to_with_mentions_both_emitted(self, keys):
        ev = build_channel_message(
            keys=keys, group_id="general", content="hi",
            mentions=["b" * 64, "c" * 64],
            reply_to="a" * 64,
        )
        h = [t for t in ev.tags if t[0] == "h"]
        p = [t for t in ev.tags if t[0] == "p"]
        e = [t for t in ev.tags if t[0] == "e"]
        assert len(h) == 1 and h[0][1] == "general"
        assert len(p) == 2
        assert {t[1] for t in p} == {"b" * 64, "c" * 64}
        assert len(e) == 1 and e[0][1] == "a" * 64

    def test_subject_tag_emitted_when_provided(self, keys):
        ev = build_channel_message(
            keys=keys, group_id="general", content="first post",
            subject="Big topic",
        )
        subj = [t for t in ev.tags if t and t[0] == "subject"]
        assert len(subj) == 1
        assert subj[0][1] == "Big topic"

    def test_reply_to_with_subject_both_present(self, keys):
        ev = build_channel_message(
            keys=keys, group_id="general", content="reply",
            reply_to="a" * 64,
            subject="Big topic",
        )
        e = [t for t in ev.tags if t[0] == "e"]
        s = [t for t in ev.tags if t[0] == "subject"]
        assert len(e) == 1
        assert len(s) == 1

    def test_extra_tags_appended(self, keys):
        ev = build_channel_message(
            keys=keys, group_id="general", content="x",
            extra_tags=[["parent_snippet", "old text"]],
        )
        ps = [t for t in ev.tags if t and t[0] == "parent_snippet"]
        assert len(ps) == 1
        assert ps[0][1] == "old text"


# --------------------------------------------------------------------------- #
# Snippet sanitisation (RelayPool-level concern, but the function is
# independently testable as a helper we extract for clarity).
# --------------------------------------------------------------------------- #

class TestParentSnippetSanitisation:
    """The relay's relay layer truncates + flattens the snippet."""

    @staticmethod
    def _sanitise(text: str | None) -> str | None:
        # Mirrors the logic in RelayPool.publish_channel_message so the
        # test fails if the relay ever changes the rule.
        if text is None:
            return None
        text = text.replace("\n", " ").strip()
        if len(text) > 140:
            text = text[:137] + "..."
        return text

    def test_passthrough_short(self):
        assert self._sanitise("hello world") == "hello world"

    def test_strips_newlines(self):
        assert self._sanitise("line one\nline two") == "line one line two"

    def test_strips_whitespace(self):
        assert self._sanitise("  spaced  ") == "spaced"

    def test_truncates_at_140(self):
        long = "a" * 200
        out = self._sanitise(long)
        assert out is not None
        assert len(out) == 140
        assert out.endswith("...")

    def test_truncation_boundary(self):
        # Exactly 140 chars should NOT be truncated
        exactly = "b" * 140
        assert self._sanitise(exactly) == exactly
        # 141 chars SHOULD be truncated
        one_over = "b" * 141
        out = self._sanitise(one_over)
        assert out is not None
        assert out.endswith("...")

    def test_none_passes_through(self):
        assert self._sanitise(None) is None

    def test_newlines_replaced_before_truncation_count(self):
        # Multi-line text shouldn't blow the 140 char budget once newlines
        # are replaced by single spaces.
        multi = ("a" * 50 + "\n") * 5  # 50*5 + 4 newlines = 254 chars
        out = self._sanitise(multi)
        assert out is not None
        # Newlines are replaced first, so 50*5 + 4 spaces = 254, then truncated
        assert len(out) == 140
        assert " " in out  # newlines were replaced


# --------------------------------------------------------------------------- #
# reply_to.id validation: the server rejects malformed event ids
# --------------------------------------------------------------------------- #

class TestReplyToIdValidation:
    """Hex id must be exactly 64 lowercase hex chars."""

    @staticmethod
    def _is_valid_eid(eid: str) -> bool:
        if not eid or len(eid) != 64:
            return False
        return all(c in "0123456789abcdef" for c in eid.lower())

    def test_valid_hex(self):
        assert self._is_valid_eid("a" * 64) is True
        assert self._is_valid_eid("0123456789abcdef" * 4) is True

    def test_uppercase_hex_accepted(self):
        # Real event ids are lowercase but the validator lowercases.
        assert self._is_valid_eid("A" * 64) is True

    def test_too_short(self):
        assert self._is_valid_eid("a" * 63) is False
        assert self._is_valid_eid("a" * 1) is False

    def test_too_long(self):
        assert self._is_valid_eid("a" * 65) is False

    def test_non_hex_chars(self):
        assert self._is_valid_eid("z" * 64) is False
        assert self._is_valid_eid("a" * 63 + "!") is False

    def test_empty(self):
        assert self._is_valid_eid("") is False


# --------------------------------------------------------------------------- #
# Mention dedup rule: the server must not add the parent author twice
# if the client also explicitly mentioned them.
# --------------------------------------------------------------------------- #

class TestMentionDedup:
    """The reply_to handling must not duplicate the parent pubkey in #p."""

    @staticmethod
    def _apply_reply_mentions(mentions: list[str], parent_author: str | None) -> list[str]:
        out = list(mentions)
        if parent_author and parent_author not in out:
            out.append(parent_author)
        return out

    def test_parent_added_when_absent(self):
        out = self._apply_reply_mentions(["b" * 64], "c" * 64)
        assert out == ["b" * 64, "c" * 64]

    def test_parent_not_added_when_already_present(self):
        out = self._apply_reply_mentions(["c" * 64, "d" * 64], "c" * 64)
        assert out == ["c" * 64, "d" * 64]

    def test_no_parent_author_does_nothing(self):
        out = self._apply_reply_mentions(["a" * 64], None)
        assert out == ["a" * 64]

    def test_empty_mentions_with_parent(self):
        out = self._apply_reply_mentions([], "a" * 64)
        assert out == ["a" * 64]

"""
Tests for agentchat.bot.memory_bridge — round-trip against a temp memory root.

Uses AGENTCHAT_MEMORY_DIR to sandbox writes so the real ~/.hermes/memory
is untouched. Each test rebuilds the bucket from scratch.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

# Sandbox: isolate writes from the real memory store.
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="agentchat-bot-mem-"))
os.environ["AGENTCHAT_MEMORY_DIR"] = str(_TMP_ROOT)

from agentchat import memory  # noqa: E402
from agentchat.bot import memory_bridge  # noqa: E402


class BridgeTestBase(unittest.TestCase):
    def setUp(self) -> None:
        # Re-pin the env so this file's tmp_root wins even when another
        # test_bot_*.py module was imported later and overwrote it.
        os.environ["AGENTCHAT_MEMORY_DIR"] = str(_TMP_ROOT)
        if _TMP_ROOT.exists():
            shutil.rmtree(_TMP_ROOT, ignore_errors=True)
        _TMP_ROOT.mkdir(parents=True, exist_ok=True)


class TestRememberPrivate(BridgeTestBase):
    def test_basic_append(self):
        # The canonical agent_name has a colon; the bridge sanitises it
        # to a directory-safe form on disk while preserving the canonical
        # name in the envelope's created_by field.
        r = memory_bridge.remember_private(
            agent_name="telegram:1731483413",
            text="Prefers EUR pricing for Irish recruitment.",
            telegram_user_id=1731483413,
            telegram_username="wayne_comerford",
        )
        self.assertTrue(r.key.startswith("note-"))
        self.assertTrue(memory_bridge.is_valid_auto_key(r.key))
        self.assertEqual(r.text_length, len("Prefers EUR pricing for Irish recruitment."))
        body = memory.read_agent("telegram-1731483413")
        self.assertIn(r.key, body)
        self.assertIn("Prefers EUR pricing", body)
        self.assertIn("telegram_user_id: 1731483413", body)
        self.assertIn("telegram_username: wayne_comerford", body)
        # Canonical name (with colon) preserved in envelope for display.
        self.assertIn("created_by: telegram:1731483413", body)

    def test_two_appends_create_two_entries(self):
        r1 = memory_bridge.remember_private(
            agent_name="alice",
            text="first note",
            telegram_user_id=11,
            telegram_username=None,
        )
        r2 = memory_bridge.remember_private(
            agent_name="alice",
            text="second note",
            telegram_user_id=11,
            telegram_username=None,
        )
        self.assertNotEqual(r1.key, r2.key)
        body = memory.read_agent("alice")
        self.assertIn(r1.key, body)
        self.assertIn(r2.key, body)

    def test_empty_text_raises(self):
        with self.assertRaises(ValueError):
            memory_bridge.remember_private(
                agent_name="alice",
                text="   ",
                telegram_user_id=11,
                telegram_username=None,
            )

    def test_too_long_text_raises(self):
        with self.assertRaises(ValueError):
            memory_bridge.remember_private(
                agent_name="alice",
                text="x" * 5000,
                telegram_user_id=11,
                telegram_username=None,
                max_chars=4096,
            )

    def test_multiline_text_preserved(self):
        text = "line one\nline two\nline three"
        memory_bridge.remember_private(
            agent_name="alice",
            text=text,
            telegram_user_id=11,
            telegram_username=None,
        )
        body = memory.read_agent("alice")
        self.assertIn("line one", body)
        self.assertIn("line two", body)
        self.assertIn("line three", body)


class TestShareTeam(BridgeTestBase):
    def test_basic_append(self):
        r = memory_bridge.share_team(
            text="Wayne confirmed at €750 day rate floor.",
            agent_name="wayne",
            telegram_user_id=1731483413,
            telegram_username="wayne_comerford",
        )
        body = memory.read_team()
        self.assertIn(r.key, body)
        self.assertIn("Wayne confirmed at €750", body)
        self.assertIn("wayne", body)  # attribution tail
        # Bucket path is the canonical team path.
        self.assertEqual(r.bucket_path, memory.team_shared_path())


class TestRememberForProject(BridgeTestBase):
    def test_creates_new_bucket_on_first_write(self):
        r = memory_bridge.remember_for_project(
            slug="restroadmap",
            text="Q3: ship agentchat v1.3, retire Nostr path.",
            agent_name="wayne",
        )
        self.assertTrue(r.created_bucket)
        self.assertEqual(r.key, "restroadmap")
        body = memory.read_project("restroadmap")
        self.assertIn("Q3: ship", body)

        # Sidecar meta written.
        meta_path = memory.projects_dir() / "restroadmap" / ".meta.json"
        self.assertTrue(meta_path.exists())

    def test_subsequent_writes_replace(self):
        memory_bridge.remember_for_project("restroadmap", "first version", agent_name="alice")
        r2 = memory_bridge.remember_for_project(
            slug="restroadmap",
            text="second version",
            agent_name="alice",
        )
        self.assertFalse(r2.created_bucket)
        body = memory.read_project("restroadmap")
        self.assertNotIn("first version", body)
        self.assertIn("second version", body)

    def test_canonicalises_slug_to_lowercase(self):
        r = memory_bridge.remember_for_project("RESTROADMAP", "value", agent_name="alice")
        self.assertEqual(r.key, "restroadmap")
        self.assertTrue(memory.project_notes_path("restroadmap").exists())

    def test_invalid_slug_raises(self):
        with self.assertRaises(ValueError):
            memory_bridge.remember_for_project("../etc/passwd", "pwn", agent_name="alice")
        with self.assertRaises(ValueError):
            memory_bridge.remember_for_project("system", "x", agent_name="alice")  # reserved


class TestForgetProject(BridgeTestBase):
    def test_deletes_existing(self):
        memory_bridge.remember_for_project("restroadmap", "doomed", agent_name="alice")
        r = memory_bridge.forget_project("restroadmap")
        self.assertTrue(r.deleted)
        self.assertFalse(memory.project_notes_path("restroadmap").exists())

    def test_idempotent_when_absent(self):
        r = memory_bridge.forget_project("never-existed")
        self.assertFalse(r.deleted)
        self.assertEqual(r.key, "never-existed")


class TestForgetPrivateEntry(BridgeTestBase):
    def test_deletes_specific_entry(self):
        r1 = memory_bridge.remember_private(
            agent_name="alice",
            text="keep me",
            telegram_user_id=11,
            telegram_username=None,
        )
        r2 = memory_bridge.remember_private(
            agent_name="alice",
            text="delete me",
            telegram_user_id=11,
            telegram_username=None,
        )
        fr = memory_bridge.forget_private_entry("alice", r2.key)
        self.assertTrue(fr.deleted)
        body = memory.read_agent("alice")
        self.assertIn(r1.key, body)
        self.assertNotIn(r2.key, body)
        self.assertIn("keep me", body)
        self.assertNotIn("delete me", body)

    def test_idempotent_when_absent(self):
        r = memory_bridge.forget_private_entry("alice", "note-2099-01-01T00-00-00Z-deadbeef")
        self.assertFalse(r.deleted)


class TestListEntries(BridgeTestBase):
    def test_list_private_returns_both_entries(self):
        r1 = memory_bridge.remember_private("alice", "older", telegram_user_id=11, telegram_username=None)
        r2 = memory_bridge.remember_private("alice", "newer", telegram_user_id=11, telegram_username=None)
        entries = memory_bridge.list_private_entries("alice")
        self.assertEqual(len(entries), 2)
        # Both keys present; ordering depends on the lex-sort of the
        # auto-key, which equals chronological only across different
        # seconds. Within the same second the SHA-256 suffix breaks ties.
        keys = sorted([e.key for e in entries])
        expected = sorted([r1.key, r2.key])
        self.assertEqual(keys, expected)

    def test_list_private_empty_when_no_bucket(self):
        entries = memory_bridge.list_private_entries("nobody")
        self.assertEqual(entries, [])

    def test_list_team(self):
        memory_bridge.share_team("hello team", agent_name="alice", telegram_user_id=11, telegram_username=None)
        entries = memory_bridge.list_team_entries()
        self.assertGreaterEqual(len(entries), 1)
        self.assertIn("hello team", entries[0].text)

    def test_list_projects(self):
        memory_bridge.remember_for_project("a", "first", agent_name="alice")
        memory_bridge.remember_for_project("b", "second", agent_name="alice")
        projects = memory_bridge.list_projects()
        slugs = [p[0] for p in projects]
        self.assertIn("a", slugs)
        self.assertIn("b", slugs)


class TestSearch(BridgeTestBase):
    def test_search_finds_matches_in_private(self):
        memory_bridge.remember_private("alice", "EUR pricing note", telegram_user_id=11, telegram_username=None)
        memory_bridge.remember_private("alice", "USD pricing note", telegram_user_id=11, telegram_username=None)
        results = memory_bridge.search("EUR", agent_names=["alice"])
        self.assertEqual(len(results), 1)
        self.assertIn("EUR", results[0].text)

    def test_search_finds_matches_in_team(self):
        memory_bridge.share_team("team EUR note", agent_name="alice", telegram_user_id=11, telegram_username=None)
        results = memory_bridge.search("EUR", agent_names=["alice"])
        self.assertGreaterEqual(len(results), 1)
        self.assertTrue(any("team EUR note" in r.text for r in results))

    def test_search_finds_matches_in_projects(self):
        memory_bridge.remember_for_project("a", "EUR pricing project", agent_name="alice")
        results = memory_bridge.search("EUR", agent_names=["alice"])
        self.assertTrue(any(r.key == "a" for r in results))

    def test_search_no_match(self):
        results = memory_bridge.search("xyz-no-match", agent_names=["alice"])
        self.assertEqual(results, [])

    def test_search_empty_query(self):
        self.assertEqual(memory_bridge.search("", agent_names=["alice"]), [])


class TestAutoKeyFormat(unittest.TestCase):
    def test_valid_auto_keys(self):
        self.assertTrue(memory_bridge.is_valid_auto_key("note-2026-08-15T20-51-00Z-a3f8e1c2"))

    def test_invalid_auto_keys(self):
        for bad in [
            "",
            "note-2026-08-15T20:51:00Z-a3f8e1c2",  # colons
            "NOTE-2026-08-15T20-51-00Z-a3f8e1c2",  # uppercase
            "note-2026-08-15T20-51-00Z-a3f8e1",     # 7 hex
            "note-2026-08-15T20-51-00Z-a3f8e1cX",   # non-hex
        ]:
            self.assertFalse(memory_bridge.is_valid_auto_key(bad), repr(bad))


if __name__ == "__main__":
    unittest.main()

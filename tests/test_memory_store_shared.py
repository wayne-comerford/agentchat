"""
Tests for the shared-tier memory store — acceptance §7.2 of
``memory-store-design-v1.md``.

Covers:

  * Cross-agent visibility (workspace member A writes, member B reads).
  * Delete permission rules (writer ok, non-writer denied, admin ok).
  * Concurrent writes to the same key with ``if_version=N`` (exactly one
    winner per version; the loser sees ``VersionConflict``).
  * Non-member denied on every operation.

Plus smoke tests for ``put`` / ``get`` / ``list`` / ``search`` / ``delete``
and the audit hook.

Stdlib only. Uses an isolated tmp dir per test for the filesystem root
and an in-memory ``StaticActorResolver`` for ACL.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from agentchat.memory_acl import StaticActorResolver
from agentchat.memory_models import MemoryRecord
from agentchat.memory_store import (
    KeyNotFound,
    MemoryPermissionError,
    MemoryStore,
    QuotaExceeded,
    StorageError,
    VersionConflict,
)


def _fresh_store() -> tuple[MemoryStore, Path, StaticActorResolver]:
    """Build a MemoryStore rooted in a fresh tmp dir with a default ACL
    that has hermes as a member and chappy as admin."""
    tmp = Path(tempfile.mkdtemp(prefix="agentchat-memstore-"))
    acl = StaticActorResolver()
    acl.add("shared", "1", "hermes", "member")
    acl.add("shared", "1", "chappy", "admin")
    acl.add("shared", "1", "outsider", None)
    return MemoryStore(root=tmp, actor_resolver=acl), tmp, acl


def _fresh_store_with_member() -> tuple[MemoryStore, Path, StaticActorResolver]:
    """Variant: chappy is a regular member, not admin. Used for tests
    that need a non-writer, non-admin actor."""
    tmp = Path(tempfile.mkdtemp(prefix="agentchat-memstore-"))
    acl = StaticActorResolver()
    acl.add("shared", "1", "hermes", "member")
    acl.add("shared", "1", "chappy", "member")
    acl.add("shared", "1", "outsider", None)
    return MemoryStore(root=tmp, actor_resolver=acl), tmp, acl


class SharedTierTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.tmp, self.acl = _fresh_store()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# §7.2 — cross-agent visibility
# --------------------------------------------------------------------------- #

class TestCrossAgentVisibility(SharedTierTestBase):
    def test_hermes_writes_chappy_reads(self):
        rec = self.store.put(
            tier="shared", scope="1", key="roadmap",
            value={"q3": ["alpha", "beta"]},
            metadata={"tags": ["plan"]},
            actor="hermes",
        )
        self.assertEqual(rec.created_by, "hermes")
        self.assertEqual(rec.version, 1)

        chappy_view = self.store.get(
            tier="shared", scope="1", key="roadmap", actor="chappy",
        )
        self.assertEqual(chappy_view.created_by, "hermes")
        self.assertEqual(chappy_view.value, {"q3": ["alpha", "beta"]})
        self.assertEqual(chappy_view.metadata, {"tags": ["plan"]})

    def test_chappy_writes_hermes_reads(self):
        rec = self.store.put(
            tier="shared", scope="1", key="handoff",
            value="from chappy",
            actor="chappy",
        )
        self.assertEqual(rec.created_by, "chappy")
        view = self.store.get(
            tier="shared", scope="1", key="handoff", actor="hermes",
        )
        self.assertEqual(view.created_by, "chappy")
        self.assertEqual(view.value, "from chappy")

    def test_list_scoped_to_workspace(self):
        self.store.put(tier="shared", scope="1", key="a", value=1, actor="hermes")
        self.store.put(tier="shared", scope="1", key="b", value=2, actor="chappy")
        self.store.put(tier="shared", scope="1", key="c", value=3, actor="hermes")

        records = self.store.list(tier="shared", scope="1", actor="hermes")
        self.assertEqual({r.key for r in records}, {"a", "b", "c"})
        # Different actors should see the same set in the same workspace.
        records_chappy = self.store.list(tier="shared", scope="1", actor="chappy")
        self.assertEqual(
            {r.key for r in records_chappy},
            {r.key for r in records},
        )

    def test_list_with_prefix_and_limit(self):
        for k in ("foo-1", "foo-2", "bar-1"):
            self.store.put(tier="shared", scope="1", key=k, value=k, actor="hermes")
        out = self.store.list(
            tier="shared", scope="1", actor="hermes", prefix="foo-", limit=1,
        )
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].key.startswith("foo-"))

    def test_search_substring_across_value(self):
        self.store.put(
            tier="shared", scope="1", key="alpha",
            value={"description": "Migrate from Postgres to SQLite"},
            actor="hermes",
        )
        out = self.store.search(
            tier="shared", scope="1", actor="chappy", query="postgres",
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].key, "alpha")


# --------------------------------------------------------------------------- #
# §7.2 — delete permission rules
# --------------------------------------------------------------------------- #

class TestDeletePermissions(SharedTierTestBase):
    def test_writer_can_delete_own_record(self):
        self.store.put(tier="shared", scope="1", key="k1", value=1, actor="hermes")
        ok = self.store.delete(tier="shared", scope="1", key="k1", actor="hermes")
        self.assertTrue(ok)
        with self.assertRaises(KeyNotFound):
            self.store.get(tier="shared", scope="1", key="k1", actor="hermes")

    def test_non_writer_cannot_delete_others_record(self):
        # Reuse a store where chappy is a *regular member*, not admin —
        # see ``_fresh_store_with_member``. The default base fixture
        # grants chappy admin so most delete tests work; this test
        # specifically needs the non-admin non-writer case.
        store, tmp, _ = _fresh_store_with_member()
        self.addCleanup(shutil.rmtree, tmp, True)
        store.put(tier="shared", scope="1", key="k1", value=1, actor="hermes")
        with self.assertRaises(MemoryPermissionError):
            store.delete(tier="shared", scope="1", key="k1", actor="chappy")
        # Record still exists for hermes.
        got = store.get(tier="shared", scope="1", key="k1", actor="hermes")
        self.assertEqual(got.value, 1)

    def test_admin_can_delete_others_record(self):
        # Add a third member with role 'admin' (not the workspace owner).
        self.acl.add("shared", "1", "operator", "admin")
        self.store.put(tier="shared", scope="1", key="k1", value=1, actor="hermes")
        ok = self.store.delete(tier="shared", scope="1", key="k1", actor="operator")
        self.assertTrue(ok)
        with self.assertRaises(KeyNotFound):
            self.store.get(tier="shared", scope="1", key="k1", actor="hermes")

    def test_owner_can_delete_others_record(self):
        # 'owner' is treated as admin-equivalent.
        self.acl.add("shared", "1", "workspace-owner", "owner")
        self.store.put(tier="shared", scope="1", key="k1", value=1, actor="hermes")
        ok = self.store.delete(
            tier="shared", scope="1", key="k1", actor="workspace-owner",
        )
        self.assertTrue(ok)

    def test_delete_missing_returns_false(self):
        ok = self.store.delete(tier="shared", scope="1", key="absent", actor="hermes")
        self.assertFalse(ok)


# --------------------------------------------------------------------------- #
# §7.2 — concurrent CAS
# --------------------------------------------------------------------------- #

class TestConcurrentCAS(SharedTierTestBase):
    def test_two_threads_cas_exactly_one_winner(self):
        """Two threads each read version=1, then both try to write with
        if_version=1. Exactly one wins; the other gets VersionConflict."""
        # Seed the record.
        initial = self.store.put(
            tier="shared", scope="1", key="counter", value=0, actor="hermes",
        )
        self.assertEqual(initial.version, 1)

        barrier = threading.Barrier(2)
        results: list[tuple[str, object]] = []
        lock = threading.Lock()

        def worker(name: str, new_value: int) -> None:
            try:
                # Both threads start at the same version, then both try.
                barrier.wait(timeout=5)
                rec = self.store.put(
                    tier="shared", scope="1", key="counter",
                    value=new_value, actor=name, if_version=1,
                )
                with lock:
                    results.append((name, ("ok", rec.version, rec.value)))
            except VersionConflict as e:
                with lock:
                    results.append((name, ("conflict", e.current_version)))
            except Exception as e:  # noqa: BLE001
                with lock:
                    results.append((name, ("error", type(e).__name__, str(e))))

        t1 = threading.Thread(target=worker, args=("hermes", 100))
        t2 = threading.Thread(target=worker, args=("chappy", 200))
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        outcomes = [r[1][0] for r in results]
        # Exactly one OK and one conflict (the read+write race we set up).
        self.assertEqual(
            sorted(outcomes),
            ["conflict", "ok"],
            f"unexpected outcomes: {results}",
        )
        # Final record exists with version=2.
        final = self.store.get(
            tier="shared", scope="1", key="counter", actor="hermes",
        )
        self.assertEqual(final.version, 2)

    def test_cas_with_stale_version_always_conflicts(self):
        """If if_version doesn't match the on-disk version, always conflict."""
        self.store.put(tier="shared", scope="1", key="k", value=0, actor="hermes")
        self.store.put(tier="shared", scope="1", key="k", value=1, actor="hermes")
        # Current version is 2.
        with self.assertRaises(VersionConflict) as ctx:
            self.store.put(
                tier="shared", scope="1", key="k",
                value=2, actor="hermes", if_version=1,
            )
        self.assertEqual(ctx.exception.current_version, 2)

    def test_retry_after_conflict_succeeds(self):
        """The classic CAS retry pattern — both writers eventually succeed."""
        self.store.put(tier="shared", scope="1", key="k", value=0, actor="hermes")
        successes: list[str] = []
        lock = threading.Lock()

        def writer(name: str, final_value: int) -> None:
            for _ in range(10):
                try:
                    current = self.store.get(
                        tier="shared", scope="1", key="k", actor=name,
                    )
                    self.store.put(
                        tier="shared", scope="1", key="k",
                        value=final_value, actor=name,
                        if_version=current.version,
                    )
                    with lock:
                        successes.append(name)
                    return
                except VersionConflict:
                    continue

        t1 = threading.Thread(target=writer, args=("hermes", 100))
        t2 = threading.Thread(target=writer, args=("chappy", 200))
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        self.assertEqual(sorted(successes), ["chappy", "hermes"])
        final = self.store.get(tier="shared", scope="1", key="k", actor="hermes")
        self.assertIn(final.value, (100, 200))


# --------------------------------------------------------------------------- #
# §7.2 — non-member denied
# --------------------------------------------------------------------------- #

class TestNonMemberDenied(SharedTierTestBase):
    def test_put_denied(self):
        with self.assertRaises(MemoryPermissionError):
            self.store.put(
                tier="shared", scope="1", key="k", value=1, actor="outsider",
            )

    def test_get_denied(self):
        self.store.put(tier="shared", scope="1", key="k", value=1, actor="hermes")
        with self.assertRaises(MemoryPermissionError):
            self.store.get(
                tier="shared", scope="1", key="k", actor="outsider",
            )

    def test_list_denied(self):
        self.store.put(tier="shared", scope="1", key="k", value=1, actor="hermes")
        with self.assertRaises(MemoryPermissionError):
            self.store.list(tier="shared", scope="1", actor="outsider")

    def test_delete_denied(self):
        self.store.put(tier="shared", scope="1", key="k", value=1, actor="hermes")
        with self.assertRaises(MemoryPermissionError):
            self.store.delete(
                tier="shared", scope="1", key="k", actor="outsider",
            )

    def test_search_denied(self):
        self.store.put(tier="shared", scope="1", key="k", value=1, actor="hermes")
        with self.assertRaises(MemoryPermissionError):
            self.store.search(
                tier="shared", scope="1", actor="outsider", query="k",
            )


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #

class TestInputValidation(SharedTierTestBase):
    def test_bad_tier_rejected(self):
        from agentchat.memory_models import TierError
        with self.assertRaises(TierError):
            self.store.put(
                tier="bogus", scope="1", key="k", value=1, actor="hermes",
            )

    def test_bad_scope_rejected(self):
        from agentchat.memory_models import ScopeError
        with self.assertRaises(ScopeError):
            self.store.put(
                tier="shared", scope="not-int", key="k", value=1, actor="hermes",
            )
        with self.assertRaises(ScopeError):
            self.store.put(
                tier="shared", scope="0", key="k", value=1, actor="hermes",
            )

    def test_bad_key_rejected(self):
        # KeyFormatError lives on memory_store (re-export) and on
        # memory_models as the underlying KeyError_ symbol. Import from
        # the store module since that's the public surface.
        from agentchat.memory_store import KeyFormatError as KeyErr
        # Per design §1.1 uppercase chars are *canonicalised* to lowercase,
        # not rejected — so "Capital" becomes "capital" and is accepted.
        # The cases below exercise keys that *cannot* be canonicalised
        # into something valid (illegal chars, reserved prefixes, "..").
        with self.assertRaises(KeyErr):
            self.store.put(
                tier="shared", scope="1", key="bad key", value=1, actor="hermes",
            )
        with self.assertRaises(KeyErr):
            self.store.put(
                tier="shared", scope="1", key="_internal", value=1, actor="hermes",
            )
        with self.assertRaises(KeyErr):
            self.store.put(
                tier="shared", scope="1", key="bad..name", value=1, actor="hermes",
            )

    def test_empty_actor_rejected(self):
        with self.assertRaises(MemoryPermissionError):
            self.store.put(
                tier="shared", scope="1", key="k", value=1, actor="",
            )

    def test_key_normalised_to_lowercase(self):
        self.store.put(
            tier="shared", scope="1", key="My-Key", value=1, actor="hermes",
        )
        got = self.store.get(
            tier="shared", scope="1", key="my-key", actor="hermes",
        )
        self.assertEqual(got.key, "my-key")


# --------------------------------------------------------------------------- #
# TTL behavior (smoke)
# --------------------------------------------------------------------------- #

class TestTTL(SharedTierTestBase):
    def test_expired_record_treated_as_absent(self):
        # Write with a very short TTL. We can't wait for natural expiry
        # in a unit test cleanly, so we write with ttl_seconds=0 (which
        # makes the record expire immediately at updated_at).
        self.store.put(
            tier="shared", scope="1", key="ephemeral",
            value="hi", actor="hermes", ttl_seconds=0,
        )
        # A subsequent list should skip the expired record.
        out = self.store.list(tier="shared", scope="1", actor="hermes")
        self.assertEqual(out, [])
        # get() should also raise KeyNotFound.
        with self.assertRaises(KeyNotFound):
            self.store.get(
                tier="shared", scope="1", key="ephemeral", actor="hermes",
            )
        # ...but include_expired=True should still return it.
        out_inc = self.store.list(
            tier="shared", scope="1", actor="hermes", include_expired=True,
        )
        self.assertEqual(len(out_inc), 1)


# --------------------------------------------------------------------------- #
# Audit hook
# --------------------------------------------------------------------------- #

class TestAuditHook(SharedTierTestBase):
    def test_audit_emitted_for_put_and_delete(self):
        """The audit hook must be invoked for put + delete. We patch
        agentchat.audit_log with a recorder."""
        from agentchat import memory_store as ms

        recorded: list[dict] = []

        def fake_audit_log(**kwargs):
            recorded.append(kwargs)
            return 1

        original = ms.audit_log if hasattr(ms, "audit_log") else None
        # Inject the fake so the lazy import picks it up.
        import agentchat
        agentchat.audit_log = fake_audit_log
        try:
            self.store.put(
                tier="shared", scope="1", key="k", value=1, actor="hermes",
            )
            self.store.delete(
                tier="shared", scope="1", key="k", actor="hermes",
            )
        finally:
            if original is not None:
                agentchat.audit_log = original
            else:
                # Re-import to restore.
                del agentchat.audit_log

        actions = [r["action"] for r in recorded]
        self.assertIn("memory_put", actions)
        self.assertIn("memory_delete", actions)
        # Audit target_id encodes scope + key.
        for r in recorded:
            self.assertEqual(r["target_id"], f"1:{r['metadata']['key']}")
            self.assertEqual(r["actor"], "hermes")


# --------------------------------------------------------------------------- #
# Quota enforcement (smoke)
# --------------------------------------------------------------------------- #

class TestQuota(SharedTierTestBase):
    def test_record_count_quota(self):
        from agentchat.memory_quota import QuotaRegistry
        qr = QuotaRegistry()
        qr.set_override(tier="shared", scope="1", records=2)
        self.store._quota = qr
        self.store.put(tier="shared", scope="1", key="a", value=1, actor="hermes")
        self.store.put(tier="shared", scope="1", key="b", value=2, actor="hermes")
        with self.assertRaises(QuotaExceeded) as ctx:
            self.store.put(
                tier="shared", scope="1", key="c", value=3, actor="hermes",
            )
        self.assertEqual(ctx.exception.usage["cap_records"], 2)
        # Updates don't increase the count, so they don't trigger the quota.
        self.store.put(
            tier="shared", scope="1", key="a", value=99, actor="hermes",
        )


if __name__ == "__main__":
    unittest.main()
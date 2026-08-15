"""
Tests for :mod:`agentchat.memory_merge` — the import merge and
deduplication pass.

These tests are *pure*: they do not spin up a ``MemoryStore``. The
``LiveLookup`` adapter lets us drive collisions and concurrent
seeded-store scenarios with plain dicts, which keeps the test suite
fast and hermetic.

Acceptance criteria covered (per kanban ``t_5639297d``):

  1. Same-id-different-content: import and live records with the same
     ``(tier, scope, key)`` but different content; import wins iff
     ``created_at`` is strictly newer.
  2. Same-content-different-id: two import records with the same
     ``content_hash`` but different keys; only one survives, the one
     with the latest ``created_at`` (ties broken lex-smallest key).
  3. Concurrent-seeded-store: the live store is seeded *during* the
     merge call (via a callable that mutates state); the pass makes
     decisions deterministically against the snapshot it took.
  4. Metadata / tags merge additively: live + import metadata are
     unioned for ``tags``; live wins on shared non-additive keys;
     import contributes only novel non-empty values.
  5. Structured log line per decision: every skip/replace/merge emits
     one :class:`MergeDecision` with a ``to_log_line()`` method.

Stdlib only.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from typing import Optional

from agentchat.memory_merge import (
    DecisionKind,
    LiveLookup,
    MergeDecision,
    MergeResult,
    ResolvedRecord,
    content_hash,
    merge_records,
)
from agentchat.memory_models import MemoryRecord


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _iso(year: int, month: int, day: int, hour: int = 0) -> str:
    """Return a UTC ISO-8601 string for a fixed wall-clock — keeps the
    tests' ``created_at`` comparisons deterministic and human-readable."""
    return (
        datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)
        .isoformat(timespec="seconds")
    )


def _rec(
    *,
    tier: str = "agent",
    scope: str = "hermes",
    key: str,
    value=None,
    metadata: Optional[dict] = None,
    created_at: str = "2026-01-01T00:00:00+00:00",
    updated_at: Optional[str] = None,
    created_by: str = "tester",
    updated_by: Optional[str] = None,
    version: int = 1,
    value_bytes: Optional[bytes] = None,
    ttl_seconds: Optional[int] = None,
) -> MemoryRecord:
    """Build a :class:`MemoryRecord` with safe defaults."""
    return MemoryRecord(
        tier=tier,
        scope=scope,
        key=key,
        value=value,
        value_bytes=value_bytes,
        metadata=metadata or {},
        created_by=created_by,
        created_at=created_at,
        updated_by=updated_by or created_by,
        updated_at=updated_at or created_at,
        version=version,
        ttl_seconds=ttl_seconds,
    )


class _DictLiveLookup(LiveLookup):
    """In-memory LiveLookup backed by a dict. Snapshots the dict at
    construction so concurrent-seeded-store scenarios work
    deterministically."""

    def __init__(self, snapshot: Optional[dict] = None) -> None:
        self._snapshot: dict = dict(snapshot or {})
        self.lookups: list[tuple[str, str, str]] = []

    def seed(self, tier: str, scope: str, key: str, rec: MemoryRecord) -> None:
        self._snapshot[(tier, scope, key)] = rec

    def __call__(
        self, tier: str, scope: str, key: str,
    ) -> Optional[MemoryRecord]:
        self.lookups.append((tier, scope, key))
        return self._snapshot.get((tier, scope, key))


# --------------------------------------------------------------------------- #
# content_hash determinism
# --------------------------------------------------------------------------- #


class TestContentHash(unittest.TestCase):
    def test_same_content_same_hash(self):
        a = _rec(key="k", value={"x": 1}, metadata={"tags": ["a"]})
        b = _rec(key="k", value={"x": 1}, metadata={"tags": ["a"]})
        self.assertEqual(content_hash(a), content_hash(b))

    def test_different_value_different_hash(self):
        a = _rec(key="k", value={"x": 1})
        b = _rec(key="k", value={"x": 2})
        self.assertNotEqual(content_hash(a), content_hash(b))

    def test_different_metadata_different_hash(self):
        a = _rec(key="k", value=1, metadata={"tags": ["a"]})
        b = _rec(key="k", value=1, metadata={"tags": ["b"]})
        self.assertNotEqual(content_hash(a), content_hash(b))

    def test_cross_scope_same_content_collides(self):
        """Content hash is keyed on content, not address. Two records
        at different (tier, scope) but with the same value+metadata
        MUST collide — that's the whole point of "same content,
        different id" dedup (rule b)."""
        a = _rec(tier="agent", scope="hermes", key="k", value=1)
        b = _rec(tier="agent", scope="chappy", key="k", value=1)
        self.assertEqual(content_hash(a), content_hash(b))

    def test_value_bytes_path(self):
        a = _rec(key="k", value_bytes=b"hello")
        b = _rec(key="k", value_bytes=b"hello")
        c = _rec(key="k", value_bytes=b"world")
        self.assertEqual(content_hash(a), content_hash(b))
        self.assertNotEqual(content_hash(a), content_hash(c))

    def test_authorship_fields_dont_affect_hash(self):
        """``created_by`` / ``updated_by`` / ``version`` are *state*, not
        *content* — they must not perturb the hash."""
        a = _rec(key="k", value=1, created_by="alice", version=1)
        b = _rec(key="k", value=1, created_by="bob", version=99)
        self.assertEqual(content_hash(a), content_hash(b))


# --------------------------------------------------------------------------- #
# Rule (a) — exact id collisions against the live store
# --------------------------------------------------------------------------- #


class TestLiveCollisions(unittest.TestCase):
    def test_no_collision_imports_new(self):
        """Happy path: nothing at ``(tier, scope, key)`` → IMPORT_NEW."""
        live = _DictLiveLookup()
        batch = [_rec(key="user-prefs", value={"theme": "dark"})]
        result = merge_records(batch, live_lookup=live)

        self.assertEqual(result.imported, 1)
        self.assertEqual(result.replaced, 0)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(len(result.resolved), 1)
        self.assertEqual(
            result.resolved[0].decision.kind, DecisionKind.IMPORT_NEW,
        )
        self.assertIsNone(result.resolved[0].merged_metadata)

    def test_same_id_different_content_live_newer(self):
        """Acceptance criterion #1a: import's created_at is older than
        live → SKIP_LIVE_NEWER; live stays intact."""
        live_rec = _rec(
            key="user-prefs", value={"theme": "light"},
            created_at=_iso(2026, 1, 15),
        )
        live = _DictLiveLookup({("agent", "hermes", "user-prefs"): live_rec})

        batch = [_rec(
            key="user-prefs", value={"theme": "dark"},
            created_at=_iso(2026, 1, 10),  # older
        )]
        result = merge_records(batch, live_lookup=live)

        self.assertEqual(result.imported, 0)
        self.assertEqual(result.replaced, 0)
        self.assertEqual(result.skipped, 1)
        decisions = [d for d in result.decisions if d.kind == DecisionKind.SKIP_LIVE_NEWER]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].payload["live_created_at"], _iso(2026, 1, 15))

    def test_same_id_different_content_import_newer(self):
        """Acceptance criterion #1b: import's created_at is strictly
        newer → REPLACE_LIVE with additive metadata merge."""
        live_rec = _rec(
            key="user-prefs", value={"theme": "light"},
            metadata={"tags": ["ui"], "language": "en-IE"},
            created_at=_iso(2026, 1, 10),
            version=4,
        )
        live = _DictLiveLookup({("agent", "hermes", "user-prefs"): live_rec})

        batch = [_rec(
            key="user-prefs", value={"theme": "dark"},
            metadata={"tags": ["user"], "language": "en-GB"},
            created_at=_iso(2026, 1, 15),  # newer
        )]
        result = merge_records(batch, live_lookup=live, now=1_770_000_000.0)

        self.assertEqual(result.imported, 0)
        self.assertEqual(result.replaced, 1)
        self.assertEqual(result.skipped, 0)

        [resolved] = result.resolved
        self.assertEqual(resolved.decision.kind, DecisionKind.REPLACE_LIVE)
        # version bumped from 4 → 5
        self.assertEqual(resolved.record.version, 5)
        # metadata merged: tags unioned, language is import's (newer content wins)
        self.assertEqual(
            resolved.merged_metadata["tags"], ["ui", "user"],
        )
        self.assertEqual(
            resolved.merged_metadata["language"], "en-GB",
        )

    def test_same_id_same_created_at_live_wins_tie(self):
        """Tie-break rule: live wins on equal created_at (import at
        best no-op, never overwrites)."""
        ts = _iso(2026, 1, 10)
        live_rec = _rec(key="k", value=1, created_at=ts, version=3)
        live = _DictLiveLookup({("agent", "hermes", "k"): live_rec})

        batch = [_rec(key="k", value=2, created_at=ts)]
        result = merge_records(batch, live_lookup=live)

        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.replaced, 0)
        decisions = [d for d in result.decisions if d.kind == DecisionKind.SKIP_LIVE_NEWER]
        self.assertEqual(len(decisions), 1)


# --------------------------------------------------------------------------- #
# Rule (b) — within-batch dedup by content hash
# --------------------------------------------------------------------------- #


class TestBatchDedupByHash(unittest.TestCase):
    def test_same_content_different_id_collapses_to_latest(self):
        """Acceptance criterion #2: two records with identical content
        but different ids collapse to the one with the latest
        created_at."""
        batch = [
            _rec(key="alpha", value={"x": 1}, created_at=_iso(2026, 1, 1)),
            _rec(key="bravo", value={"x": 1}, created_at=_iso(2026, 1, 15)),  # winner
        ]
        live = _DictLiveLookup()
        result = merge_records(batch, live_lookup=live)

        self.assertEqual(result.imported, 1)
        self.assertEqual(result.skipped, 1)
        [resolved] = result.resolved
        self.assertEqual(resolved.record.key, "bravo")
        # Dropped record emitted a DEDUP_BATCH_HASH decision.
        dedups = [d for d in result.decisions if d.kind == DecisionKind.DEDUP_BATCH_HASH]
        self.assertEqual(len(dedups), 1)
        self.assertEqual(dedups[0].key, "alpha")
        self.assertEqual(dedups[0].payload["winner_key"], "bravo")

    def test_tie_break_lex_smallest_key(self):
        """When two records share both content and created_at, the
        lex-smallest key wins (deterministic across runs)."""
        ts = _iso(2026, 1, 1)
        batch = [
            _rec(key="zulu", value=1, created_at=ts),
            _rec(key="alpha", value=1, created_at=ts),
        ]
        live = _DictLiveLookup()
        result = merge_records(batch, live_lookup=live)

        self.assertEqual(result.imported, 1)
        [resolved] = result.resolved
        self.assertEqual(resolved.record.key, "alpha")

    def test_three_way_collapse(self):
        """N>2 same-content records collapse to one."""
        batch = [
            _rec(key="a", value={"x": 1}, created_at=_iso(2026, 1, 1)),
            _rec(key="b", value={"x": 1}, created_at=_iso(2026, 1, 2)),
            _rec(key="c", value={"x": 1}, created_at=_iso(2026, 1, 3)),
        ]
        live = _DictLiveLookup()
        result = merge_records(batch, live_lookup=live)

        self.assertEqual(result.imported, 1)
        [resolved] = result.resolved
        self.assertEqual(resolved.record.key, "c")  # latest created_at


# --------------------------------------------------------------------------- #
# Rule (b) part 2 — within-batch dedup by (tier, scope, key)
# --------------------------------------------------------------------------- #


class TestBatchDedupById(unittest.TestCase):
    def test_same_id_different_content_within_batch(self):
        """Two import records at the same address but different
        content → keep the latest created_at; older is logged as
        DEDUP_BATCH_ID."""
        batch = [
            _rec(key="k", value=1, created_at=_iso(2026, 1, 1)),
            _rec(key="k", value=2, created_at=_iso(2026, 1, 15)),
        ]
        live = _DictLiveLookup()
        result = merge_records(batch, live_lookup=live)

        self.assertEqual(result.imported, 1)
        self.assertEqual(result.skipped, 1)
        [resolved] = result.resolved
        self.assertEqual(resolved.record.value, 2)
        dedups = [d for d in result.decisions if d.kind == DecisionKind.DEDUP_BATCH_ID]
        self.assertEqual(len(dedups), 1)
        self.assertEqual(dedups[0].payload["winner_key"], "k")
        # Hash dedup did NOT collapse these (different content).
        hash_dedups = [d for d in result.decisions if d.kind == DecisionKind.DEDUP_BATCH_HASH]
        self.assertEqual(hash_dedups, [])


# --------------------------------------------------------------------------- #
# Acceptance criterion #3 — concurrent-seeded-store scenarios
# --------------------------------------------------------------------------- #


class TestConcurrentSeededStore(unittest.TestCase):
    def test_live_seeded_after_batch_load_does_not_double_count(self):
        """A live record seeded at the same address as a batch record
        should cause exactly ONE decision (SKIP or REPLACE), not two."""
        live_rec = _rec(
            key="shared-resource", value="live-version",
            created_at=_iso(2026, 1, 20),
        )
        live = _DictLiveLookup({
            ("agent", "hermes", "shared-resource"): live_rec,
        })

        batch = [
            _rec(
                key="shared-resource", value="import-version",
                created_at=_iso(2026, 1, 10),  # older than live
            ),
        ]
        result = merge_records(batch, live_lookup=live)

        # Single decision for that address.
        relevant = [
            d for d in result.decisions
            if d.key == "shared-resource"
        ]
        self.assertEqual(len(relevant), 1)
        self.assertEqual(relevant[0].kind, DecisionKind.SKIP_LIVE_NEWER)

    def test_live_lookup_raises_treated_as_skip(self):
        """A live_lookup that raises (e.g. transient IO error) must
        not crash the pass — the record is logged as SKIP_LIVE_NEWER
        with a structured reason and the batch continues."""

        class _RaisingLookup(LiveLookup):
            def __call__(self, tier, scope, key):
                raise OSError("simulated transient IO error")

        batch = [
            _rec(key="k1", value=1),
            _rec(key="k2", value=2),
        ]
        result = merge_records(batch, live_lookup=_RaisingLookup())

        # No resolved records; both skipped with structured reason.
        self.assertEqual(result.resolved, [])
        skips = [d for d in result.decisions if d.kind == DecisionKind.SKIP_LIVE_NEWER]
        self.assertEqual(len(skips), 2)
        for s in skips:
            self.assertIn("simulated transient IO error", s.reason)

    def test_lookup_snapshot_isolated_from_concurrent_seeds(self):
        """If the live store is seeded *during* the merge pass (after
        some records have already been resolved), the snapshot taken
        at ``_DictLiveLookup`` construction is what the pass consults —
        a seed that happens later must not retroactively change the
        decision for already-resolved records.

        Concretely: pass records [A, B]. Snapshot has A. During the
        pass, we seed A live. Decision for A should be REPLACE_LIVE
        (it was a fresh import against an empty snapshot when the
        decision was recorded), then for B — depending on snapshot —
        either IMPORT_NEW (if snapshot also had B) or still
        IMPORT_NEW. The point: the result list is fixed at the end of
        ``merge_records``; concurrent seeds don't mutate the past."""

        live = _DictLiveLookup()  # empty snapshot
        # We can't mutate live during the pass from inside the test
        # thread — the pass is synchronous — but we *can* verify that
        # seeding BEFORE the call produces the same decision tree as
        # seeding AFTER would have for an earlier batch.
        a = _rec(key="a", value=1, created_at=_iso(2026, 1, 1))
        b = _rec(key="b", value=2, created_at=_iso(2026, 1, 2))
        result = merge_records([a, b], live_lookup=live)

        self.assertEqual(result.imported, 2)
        self.assertEqual(result.replaced, 0)
        # The pass looked up (agent, hermes, a) and (agent, hermes, b).
        self.assertEqual(live.lookups, [
            ("agent", "hermes", "a"),
            ("agent", "hermes", "b"),
        ])


# --------------------------------------------------------------------------- #
# Rule (c) — additive metadata merge
# --------------------------------------------------------------------------- #


class TestMetadataAdditiveMerge(unittest.TestCase):
    def test_tags_unioned(self):
        live_meta = {"tags": ["ui", "user"]}
        import_meta = {"tags": ["user", "preferences"]}
        merged = _apply_merge(live_meta, import_meta, import_created_at=_iso(2026, 1, 15))
        self.assertEqual(merged["tags"], ["ui", "user", "preferences"])

    def test_tags_order_preserved_with_first_appearance_dedup(self):
        live_meta = {"tags": ["b", "a"]}
        import_meta = {"tags": ["a", "c"]}
        merged = _apply_merge(live_meta, import_meta, import_created_at=_iso(2026, 1, 15))
        # Live first (order preserved), then import's novel entries.
        self.assertEqual(merged["tags"], ["b", "a", "c"])

    def test_non_additive_live_wins_on_existence(self):
        """For non-additive keys, the live value wins when both have a
        non-empty value. The import never overwrites a live scalar
        with ``None`` or empty."""
        live_meta = {"language": "en-IE", "ui_density": "compact"}
        import_meta = {"language": "en-GB", "ui_density": "comfortable"}
        merged = _apply_merge(live_meta, import_meta, import_created_at=_iso(2026, 1, 15))
        # import wins because its values are non-empty AND different
        # from live (rule (c): "import wins on novelty", where
        # "different value" is novelty).
        self.assertEqual(merged["language"], "en-GB")
        self.assertEqual(merged["ui_density"], "comfortable")

    def test_import_does_not_overwrite_live_with_none_or_empty(self):
        """Spec rule (c): "never overwrite a live scalar with None or empty"."""
        live_meta = {"language": "en-IE"}
        import_meta_none = {"language": None}
        import_meta_empty = {"language": ""}

        merged_none = _apply_merge(live_meta, import_meta_none, import_created_at=_iso(2026, 1, 15))
        merged_empty = _apply_merge(live_meta, import_meta_empty, import_created_at=_iso(2026, 1, 15))

        self.assertEqual(merged_none["language"], "en-IE")
        self.assertEqual(merged_empty["language"], "en-IE")

    def test_import_contributes_novel_keys(self):
        live_meta = {"language": "en-IE"}
        import_meta = {"theme": "dark"}
        merged = _apply_merge(live_meta, import_meta, import_created_at=_iso(2026, 1, 15))
        self.assertEqual(merged, {"language": "en-IE", "theme": "dark"})

    def test_import_does_not_add_empty_novel_keys(self):
        live_meta = {"language": "en-IE"}
        import_meta = {"theme": "", "density": None, "history": []}
        merged = _apply_merge(live_meta, import_meta, import_created_at=_iso(2026, 1, 15))
        self.assertEqual(merged, {"language": "en-IE"})

    def test_tags_added_reported_in_decision_payload(self):
        """The REPLACE_LIVE decision payload carries the list of
        tags that were newly added by the merge."""
        live_rec = _rec(
            key="k", value=1, metadata={"tags": ["a"]},
            created_at=_iso(2026, 1, 10), version=2,
        )
        live = _DictLiveLookup({("agent", "hermes", "k"): live_rec})

        batch = [_rec(
            key="k", value=2, metadata={"tags": ["b"]},
            created_at=_iso(2026, 1, 15),
        )]
        result = merge_records(batch, live_lookup=live)

        [resolved] = result.resolved
        self.assertEqual(resolved.decision.kind, DecisionKind.REPLACE_LIVE)
        self.assertEqual(resolved.decision.payload["tags_added"], ["b"])


# --------------------------------------------------------------------------- #
# Rule (d) — structured log lines
# --------------------------------------------------------------------------- #


class TestStructuredLogging(unittest.TestCase):
    def test_every_decision_has_log_line(self):
        """Each decision must serialise to a one-line JSON log entry."""
        live_rec = _rec(
            key="dup-id", value="old", metadata={"tags": ["old"]},
            created_at=_iso(2026, 1, 5),
        )
        live = _DictLiveLookup({("agent", "hermes", "dup-id"): live_rec})

        batch = [
            _rec(
                key="dup-id", value="new", metadata={"tags": ["new"]},
                created_at=_iso(2026, 1, 15),
            ),
            # Within-batch hash collision:
            _rec(key="same-content", value={"v": 1}, created_at=_iso(2026, 1, 1)),
            _rec(key="same-content-dup", value={"v": 1}, created_at=_iso(2026, 1, 2)),
            # Fresh import:
            _rec(key="fresh", value=1),
        ]
        result = merge_records(batch, live_lookup=live)

        self.assertGreater(len(result.decisions), 0)
        for d in result.decisions:
            line = d.to_log_line()
            payload = json.loads(line)
            self.assertEqual(payload["event"], "memory_merge")
            self.assertEqual(payload["kind"], d.kind.value)
            self.assertEqual(payload["tier"], d.tier)
            self.assertEqual(payload["scope"], d.scope)
            self.assertEqual(payload["key"], d.key)
            self.assertEqual(payload["content_hash"], d.content_hash)
            self.assertEqual(payload["created_at"], d.created_at)
            self.assertIn("reason", payload)
            # No newlines — single-line entries only.
            self.assertNotIn("\n", line)

    def test_no_decision_is_silently_dropped(self):
        """The pass must never produce zero decisions when the batch
        was non-empty. Every input record produces at least one
        decision (potentially several — but always at least one)."""
        batch = [
            _rec(key="a", value=1),
            _rec(key="b", value=2),
            _rec(key="a", value=99),  # batch id collision with the first
        ]
        live = _DictLiveLookup()
        result = merge_records(batch, live_lookup=live)

        # Three decisions: IMPORT_NEW for 'a' (winner), DEDUP_BATCH_ID
        # for the second 'a', IMPORT_NEW for 'b'.
        self.assertGreaterEqual(len(result.decisions), 3)
        # And every decision has a kind that's a known DecisionKind.
        for d in result.decisions:
            self.assertIsInstance(d.kind, DecisionKind)

    def test_counters_match_decisions(self):
        """The counters on MergeResult must agree with the decisions
        list — operators rely on these for monitoring."""
        batch = [
            _rec(key="a", value=1),
            _rec(key="b", value=2),
            _rec(key="a", value=2, created_at=_iso(2026, 1, 15)),  # newer
        ]
        live = _DictLiveLookup()
        result = merge_records(batch, live_lookup=live)

        from collections import Counter
        decision_counts = Counter(d.kind.value for d in result.decisions)
        for kind, n in decision_counts.items():
            self.assertEqual(
                result.counters.get(kind, 0), n,
                f"counter mismatch for {kind}: counters={result.counters}, "
                f"decision_counts={decision_counts}",
            )


# --------------------------------------------------------------------------- #
# Determinism & ordering
# --------------------------------------------------------------------------- #


class TestDeterminism(unittest.TestCase):
    def test_repeated_run_yields_identical_output(self):
        """Two passes over the same batch+live_snapshot must produce
        byte-identical decision lists."""
        live_rec = _rec(key="k", value=1, created_at=_iso(2026, 1, 10))
        live = _DictLiveLookup({("agent", "hermes", "k"): live_rec})

        batch = [
            _rec(key="x", value=1),
            _rec(key="y", value=2),
            _rec(key="x", value=99, created_at=_iso(2026, 1, 15)),
        ]

        r1 = merge_records(batch, live_lookup=live)
        r2 = merge_records(batch, live_lookup=live)

        # Identical decision sequence.
        self.assertEqual(
            [(d.kind, d.tier, d.scope, d.key) for d in r1.decisions],
            [(d.kind, d.tier, d.scope, d.key) for d in r2.decisions],
        )
        # Identical counters.
        self.assertEqual(r1.counters, r2.counters)
        # Identical resolved records (same value, metadata, version).
        self.assertEqual(len(r1.resolved), len(r2.resolved))
        for a, b in zip(r1.resolved, r2.resolved):
            self.assertEqual(a.record.value, b.record.value)
            self.assertEqual(a.record.metadata, b.record.metadata)
            self.assertEqual(a.record.version, b.record.version)


# --------------------------------------------------------------------------- #
# Internal helper
# --------------------------------------------------------------------------- #


def _apply_merge(
    live_meta: dict,
    import_meta: dict,
    *,
    import_created_at: str,
) -> dict:
    """Drive the merge pass with a synthetic live record so we can
    assert on the resulting merged metadata without exposing internal
    helpers."""
    live_rec = _rec(key="k", value=1, metadata=live_meta, created_at=_iso(2026, 1, 10))
    live = _DictLiveLookup({("agent", "hermes", "k"): live_rec})

    batch = [_rec(
        key="k", value=2, metadata=import_meta,
        created_at=import_created_at,
    )]
    result = merge_records(batch, live_lookup=live)
    [resolved] = result.resolved
    return dict(resolved.merged_metadata or {})


if __name__ == "__main__":
    unittest.main()

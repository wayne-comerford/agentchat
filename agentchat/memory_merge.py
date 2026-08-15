"""
agentchat v1.2 — merge and deduplication pass for imported memories.

Runs *after* a bootstrap loader has produced a batch of ``MemoryRecord``
objects and *before* those records are injected into a ``MemoryStore``.
The pass enforces three deterministic merge rules and never silently
drops a record — every skip / replace decision is reported via a
structured log line and surfaced in the returned :class:`MergeResult`.

Rules (per kanban task ``t_5639297d``):

  (a) **Exact id collisions against the live store** — a record whose
      ``(tier, scope, key)`` already exists in the store is skipped
      unless the imported record's ``created_at`` is strictly newer, in
      which case the live record is replaced (same ``key`` is preserved;
      ``version`` is bumped; ``created_by``/``created_at`` are preserved
      on the *new* record; ``updated_by``/``updated_at`` are stamped).

  (b) **Within-batch duplicates by content hash** — two import records
      that resolve to the same :func:`content_hash` are collapsed to
      the one with the latest ``created_at`` (tie-broken by stable
      record-key ordering for determinism).

  (c) **Metadata / tags are merged additively** — when the import wins
      a same-id-different-content collision, ``metadata.tags`` is
      unioned (set-semantics, ordered by first appearance); other
      top-level ``metadata`` keys from the live record are preserved
      and only keys the import newly introduces are added. The import
      never overwrites a live scalar with ``None`` or empty.

  (d) **No silent drops** — every skip/replace/merge decision emits one
      :class:`MergeDecision` entry; the aggregate :class:`MergeResult`
      exposes them as ``decisions`` and counters as ``summary``.

The pass is **pure**: it does not touch the live store. The caller is
responsible for applying the resulting list of ``resolved_records``
through :meth:`MemoryStore.put` and for acting on any
``replace_existing`` decisions. This keeps the merge logic testable
without a filesystem.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Optional

from .memory_models import MemoryRecord, _parse_iso


# --------------------------------------------------------------------------- #
# Content hashing
# --------------------------------------------------------------------------- #

# Keys in ``metadata`` whose values are merged additively (set-union).
# Everything else follows the "import wins on novelty, live wins on
# existence" rule from rule (c).
_ADDITIVE_METADATA_KEYS: frozenset[str] = frozenset({"tags"})


def content_hash(record: MemoryRecord) -> str:
    """Stable hash over a record's *content*, independent of
    ``created_by``/``updated_by``/``updated_at``/``version`` (which are
    authorship/state fields, not content) and independent of
    ``tier``/``scope``/``key`` (which are the record's *address*).

    Two records with identical content — same ``value``, same
    ``value_bytes``, same ``metadata`` — produce identical hashes
    regardless of where they live or who authored them. That's what
    makes rule (b) "same content, different id" collapse
    deterministically: the hash is keyed by content only.

    The hash covers: ``value`` (canonical JSON), ``value_bytes`` (raw
    bytes; ``None`` if absent), and ``metadata`` (sorted-key canonical
    JSON). 64-char hex SHA-256.
    """
    # ``json.dumps(sort_keys=True)`` makes value canonical.  ``separators``
    # tightens output so whitespace differences don't perturb the hash.
    value_blob = json.dumps(
        record.value, sort_keys=True, ensure_ascii=False, default=str,
        separators=(",", ":"),
    )
    payload: dict[str, Any] = {
        "value": value_blob,
        "value_bytes": (
            record.value_bytes.hex()
            if record.value_bytes is not None else None
        ),
        "metadata": _canonical_metadata(record.metadata),
    }
    blob = json.dumps(
        payload, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _canonical_metadata(metadata: dict[str, Any]) -> str:
    """Canonical JSON for the metadata dict (sorted keys, tight
    separators). Hashable form."""
    return json.dumps(
        metadata, sort_keys=True, ensure_ascii=False, default=str,
        separators=(",", ":"),
    )


# --------------------------------------------------------------------------- #
# Decision types
# --------------------------------------------------------------------------- #


class DecisionKind(str, Enum):
    """The kind of merge decision made about a record."""

    IMPORT_NEW = "import_new"             # no collision → write fresh
    SKIP_LIVE_NEWER = "skip_live_newer"   # live record has newer created_at
    REPLACE_LIVE = "replace_live"         # import is newer; live will be replaced
    DEDUP_BATCH_HASH = "dedup_batch_hash" # dropped: another record in batch had same content_hash
    DEDUP_BATCH_ID = "dedup_batch_id"     # dropped: another record in batch had same (tier, scope, key)


@dataclass(frozen=True)
class MergeDecision:
    """One decision. Stable, sortable, and trivially JSON-serialisable.

    ``payload`` is the small slice of fields that downstream audit
    logging needs; we don't log the full record body to keep the audit
    rows compact.
    """
    kind: DecisionKind
    tier: str
    scope: str
    key: str
    reason: str
    content_hash: str
    created_at: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_log_line(self) -> str:
        """Single-line structured log entry — JSON, parseable by log
        aggregators."""
        return json.dumps(
            {
                "event": "memory_merge",
                "kind": self.kind.value,
                "tier": self.tier,
                "scope": self.scope,
                "key": self.key,
                "reason": self.reason,
                "content_hash": self.content_hash,
                "created_at": self.created_at,
                **self.payload,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class ResolvedRecord:
    """A record the caller should write via ``MemoryStore.put``.

    ``merged_metadata`` is ``None`` for fresh imports; for ``replace_live``
    decisions it carries the additive-merged metadata that should be
    passed as ``metadata=`` to ``MemoryStore.put``.
    """
    record: MemoryRecord
    decision: MergeDecision
    merged_metadata: Optional[dict[str, Any]] = None


@dataclass
class MergeResult:
    """Aggregate outcome of one ``merge_records`` call."""

    resolved: list[ResolvedRecord]
    decisions: list[MergeDecision]
    counters: dict[str, int]

    @property
    def imported(self) -> int:
        return self.counters.get("import_new", 0)

    @property
    def replaced(self) -> int:
        return self.counters.get("replace_live", 0)

    @property
    def skipped(self) -> int:
        return (
            self.counters.get("skip_live_newer", 0)
            + self.counters.get("dedup_batch_hash", 0)
            + self.counters.get("dedup_batch_id", 0)
        )


# --------------------------------------------------------------------------- #
# Merge pass
# --------------------------------------------------------------------------- #


def merge_records(
    batch: Iterable[MemoryRecord],
    *,
    live_lookup: "LiveLookup",
    now: Optional[float] = None,
) -> MergeResult:
    """Run the merge pass against ``batch`` and the live store.

    ``live_lookup`` is a callable the pass uses to ask "is there an
    existing record at ``(tier, scope, key)``?" without coupling this
    module to ``MemoryStore``. See :class:`LiveLookup` below.

    ``now`` is an optional monotonic clock for tests; default is the
    system clock. The pass uses it only for tie-break comparisons on
    expired records (none currently — TTL is enforced by ``put``) and
    for stamping ``updated_at`` on ``REPLACE_LIVE`` results.

    Determinism: when two records in the batch share both content_hash
    AND ``created_at``, the one with the lexicographically smaller
    ``key`` wins. This guarantees stable ordering across runs and
    platforms (independent of dict insertion order).
    """
    # ---- 0. Materialise the batch into a list (we need multiple passes). ----
    materialised: list[MemoryRecord] = list(batch)

    decisions: list[MergeDecision] = []
    counters: dict[str, int] = {
        DecisionKind.IMPORT_NEW.value: 0,
        DecisionKind.SKIP_LIVE_NEWER.value: 0,
        DecisionKind.REPLACE_LIVE.value: 0,
        DecisionKind.DEDUP_BATCH_HASH.value: 0,
        DecisionKind.DEDUP_BATCH_ID.value: 0,
    }
    resolved: list[ResolvedRecord] = []

    # ---- 1. Within-batch dedup by content_hash. --------------------------
    #   Group by content_hash. For each group, keep the record with the
    #   latest created_at; tie-broken by lex-smallest key for determinism.
    #   Others become DEDUP_BATCH_HASH decisions.
    by_hash: dict[str, list[MemoryRecord]] = {}
    for rec in materialised:
        by_hash.setdefault(content_hash(rec), []).append(rec)

    after_hash_dedup: list[MemoryRecord] = []
    for h, group in by_hash.items():
        if len(group) == 1:
            after_hash_dedup.append(group[0])
            continue
        winner = _pick_winner(group)
        for rec in group:
            if rec is winner:
                continue
            decisions.append(
                MergeDecision(
                    kind=DecisionKind.DEDUP_BATCH_HASH,
                    tier=rec.tier,
                    scope=rec.scope,
                    key=rec.key,
                    reason="duplicate content_hash within import batch",
                    content_hash=h,
                    created_at=rec.created_at,
                    payload={"winner_key": winner.key, "winner_created_at": winner.created_at},
                )
            )
            counters[DecisionKind.DEDUP_BATCH_HASH.value] += 1
        after_hash_dedup.append(winner)

    # ---- 2. Within-batch dedup by (tier, scope, key). --------------------
    #   After hash dedup, two records with the same identity MUST have
    #   different content (different content_hash). Keep the one with the
    #   latest created_at; emit DEDUP_BATCH_ID for the others.
    by_id: dict[tuple[str, str, str], list[MemoryRecord]] = {}
    for rec in after_hash_dedup:
        by_id.setdefault((rec.tier, rec.scope, rec.key), []).append(rec)

    after_id_dedup: list[MemoryRecord] = []
    for ident, group in by_id.items():
        if len(group) == 1:
            after_id_dedup.append(group[0])
            continue
        winner = _pick_winner(group)
        for rec in group:
            if rec is winner:
                continue
            decisions.append(
                MergeDecision(
                    kind=DecisionKind.DEDUP_BATCH_ID,
                    tier=rec.tier,
                    scope=rec.scope,
                    key=rec.key,
                    reason="duplicate (tier,scope,key) within import batch",
                    content_hash=content_hash(rec),
                    created_at=rec.created_at,
                    payload={
                        "winner_key": winner.key,
                        "winner_created_at": winner.created_at,
                        "winner_content_hash": content_hash(winner),
                    },
                )
            )
            counters[DecisionKind.DEDUP_BATCH_ID.value] += 1
        after_id_dedup.append(winner)

    # ---- 3. Cross-store dedup against the live store. -------------------
    #   For each remaining import record, ask live_lookup. If the live
    #   record is missing → IMPORT_NEW. If the live record's created_at
    #   is >= import's → SKIP_LIVE_NEWER. If import is newer →
    #   REPLACE_LIVE with additive metadata merge.
    stamp_now = (
        datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds")
        if now is not None
        else datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    for rec in after_id_dedup:
        h = content_hash(rec)
        existing: Optional[MemoryRecord] = None
        try:
            existing = live_lookup(rec.tier, rec.scope, rec.key)
        except Exception as e:  # noqa: BLE001
            # The lookup callable is documented as raising only on
            # permission errors; treat any other unexpected failure as
            # a SKIP with a structured reason so the import still
            # proceeds safely.
            decisions.append(
                MergeDecision(
                    kind=DecisionKind.SKIP_LIVE_NEWER,
                    tier=rec.tier,
                    scope=rec.scope,
                    key=rec.key,
                    reason=f"live_lookup raised {type(e).__name__}: {e}",
                    content_hash=h,
                    created_at=rec.created_at,
                )
            )
            counters[DecisionKind.SKIP_LIVE_NEWER.value] += 1
            continue

        if existing is None:
            decisions.append(
                MergeDecision(
                    kind=DecisionKind.IMPORT_NEW,
                    tier=rec.tier,
                    scope=rec.scope,
                    key=rec.key,
                    reason="no live record at this identity",
                    content_hash=h,
                    created_at=rec.created_at,
                )
            )
            counters[DecisionKind.IMPORT_NEW.value] += 1
            resolved.append(ResolvedRecord(record=rec, decision=decisions[-1]))
            continue

        # Compare created_at; "strictly newer" means import wins.
        # Same created_at → live wins (the import is at best a no-op).
        if not _is_strictly_newer(rec.created_at, existing.created_at):
            decisions.append(
                MergeDecision(
                    kind=DecisionKind.SKIP_LIVE_NEWER,
                    tier=rec.tier,
                    scope=rec.scope,
                    key=rec.key,
                    reason="live record has equal or newer created_at",
                    content_hash=h,
                    created_at=rec.created_at,
                    payload={
                        "live_created_at": existing.created_at,
                        "live_version": existing.version,
                    },
                )
            )
            counters[DecisionKind.SKIP_LIVE_NEWER.value] += 1
            continue

        # Import is newer → REPLACE_LIVE with additive metadata merge.
        merged_meta = _merge_metadata_additive(existing.metadata, rec.metadata)
        stamped = MemoryRecord(
            tier=rec.tier,
            scope=rec.scope,
            key=rec.key,
            value=rec.value,
            value_bytes=rec.value_bytes,
            metadata=merged_meta,
            created_by=rec.created_by or existing.created_by,
            created_at=rec.created_at,
            updated_by=rec.updated_by or existing.updated_by,
            updated_at=stamp_now,
            version=existing.version + 1,
            ttl_seconds=(
                rec.ttl_seconds if rec.ttl_seconds is not None
                else existing.ttl_seconds
            ),
        )
        decisions.append(
            MergeDecision(
                kind=DecisionKind.REPLACE_LIVE,
                tier=rec.tier,
                scope=rec.scope,
                key=rec.key,
                reason="import created_at is newer; live will be replaced",
                content_hash=h,
                created_at=rec.created_at,
                payload={
                    "live_created_at": existing.created_at,
                    "live_version": existing.version,
                    "merged_metadata_keys": sorted(merged_meta.keys()),
                    "tags_added": _tags_added(existing.metadata, rec.metadata),
                },
            )
        )
        counters[DecisionKind.REPLACE_LIVE.value] += 1
        resolved.append(
            ResolvedRecord(
                record=stamped,
                decision=decisions[-1],
                merged_metadata=merged_meta,
            )
        )

    return MergeResult(
        resolved=resolved,
        decisions=decisions,
        counters=counters,
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _pick_winner(group: list[MemoryRecord]) -> MemoryRecord:
    """Deterministic tie-break: latest created_at, then lex-smallest key."""
    return max(
        group,
        key=lambda r: (_parse_iso(r.created_at).timestamp(), _neg_key(r.key)),
    )


def _neg_key(key: str) -> str:
    """Inverse-lex key for ``max()`` to pick the lex-smallest value."""
    # ``max()`` on strings picks the lex-greatest; we want lex-smallest,
    # so we negate by reversing each code point. Using tuple-of-ords is
    # unambiguous for ASCII.
    return "".join(chr(0x10FFFF - ord(c)) for c in key)


def _is_strictly_newer(candidate_iso: str, baseline_iso: str) -> bool:
    """True if ``candidate_iso`` is strictly later than ``baseline_iso``.
    Same timestamp → False (live wins ties per spec §a)."""
    try:
        cand = _parse_iso(candidate_iso)
        base = _parse_iso(baseline_iso)
    except ValueError:
        # On parse failure, treat as "not strictly newer" — the live
        # record is the safer default. The bootstrap loader already
        # validates ISO format upstream, so this only fires on garbage.
        return False
    return cand > base


def _merge_metadata_additive(
    live_meta: dict[str, Any],
    import_meta: dict[str, Any],
) -> dict[str, Any]:
    """Additive metadata merge per rule (c).

    * Keys in :data:`_ADDITIVE_METADATA_KEYS` (currently ``tags``) are
      unioned: order-preserving, deduped, no ``None``/empty entries.
    * Other keys: import wins on novelty (key absent from live); live
      wins on existence (key present in both → live value kept, even
      if import's value is ``None`` or empty — rule (c) says "never
      overwrite a live scalar with None or empty").
    """
    out: dict[str, Any] = dict(live_meta)  # start with live snapshot
    for k, v in import_meta.items():
        if k in _ADDITIVE_METADATA_KEYS:
            out[k] = _union_tags(out.get(k), v)
            continue
        if k in out:
            # Live already has this key — preserve live value unless
            # import introduces a *different* non-empty value.
            live_v = out[k]
            if v is None or v == "" or v == [] or v == {}:
                continue
            if _values_equal(live_v, v):
                continue
            # Different non-empty value — import wins (it is "newer"
            # content by definition at this point).
            out[k] = v
        else:
            # Live lacks this key — take the import value as-is unless
            # it's empty/None.
            if v is None or v == "" or v == [] or v == {}:
                continue
            out[k] = v
    return out


def _union_tags(*sources: Any) -> list[str]:
    """Order-preserving union of tag lists/values. Non-string entries
    are coerced via ``str()``."""
    seen: set[str] = set()
    out: list[str] = []
    for src in sources:
        if src is None:
            continue
        if isinstance(src, str):
            items = [src]
        elif isinstance(src, (list, tuple, set, frozenset)):
            items = list(src)
        else:
            items = [src]
        for item in items:
            if item is None or item == "":
                continue
            tag = item if isinstance(item, str) else str(item)
            if tag in seen:
                continue
            seen.add(tag)
            out.append(tag)
    return out


def _values_equal(a: Any, b: Any) -> bool:
    """Loose equality for metadata values. Lists compare as sets if both
    are list-of-strings (handles the common ``tags`` case where order
    is not semantically meaningful)."""
    if isinstance(a, list) and isinstance(b, list):
        try:
            return sorted(map(str, a)) == sorted(map(str, b))
        except TypeError:
            return a == b
    return a == b


def _tags_added(live_meta: dict[str, Any], import_meta: dict[str, Any]) -> list[str]:
    """Diagnostic: tags present in the merged output that weren't in
    ``live_meta.tags``."""
    live_tags = set(_coerce_tags(live_meta.get("tags")))
    merged_tags = set(_union_tags(live_meta.get("tags"), import_meta.get("tags")))
    return sorted(merged_tags - live_tags)


def _coerce_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(v) for v in value if v is not None and v != ""]
    return [str(value)]


# --------------------------------------------------------------------------- #
# Live store adapter
# --------------------------------------------------------------------------- #


class LiveLookup:
    """Adapter the merge pass uses to ask the live store whether a
    record at ``(tier, scope, key)`` exists.

    Decoupling here keeps the pass testable without instantiating a
    real ``MemoryStore``; production callers wrap the store::

        from agentchat.memory_store_agent import (
            AgentMemoryStore, KeyNotFound,
        )

        class _StoreLookup(LiveLookup):
            def __init__(self, store: AgentMemoryStore, actor: str):
                self._store = store
                self._actor = actor
            def __call__(self, tier, scope, key) -> MemoryRecord | None:
                try:
                    return self._store.get(
                        agent_id=scope, key=key, actor=self._actor,
                    )
                except KeyNotFound:
                    return None
    """

    def __call__(
        self, tier: str, scope: str, key: str,
    ) -> Optional[MemoryRecord]:
        ...


__all__ = [
    "DecisionKind",
    "LiveLookup",
    "MergeDecision",
    "MergeResult",
    "ResolvedRecord",
    "content_hash",
    "merge_records",
]

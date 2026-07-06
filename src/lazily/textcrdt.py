"""Free-text character CRDT — ``TextCrdt`` (Fugue/RGA-style).

The Python counterpart of ``lazily-spec/cell-model.md`` § "Free-text CRDT +
re-parse" and § "Delta sync (#lztextsync)". For anchorless prose under concurrent
edits the merge unit drops to **characters**: each char is an element with a
unique :class:`OpId` ``(counter, peer)`` plus a left origin; deletes are sticky
tombstones carrying the delete op id. Order is a pure deterministic function of
the element set, so merge is commutative, associative, idempotent, and concurrent
same-point inserts converge with both preserved.

The structural tree is then a *projection* of the merged text
(re-parse → manufactured-identity keys → reconcile), not the merge unit.

Ordering rule (the load-bearing deterministic function)::

    pre-order DFS of the origin tree, siblings sorted DESCENDING by OpId
    (most-recent first). Concurrent same-point inserts keep both, ordered by
    peer tiebreak.

Delta sync pins three operations over the same element set:

- :meth:`TextCrdt.version_vector` — greatest ``OpId`` counter per peer, taken
  over **both** insert ids and tombstone (delete) ids;
- :meth:`TextCrdt.delta_since` — the ops a partner has not observed; and
- :meth:`TextCrdt.apply_delta` — the same commutative/associative/idempotent
  algebra as :meth:`TextCrdt.merge`.

The executable reference behind ``conformance/collections/textcrdt_convergence.json``
and ``conformance/collections/textcrdt_delta_sync.json``.
"""

from __future__ import annotations


__all__ = [
    "ROOT",
    "OpId",
    "TextCrdt",
    "TextElement",
    "TextOp",
]


from dataclasses import dataclass
from typing import Any


# Sentinel for the left origin "before everything" (the start-of-buffer anchor).
# A bare ``None`` cannot serve because elements are addressed by OpId tuples.
ROOT = ("root", -1)


@dataclass(frozen=True)
class OpId:
    """Unique identifier for one element op — ``(counter, peer)``.

    The Lamport-style id under the per-peer monotonic counter. The total order
    on ids is lexicographic ``(counter, peer)``; sibling elements are sorted
    DESCENDING by id (most-recent first), so concurrent same-point inserts keep
    both and order by peer tiebreak.
    """

    counter: int
    peer: int

    def __lt__(self, other: OpId) -> bool:
        return (self.counter, self.peer) < (other.counter, other.peer)

    def __le__(self, other: OpId) -> bool:
        return (self.counter, self.peer) <= (other.counter, other.peer)

    def to_json(self) -> list[int]:
        return [self.counter, self.peer]

    @classmethod
    def from_json(cls, data: Any) -> OpId:
        return cls(counter=int(data[0]), peer=int(data[1]))


# Origin reference: either ROOT or a concrete OpId.
Origin = Any


@dataclass
class TextElement:
    """One character CRDT element.

    ``id`` is the element's unique insert id. ``origin`` is the left origin
    (the element that preceded this one at insert time, or :data:`ROOT` for a
    front-of-buffer insert). ``deleted`` flags the sticky tombstone; when set,
    ``delete_id`` carries the delete op's id (the load-bearing identity for
    sticky-minimal tombstone merge and for GC).
    """

    id: OpId
    ch: str
    origin: Origin
    deleted: bool = False
    delete_id: OpId | None = None


@dataclass(frozen=True)
class TextOp:
    """Transport form of one element — the ``delta_since`` payload shape.

    Mirrors the spec: ``{ id, ch, origin, deleted }`` (and a delete id when
    deleted). Re-applying a delta is a no-op (:meth:`TextCrdt.apply_delta`).
    """

    id: OpId
    ch: str
    origin: Origin
    deleted: bool
    delete_id: OpId | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id.to_json(),
            "ch": self.ch,
            "origin": _origin_to_json(self.origin),
            "deleted": self.deleted,
        }
        if self.delete_id is not None:
            out["delete_id"] = self.delete_id.to_json()
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TextOp:
        return cls(
            id=OpId.from_json(data["id"]),
            ch=data["ch"],
            origin=_origin_from_json(data["origin"]),
            deleted=bool(data["deleted"]),
            delete_id=(
                OpId.from_json(data["delete_id"]) if data.get("delete_id") else None
            ),
        )


def _origin_to_json(origin: Origin) -> Any:
    if origin is ROOT:
        return None
    return origin.to_json()


def _origin_from_json(data: Any) -> Origin:
    if data is None:
        return ROOT
    return OpId.from_json(data)


class TextCrdt:
    """Fugue/RGA-style character CRDT — the free-text merge unit.

    The element set is the single source of truth; the visible text is a pure
    projection. Merge is commutative, associative, idempotent; tombstones are
    sticky (a concurrent delete keeps the smaller delete id); GC is
    causal-stability-gated (a stable deleted leaf with no survivors referencing
    it as a left origin is collectable).
    """

    __slots__ = ("_by_id", "_counter", "peer")

    def __init__(self, peer: int) -> None:
        self.peer = peer
        self._counter = 0
        # OpId.counter is unique per peer, so (counter, peer) keys the dict.
        self._by_id: dict[tuple[int, int], TextElement] = {}

    # -- seed / clone --------------------------------------------------- #

    @classmethod
    def seed(cls, peer: int, text: str) -> TextCrdt:
        """Construct a replica pre-populated with ``text`` at ``peer``."""
        buf = cls(peer)
        origin: Origin = ROOT
        for ch in text:
            buf._counter += 1
            eid = OpId(buf._counter, peer)
            buf._by_id[eid.counter, eid.peer] = TextElement(eid, ch, origin)
            origin = eid
        return buf

    def clone(self) -> TextCrdt:
        """A deep copy of this replica (same peer, same ids, same state)."""
        dup = TextCrdt(self.peer)
        dup._counter = self._counter
        dup._by_id = {
            key: TextElement(
                e.id, e.ch, e.origin, e.deleted, e.delete_id
            )
            for key, e in self._by_id.items()
        }
        return dup

    # -- local ops ------------------------------------------------------ #

    def _next_id(self) -> OpId:
        self._counter += 1
        return OpId(self._counter, self.peer)

    def _visible_ordered(self) -> list[TextElement]:
        """Every element (including tombstones) in deterministic pre-order DFS
        order — siblings DESCENDING by OpId (most-recent first)."""
        # Group children by origin.
        children_by_origin: dict[Origin, list[TextElement]] = {}
        for elem in self._by_id.values():
            children_by_origin.setdefault(elem.origin, []).append(elem)
        for kids in children_by_origin.values():
            kids.sort(key=lambda e: e.id, reverse=True)  # DESCENDING
        ordered: list[TextElement] = []

        def visit(origin: Origin) -> None:
            for child in children_by_origin.get(origin, []):
                ordered.append(child)
                visit(child.id)

        visit(ROOT)
        return ordered

    def _index_to_origin(self, index: int) -> tuple[Origin, int]:
        """Resolve a visible-text insertion index to ``(origin, slot_index)``.

        ``slot_index`` is the position among the visible (non-deleted) elements
        where the new char will appear. Returns the left origin to use.
        """
        visible = [e for e in self._visible_ordered() if not e.deleted]
        if index <= 0:
            return ROOT, 0
        if index >= len(visible):
            # Append after the last visible element.
            return (visible[-1].id if visible else ROOT), len(visible)
        # Insert before visible[index]: origin = visible[index-1].id
        return visible[index - 1].id, index

    def insert(self, index: int, ch: str) -> OpId:
        """Insert ``ch`` so it becomes the ``index``-th visible character."""
        if len(ch) != 1:
            raise ValueError("insert takes exactly one character")
        origin, _ = self._index_to_origin(index)
        eid = self._next_id()
        self._by_id[eid.counter, eid.peer] = TextElement(eid, ch, origin)
        return eid

    def insert_str(self, index: int, text: str) -> list[OpId]:
        """Insert a multi-char string at ``index``; each char chains left."""
        origin, _ = self._index_to_origin(index)
        ids: list[OpId] = []
        for ch in text:
            eid = self._next_id()
            self._by_id[eid.counter, eid.peer] = TextElement(eid, ch, origin)
            ids.append(eid)
            origin = eid
        return ids

    def delete(self, index: int) -> OpId | None:
        """Tombstone the visible char at ``index``. Returns the delete op id."""
        visible = [e for e in self._visible_ordered() if not e.deleted]
        if index < 0 or index >= len(visible):
            return None
        target = visible[index]
        delete_id = self._next_id()
        target.deleted = True
        target.delete_id = delete_id
        return delete_id

    # -- merge algebra -------------------------------------------------- #

    def merge(self, other: TextCrdt) -> bool:
        """Merge ``other`` into ``self`` (commutative/assoc/idempotent).

        Returns whether ``self`` gained any element or tombstone update.
        """
        changed = False
        for key, other_elem in other._by_id.items():
            existing = self._by_id.get(key)
            if existing is None:
                self._by_id[key] = TextElement(
                    other_elem.id,
                    other_elem.ch,
                    other_elem.origin,
                    other_elem.deleted,
                    other_elem.delete_id,
                )
                changed = True
                continue
            # Sticky-minimal tombstone: a deleted element stays deleted; the
            # delete id keeps the smaller one (causally-earlier delete wins).
            if other_elem.deleted and not existing.deleted:
                existing.deleted = True
                if (
                    existing.delete_id is None
                    or (
                        other_elem.delete_id is not None
                        and other_elem.delete_id < existing.delete_id
                    )
                ):
                    existing.delete_id = other_elem.delete_id
                changed = True
            elif (
                other_elem.deleted
                and existing.deleted
                and other_elem.delete_id is not None
                and existing.delete_id is not None
                and other_elem.delete_id < existing.delete_id
            ):
                existing.delete_id = other_elem.delete_id
                changed = True
        # Advance the local Lamport counter past every observed id. The counter
        # is the Lamport clock; the peer is the node id — observing any op
        # ``(c, p)`` advances the local counter past ``c`` so the next local id
        # is fresh regardless of which peer produced the observed op.
        for counter, _peer in self._by_id:
            if counter > self._counter:
                self._counter = counter
        return changed

    # -- projections ---------------------------------------------------- #

    def text(self) -> str:
        """The visible text — non-deleted elements in deterministic order."""
        return "".join(
            e.ch for e in self._visible_ordered() if not e.deleted
        )

    def __len__(self) -> int:
        """The visible character count (tombstones excluded)."""
        return sum(1 for e in self._visible_ordered() if not e.deleted)

    def tombstone_count(self) -> int:
        return sum(1 for e in self._by_id.values() if e.deleted)

    def elements(self) -> list[TextElement]:
        return list(self._by_id.values())

    # -- delta sync (#lztextsync) --------------------------------------- #

    def version_vector(self) -> dict[int, int]:
        """The greatest OpId counter per peer over BOTH inserts and tombstones.

        Absent peer ⇒ 0. An op ``(c, p)`` is unknown to a partner iff
        ``c > their_vv[p]`` (absent peer = 0). The compact frontier a replica
        publishes.
        """
        vv: dict[int, int] = {}
        for elem in self._by_id.values():
            peer = elem.id.peer
            if elem.id.counter > vv.get(peer, 0):
                vv[peer] = elem.id.counter
            if elem.delete_id is not None and elem.delete_id.peer == peer:
                if elem.delete_id.counter > vv.get(peer, 0):
                    vv[peer] = elem.delete_id.counter
            elif elem.delete_id is not None:
                dpeer = elem.delete_id.peer
                if elem.delete_id.counter > vv.get(dpeer, 0):
                    vv[dpeer] = elem.delete_id.counter
        return vv

    def delta_since(self, their_vv: dict[int, int]) -> list[TextOp]:
        """The ops this replica holds that ``their_vv`` has not observed.

        Elements whose insert id is newer, plus elements whose tombstone id is
        newer (a fresh deletion of an already-shared element). A whole-state
        snapshot is ``delta_since({})``.
        """
        ops: list[TextOp] = []
        for elem in self._by_id.values():
            peer = elem.id.peer
            if elem.id.counter > their_vv.get(peer, 0):
                ops.append(
                    TextOp(elem.id, elem.ch, elem.origin, elem.deleted, elem.delete_id)
                )
                continue
            if (
                elem.deleted
                and elem.delete_id is not None
                and elem.delete_id.counter > their_vv.get(elem.delete_id.peer, 0)
            ):
                ops.append(
                    TextOp(elem.id, elem.ch, elem.origin, elem.deleted, elem.delete_id)
                )
        return ops

    def apply_delta(self, ops: list[TextOp]) -> bool:
        """Apply a delta op list with the SAME algebra as :meth:`merge`.

        Commutative, associative, idempotent: re-applying a delta is a no-op.
        Identity preservation is the load-bearing property — every character's
        :class:`OpId` is retained, so a later concurrent edit merges without
        duplication.
        """
        changed = False
        for op in ops:
            key = (op.id.counter, op.id.peer)
            existing = self._by_id.get(key)
            if existing is None:
                self._by_id[key] = TextElement(
                    op.id, op.ch, op.origin, op.deleted, op.delete_id
                )
                changed = True
                continue
            if op.deleted and not existing.deleted:
                existing.deleted = True
                if existing.delete_id is None or (
                    op.delete_id is not None and op.delete_id < existing.delete_id
                ):
                    existing.delete_id = op.delete_id
                changed = True
            elif (
                op.deleted
                and existing.deleted
                and op.delete_id is not None
                and existing.delete_id is not None
                and op.delete_id < existing.delete_id
            ):
                existing.delete_id = op.delete_id
                changed = True
        # Advance the local Lamport counter past every observed id (see merge).
        for counter, _peer in self._by_id:
            if counter > self._counter:
                self._counter = counter
        return changed

    # -- tombstone GC --------------------------------------------------- #

    def gc(self, stable: bool) -> int:
        """Causal-stability-gated tombstone collection.

        Collectable only once *every* replica has observed the deletion
        (``stable=True`` — the version-vector frontier, never a single replica's
        clock). The character layer is conservative: a stable deleted element is
        reclaimed only when nothing references it as a left origin, so removal
        never orphans a survivor.

        Returns the number of tombstones collected.
        """
        if not stable:
            return 0
        referenced_origins = {
            (e.origin.counter, e.origin.peer)
            for e in self._by_id.values()
            if e.origin is not ROOT
        }
        survivors: dict[tuple[int, int], TextElement] = {}
        collected = 0
        for key, elem in self._by_id.items():
            if elem.deleted and key not in referenced_origins:
                collected += 1
                continue
            survivors[key] = elem
        self._by_id = survivors
        return collected

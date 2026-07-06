"""Distributed CRDT plane — ``CrdtPlaneRuntime`` (anti-entropy).

The Python counterpart of ``lazily-spec/protocol.md`` § "Distributed: CRDT Cell
Plane". The plane rides the same ``lazily-ipc`` transport as ``Snapshot`` /
``Delta`` as a third ``IpcMessage`` variant (:class:`lazily.ipc.CrdtSync`). It
is **state-based** (CvRDT): each :class:`lazily.ipc.CrdtOp` ships the converged
register state for a node, tagged with a :class:`lazily.ipc.WireStamp`; the
receiver merges it into its local replica. Merges are commutative, associative,
and idempotent, so out-of-order, duplicated, or batched delivery all converge.

This binding models LWW root cells where the plane ``WireStamp`` IS the
register's decisive stamp: for a given ``(node, key)`` the winning state is the
op with the greatest ``WireStamp`` under lexicographic
``(wall_time, logical, peer)`` order (peer id is the final tiebreak — matching
the ``anti_entropy_converge`` conformance fixture). Op-log dedup is keyed by
``(node, stamp)`` so re-delivering an already-seen frame applies 0 new ops.

The runtime also maintains the per-peer **stamp frontier** (the highest
``WireStamp`` observed from each peer) and the **causal-stability watermark**
(the ``min`` over membership of that frontier — the causal point every replica
has provably passed, which gates tombstone GC).

The executable reference behind ``conformance/distributed/anti_entropy_converge.json``
and ``conformance/distributed/crdt_sync_frames.json``.
"""

from __future__ import annotations


__all__ = [
    "CrdtPlaneRuntime",
    "PlaneEntry",
    "stamp_key",
]


from dataclasses import dataclass

from .ipc import CrdtOp, CrdtSync, IpcValue_Inline, NodeId, NodeKey, WireStamp


def stamp_key(stamp: WireStamp) -> tuple[int, int, int]:
    """The lexicographic ``(wall_time, logical, peer)`` total order on a stamp."""
    return (stamp.wall_time, stamp.logical, stamp.peer)


@dataclass
class PlaneEntry:
    """One converged node entry in the plane — the winning op for ``(node, key)``.

    The plane ``WireStamp`` IS the LWW register's decisive stamp.
    """

    node: NodeId
    key: NodeKey | None
    stamp: WireStamp
    state: bytes

    def matches(self, node: NodeId, key: NodeKey | None) -> bool:
        if self.node != node:
            return False
        return (self.key is None and key is None) or (
            self.key is not None
            and key is not None
            and self.key.path == key.path
        )


class CrdtPlaneRuntime:
    """Anti-entropy CRDT plane runtime — state-based CvRDT ingress.

    Ingests :class:`CrdtOp` s (each a converged state-based register value tagged
    with a :class:`WireStamp`) and converges to the greatest-stamp winner per
    ``(node, key)`` regardless of delivery order. Re-ingesting an already-seen
    frame applies 0 new ops (op-log dedup keyed by ``(node, stamp)``).

    The frontier exchange is bounded, idempotent, and resumable: each peer keeps
    its per-peer highest observed stamp; the causal-stability watermark is the
    ``min`` over membership of the frontier — the causal point every replica has
    provably passed.
    """

    __slots__ = (
        "_applied",
        "_entries",
        "_frontier",
        "_self_peer",
    )

    def __init__(self, self_peer: int = 0) -> None:
        self._self_peer = self_peer
        self._entries: list[PlaneEntry] = []
        # Op-log dedup keyed by (node, stamp_key) — idempotent redelivery.
        self._applied: set[tuple[NodeId, tuple[int, int, int]]] = set()
        # Per-peer highest observed stamp (the frontier this runtime publishes).
        self._frontier: dict[int, WireStamp] = {}

    # -- ingest --------------------------------------------------------- #

    def apply(self, op: CrdtOp) -> bool:
        """Ingest one state-based :class:`CrdtOp`. Returns whether it applied.

        "Applied" means the op was newly ingested into the op log — i.e. its
        ``(node, stamp)`` was not previously seen. An already-seen op is a no-op
        (state-based CvRDT idempotence — re-delivery applies 0 new ops). A newly
        ingested op applies ``True`` regardless of whether it becomes the
        winning entry for ``(node, key)``; the winner is always the greatest
        stamp.
        """
        dedup_key = (op.node, stamp_key(op.stamp))
        if dedup_key in self._applied:
            return False
        self._applied.add(dedup_key)

        # Frontier advance: observe the producer peer.
        producer = op.stamp.peer
        cur = self._frontier.get(producer)
        if cur is None or stamp_key(op.stamp) > stamp_key(cur):
            self._frontier[producer] = op.stamp

        payload = (
            op.state.data
            if isinstance(op.state, IpcValue_Inline)
            else b""
        )
        entry = PlaneEntry(op.node, op.key, op.stamp, payload)
        idx = self._find(op.node, op.key)
        if idx is None:
            self._entries.append(entry)
            return True
        existing = self._entries[idx]
        if stamp_key(op.stamp) > stamp_key(existing.stamp):
            self._entries[idx] = entry
        return True

    def apply_frame(self, frame: CrdtSync) -> int:
        """Ingest a whole anti-entropy frame; returns the count of newly-applied
        ops (0 for an idempotent re-delivery)."""
        applied = 0
        for op in frame.ops:
            if self.apply(op):
                applied += 1
        # Merge the sender's frontier (per-peer max) into ours.
        for peer, stamp in frame.frontier:
            cur = self._frontier.get(peer)
            if cur is None or stamp_key(stamp) > stamp_key(cur):
                self._frontier[peer] = stamp
        return applied

    def apply_ops(self, ops: list[CrdtOp]) -> int:
        """Ingest a list of ops (the ``anti_entropy_converge`` replay shape)."""
        applied = 0
        for op in ops:
            if self.apply(op):
                applied += 1
        return applied

    # -- convergence ---------------------------------------------------- #

    def _find(self, node: NodeId, key: NodeKey | None) -> int | None:
        for i, entry in enumerate(self._entries):
            if entry.matches(node, key):
                return i
        return None

    def converged(self) -> list[PlaneEntry]:
        """The current winner per ``(node, key)`` in insertion order."""
        return list(self._entries)

    def applied_count(self) -> int:
        """Total ops ever applied (dedup-keyed — redeliveries excluded)."""
        return len(self._applied)

    # -- frontier / watermark ------------------------------------------- #

    def frontier(self) -> list[tuple[int, WireStamp]]:
        """This runtime's per-peer highest-observed-stamp frontier."""
        return sorted(self._frontier.items())

    def stability_watermark(self) -> WireStamp | None:
        """The causal-stability watermark — the ``min`` over frontier membership.

        ``None`` when the frontier is empty. The causal point every replica has
        provably passed; a tombstone whose delete stamp is ``<=`` it is
        collectable on *every* replica.
        """
        if not self._frontier:
            return None
        return min(self._frontier.values(), key=stamp_key)

    # -- publish -------------------------------------------------------- #

    def to_sync(self) -> CrdtSync:
        """Publish a :class:`CrdtSync` frame: this runtime's frontier plus the
        current converged op batch."""
        ops = [
            CrdtOp(
                node=entry.node,
                key=entry.key,
                stamp=entry.stamp,
                state=IpcValue_Inline(entry.state),
            )
            for entry in self._entries
        ]
        return CrdtSync(frontier=self.frontier(), ops=ops)

    def delta_sync(self, their_frontier: list[tuple[int, WireStamp]]) -> CrdtSync:
        """Publish only ops whose stamp is newer than ``their_frontier``.

        Anti-entropy: a partner publishes ``their_frontier``; we reply with the
        subset of our ops the partner has not observed, plus our full frontier
        (so the partner can recompute its watermark).
        """
        their = {peer: stamp_key(s) for peer, s in their_frontier}
        ops = []
        for entry in self._entries:
            peer = entry.stamp.peer
            if stamp_key(entry.stamp) > their.get(peer, (0, 0, 0)):
                ops.append(
                    CrdtOp(
                        node=entry.node,
                        key=entry.key,
                        stamp=entry.stamp,
                        state=IpcValue_Inline(entry.state),
                    )
                )
        return CrdtSync(frontier=self.frontier(), ops=ops)

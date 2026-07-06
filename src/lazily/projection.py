"""State projection / mirror — :class:`StateMirror`.

The Python counterpart of ``lazily-spec/protocol.md`` § "IPC Snapshot &
Incremental Update Protocol" and the value-mirror default. A :class:`StateMirror`
projects one local reactive context (its cells and slots) onto the
:class:`lazily.ipc.Snapshot` / :class:`lazily.ipc.Delta` wire plane, so a Python
graph's state can be mirrored to remote observers across processes and
languages.

The default is the **value-mirror**: at flush, the sender resolves each
invalidated allowlisted slot so the delta carries concrete :class:`DeltaOp_SlotValue`
ops (the receiver holds no compute closures). An eager :class:`lazily.signal.Signal`
whose value changed publishes a ``SlotValue`` for its backing slot; an equal
recompute (the memo guard) suppresses both ``SlotValue`` and downstream
invalidation.

A :class:`PeerPermissions` boundary gates what is shared — non-readable nodes
are **omitted entirely** (not redacted). Epochen sequencing is monotonic per
outermost flush.
"""

from __future__ import annotations


__all__ = ["MirrorEntry", "MirrorKind", "StateMirror"]


from dataclasses import dataclass
from enum import Enum

from .ipc import (
    Delta,
    DeltaOp,
    DeltaOp_Invalidate,
    EdgeSnapshot,
    NodeId,
    NodeSnapshot,
    NodeState_Opaque,
    NodeState_Payload,
    PeerId,
    PeerPermissions,
    Snapshot,
)


class MirrorKind(Enum):
    """The kind of a mirrored node — a source ``cell`` or a derived ``slot``."""

    CELL = "cell"
    SLOT = "slot"


@dataclass
class MirrorEntry:
    """One tracked node in the mirror — its kind, type tag, and last payload."""

    node: NodeId
    kind: MirrorKind
    type_tag: str
    payload: bytes | None = None
    last_published_epoch: int = -1

    def is_cell(self) -> bool:
        return self.kind is MirrorKind.CELL


class StateMirror:
    """Project a local reactive context onto the lazily-ipc wire plane.

    Track cells (``track_cell``) and slots (``track_slot``); edges record the
    dependent → dependency relationships. :meth:`snapshot` produces a fresh
    :class:`Snapshot`; :meth:`flush_delta` produces the incremental
    :class:`Delta` carrying the mutations since the last flush (value-mirror
    default: concrete ``SlotValue`` ops).

    A :class:`PeerPermissions` boundary filters both — non-readable nodes are
    omitted entirely (omission, not redaction).
    """

    __slots__ = (
        "_edges",
        "_epoch",
        "_nodes",
        "_pending_cell_sets",
        "_pending_invalidations",
        "permissions",
    )

    def __init__(self, permissions: PeerPermissions | None = None) -> None:
        self._epoch: int = 0
        self._nodes: dict[NodeId, MirrorEntry] = {}
        self._edges: list[EdgeSnapshot] = []
        self._pending_cell_sets: dict[NodeId, bytes] = {}
        self._pending_invalidations: set[NodeId] = set()
        self.permissions = permissions if permissions is not None else PeerPermissions()

    # -- registration --------------------------------------------------- #

    def track_cell(
        self, node: NodeId, type_tag: str, payload: bytes | None = None
    ) -> None:
        """Register a source cell node (root input). Idempotent."""
        entry = self._nodes.get(node)
        if entry is None:
            self._nodes[node] = MirrorEntry(
                node=node, kind=MirrorKind.CELL, type_tag=type_tag, payload=payload
            )
            return
        entry.kind = MirrorKind.CELL
        entry.type_tag = type_tag
        if payload is not None:
            entry.payload = payload

    def track_slot(
        self, node: NodeId, type_tag: str, payload: bytes | None = None
    ) -> None:
        """Register a derived slot node. Idempotent."""
        entry = self._nodes.get(node)
        if entry is None:
            self._nodes[node] = MirrorEntry(
                node=node, kind=MirrorKind.SLOT, type_tag=type_tag, payload=payload
            )
            return
        entry.kind = MirrorKind.SLOT
        entry.type_tag = type_tag
        if payload is not None:
            entry.payload = payload

    def add_edge(self, dependent: NodeId, dependency: NodeId) -> None:
        """Record a dependency edge ``dependent → dependency``."""
        edge = EdgeSnapshot(dependent=dependent, dependency=dependency)
        if edge not in self._edges:
            self._edges.append(edge)

    # -- mutations ------------------------------------------------------ #

    def publish_cell(self, node: NodeId, payload: bytes) -> None:
        """Record a source-cell value change (PartialEq-guarded at flush)."""
        entry = self._nodes.get(node)
        if entry is None:
            raise KeyError(node)
        entry.payload = payload
        self._pending_cell_sets[node] = payload

    def invalidate_slot(self, node: NodeId, payload: bytes | None = None) -> None:
        """Record a slot invalidation; the value-mirror resolves it at flush.

        If ``payload`` is supplied the slot is resolved immediately and will
        appear in the flush as a concrete ``SlotValue``; otherwise it stays a
        bare ``Invalidate`` (the mirror-lazy form).
        """
        entry = self._nodes.get(node)
        if entry is None:
            raise KeyError(node)
        if payload is not None:
            entry.payload = payload
        self._pending_invalidations.add(node)

    # -- snapshots ------------------------------------------------------ #

    def snapshot(self) -> Snapshot:
        """A fresh full :class:`Snapshot` at the current epoch.

        Non-readable nodes (per :attr:`permissions`, applied to a default peer)
        are omitted entirely; edges are retained only when both endpoints are
        readable. Call :meth:`bump_epoch` first to advance the epoch if needed.
        """
        snap = Snapshot(
            epoch=self._epoch,
            nodes=[self._node_snapshot(n) for n in sorted(self._nodes)],
            edges=list(self._edges),
            roots=sorted(n for n in self._nodes if self._nodes[n].is_cell()),
        )
        return snap

    def _node_snapshot(self, node: NodeId) -> NodeSnapshot:
        entry = self._nodes[node]
        if entry.payload is None:
            return NodeSnapshot(node=node, type_tag=entry.type_tag, state=NodeState_Opaque())
        return NodeSnapshot(
            node=node,
            type_tag=entry.type_tag,
            state=NodeState_Payload(entry.payload),
        )

    # -- delta flush ---------------------------------------------------- #

    def bump_epoch(self) -> int:
        """Advance the internal epoch counter; returns the new value."""
        self._epoch += 1
        return self._epoch

    def flush_delta(self) -> Delta:
        """Flush the accumulated mutations into a sequential :class:`Delta`.

        The value-mirror default: each invalidated allowlisted slot whose
        payload is known resolves to a concrete ``SlotValue``; unresolved slots
        emit a bare ``Invalidate`` (the mirror-lazy form). Cell changes emit
        ``CellSet``. Equality guard: an unchanged payload suppresses its op.
        """
        ops: list[DeltaOp] = []
        # CellSet ops first (root input changes), in node order.
        for node in sorted(self._pending_cell_sets):
            payload = self._pending_cell_sets[node]
            entry = self._nodes.get(node)
            if entry is None or (entry.last_published_epoch == self._epoch and entry.payload == payload):
                continue
            ops.append(DeltaOp.cell_set(node, payload))
            entry.last_published_epoch = self._epoch
        # Slot ops (value-mirror resolves each invalidated slot).
        for node in sorted(self._pending_invalidations):
            entry = self._nodes.get(node)
            if entry is None:
                continue
            if entry.payload is not None:
                ops.append(DeltaOp.slot_value(node, entry.payload))
            else:
                ops.append(DeltaOp_Invalidate(node))
            entry.last_published_epoch = self._epoch
        self._pending_cell_sets.clear()
        self._pending_invalidations.clear()
        base = self._epoch
        return Delta.next(base, ops)

    def flush_and_bump(self) -> Delta:
        """Convenience: flush a delta and advance the epoch in one step.

        The resulting delta carries ``base_epoch = old_epoch`` and
        ``epoch = old_epoch + 1``; the next flush's ``base_epoch`` is the new
        epoch. Mirrors the outermost-batch flush contract.
        """
        delta = self.flush_delta()
        self.bump_epoch()
        return delta

    # -- permission-filtered projections -------------------------------- #

    def snapshot_for(self, peer: PeerId) -> Snapshot:
        """A peer-specific :class:`Snapshot` that omits non-readable nodes."""
        return self.snapshot().filter_readable(self.permissions, peer)

    def delta_for(self, peer: PeerId, delta: Delta) -> Delta:
        """A peer-specific :class:`Delta` that omits non-readable ops."""
        return delta.filter_readable(self.permissions, peer)

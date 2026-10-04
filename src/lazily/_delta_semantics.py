"""Hand-written delta semantics mixed into the generated wire types.

``_delta_wire_gen`` is generated from ``lazily-spec/schemas/delta.json`` and
holds only declarations and strict codecs. Behaviour that is not a property of
the wire format stays here: the constructor helpers mirroring lazily-rs,
per-peer read filtering, and the receiver's epoch decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from ._delta_wire_gen import Delta, DeltaOp, IpcValue, NodeState
    from ._wire_scalars import NodeKey, ShmBlobRef
    from .ipc import DeltaApplyStatus, NodeId, PeerId, PeerPermissions


class IpcValueSemantics:
    """Coercion into an :class:`~lazily.ipc.IpcValue`."""

    __slots__ = ()

    @staticmethod
    def of(value: IpcValue | ShmBlobRef | bytes | bytearray) -> IpcValue:
        """Coerce bytes / a blob ref into an :class:`~lazily.ipc.IpcValue`."""
        from ._delta_wire_gen import IpcValue, IpcValue_Inline, IpcValue_SharedBlob
        from ._wire_scalars import ShmBlobRef

        if isinstance(value, IpcValue):
            return value
        if isinstance(value, ShmBlobRef):
            return IpcValue_SharedBlob(value)
        if isinstance(value, (bytes, bytearray)):
            return IpcValue_Inline(bytes(value))
        raise TypeError(f"cannot coerce {type(value).__name__} into IpcValue")


class DeltaOpSemantics:
    """Constructor helpers and read filtering for a :class:`~lazily.ipc.DeltaOp`."""

    __slots__ = ()

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        """Whether ``peer`` may read every node this op names.

        An edge op names two nodes and needs both; every other op names one.
        """
        dependent: Any = getattr(self, "dependent", None)
        if dependent is not None:
            dependency: Any = getattr(self, "dependency")  # noqa: B009
            return permissions.can_read(peer, dependent) and permissions.can_read(
                peer, dependency
            )
        node: Any = getattr(self, "node")  # noqa: B009
        return permissions.can_read(peer, node)

    # --- constructors mirroring the Rust helper surface ---

    @staticmethod
    def cell_set(node: NodeId, payload: IpcValue | ShmBlobRef | bytes) -> DeltaOp:
        from ._delta_wire_gen import DeltaOp_CellSet, IpcValue

        return DeltaOp_CellSet(node, IpcValue.of(payload))

    @staticmethod
    def slot_value(node: NodeId, payload: IpcValue | ShmBlobRef | bytes) -> DeltaOp:
        from ._delta_wire_gen import DeltaOp_SlotValue, IpcValue

        return DeltaOp_SlotValue(node, IpcValue.of(payload))

    @staticmethod
    def invalidate(node: NodeId) -> DeltaOp:
        from ._delta_wire_gen import DeltaOp_Invalidate

        return DeltaOp_Invalidate(node)

    @staticmethod
    def node_add(
        node: NodeId,
        type_tag: str,
        state: NodeState,
        key: NodeKey | None = None,
    ) -> DeltaOp:
        from ._delta_wire_gen import DeltaOp_NodeAdd

        return DeltaOp_NodeAdd(node, type_tag, state, key)

    @staticmethod
    def node_remove(node: NodeId) -> DeltaOp:
        from ._delta_wire_gen import DeltaOp_NodeRemove

        return DeltaOp_NodeRemove(node)

    @staticmethod
    def edge_add(dependent: NodeId, dependency: NodeId) -> DeltaOp:
        from ._delta_wire_gen import DeltaOp_EdgeAdd

        return DeltaOp_EdgeAdd(dependent, dependency)

    @staticmethod
    def edge_remove(dependent: NodeId, dependency: NodeId) -> DeltaOp:
        from ._delta_wire_gen import DeltaOp_EdgeRemove

        return DeltaOp_EdgeRemove(dependent, dependency)

    @staticmethod
    def queue_push(node: NodeId, payload: IpcValue | ShmBlobRef | bytes) -> DeltaOp:
        from ._delta_wire_gen import DeltaOp_QueuePush, IpcValue

        return DeltaOp_QueuePush(node, IpcValue.of(payload))

    @staticmethod
    def queue_pop(node: NodeId) -> DeltaOp:
        from ._delta_wire_gen import DeltaOp_QueuePop

        return DeltaOp_QueuePop(node)

    @staticmethod
    def queue_close(node: NodeId) -> DeltaOp:
        from ._delta_wire_gen import DeltaOp_QueueClose

        return DeltaOp_QueueClose(node)


class DeltaSemantics:
    """Construction, the receiver's epoch decision, and read filtering for a Delta."""

    __slots__ = ()

    if TYPE_CHECKING:
        base_epoch: int
        epoch: int
        ops: list[DeltaOp]

        def __init__(self, base_epoch: int, epoch: int, ops: list[DeltaOp]) -> None: ...

    @classmethod
    def next(cls, base_epoch: int, ops: list[DeltaOp]) -> Delta:
        """Strictly sequential delta with ``epoch == base_epoch + 1``."""
        from ._delta_wire_gen import Delta

        return Delta(base_epoch=base_epoch, epoch=base_epoch + 1, ops=list(ops))

    @classmethod
    def new(cls, base_epoch: int, epoch: int, ops: list[DeltaOp]) -> Delta:
        from ._delta_wire_gen import Delta

        return Delta(base_epoch=base_epoch, epoch=epoch, ops=list(ops))

    def is_next_after(self, last_epoch: int) -> bool:
        """Whether this delta is exactly the next delta after ``last_epoch``."""
        return self.base_epoch == last_epoch and self.epoch == self.base_epoch + 1

    def apply_status(self, last_epoch: int) -> DeltaApplyStatus:
        """The receiver action for this delta given its current ``last_epoch``."""
        from .ipc import DeltaApplyStatus

        if self.is_next_after(last_epoch):
            return DeltaApplyStatus.apply()
        return DeltaApplyStatus.resync_required(
            last_epoch=last_epoch, base_epoch=self.base_epoch, epoch=self.epoch
        )

    def filter_readable(self, permissions: PeerPermissions, peer: PeerId) -> Delta:
        """Peer-specific delta that omits non-readable operations entirely."""
        from ._delta_wire_gen import Delta

        ops = [op for op in self.ops if op._target_readable(permissions, peer)]
        return Delta(base_epoch=self.base_epoch, epoch=self.epoch, ops=ops)

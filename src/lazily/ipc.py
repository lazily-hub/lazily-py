"""lazily IPC wire protocol — Python binding.

Transport-agnostic Snapshot / Delta state image for ``lazily-ipc``, matching the
normative wire format defined by ``lazily-spec`` (``protocol.md`` + the canonical
``conformance/`` fixtures).

The JSON representation produced here is byte-compatible with the Rust reference
(`lazily-rs`) and the Zig binding: enums are **externally tagged**
(``{"Snapshot": {...}}``, ``{"Payload": [..]}``), wire-stable identifiers
(:data:`NodeId` / :data:`PeerId`) are bare integers, and serialized value bytes
are JSON arrays of ``u8`` rather than base64.

This module deliberately does not know whether messages travel over a unix
socket, pipe, WebSocket, WebRTC data channel, or shared-memory ring buffer. It
defines the stable serializable state plane and the permission-filtered
construction helpers that any transport can carry.
"""

from __future__ import annotations


__all__ = [
    "Delta",
    "DeltaApplyStatus",
    "DeltaApplyStatusKind",
    "DeltaOp",
    "DeltaOp_CellSet",
    "DeltaOp_EdgeAdd",
    "DeltaOp_EdgeRemove",
    "DeltaOp_Invalidate",
    "DeltaOp_NodeAdd",
    "DeltaOp_NodeRemove",
    "DeltaOp_SlotValue",
    "EdgeSnapshot",
    "IpcMessage",
    "IpcValue",
    "IpcValue_Inline",
    "IpcValue_SharedBlob",
    "NodeId",
    "NodeSnapshot",
    "NodeState",
    "NodeState_Opaque",
    "NodeState_Payload",
    "NodeState_SharedBlob",
    "OpKind",
    "PeerId",
    "PeerPermissions",
    "PermissionDenied",
    "RemoteOp",
    "ShmBlobRef",
    "Snapshot",
]

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Iterable


# ---------------------------------------------------------------------------
# Wire-stable identifiers
# ---------------------------------------------------------------------------

#: Wire-stable identifier for a reactive node (cell or slot). Serialized as a
#: bare JSON number. JavaScript/TypeScript peers must keep values at or below
#: ``Number.MAX_SAFE_INTEGER`` (2**53).
NodeId = int

#: Identifies a remote peer participating in a distributed session.
PeerId = int


def _bytes_to_wire(data: bytes) -> list[int]:
    return list(data)


def _bytes_from_wire(value: Any) -> bytes:
    return bytes(value)


# ---------------------------------------------------------------------------
# Shared-memory blob descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShmBlobRef:
    """Descriptor for a payload stored in a shared-memory blob arena."""

    offset: int
    len: int
    generation: int
    epoch: int
    checksum: int

    def to_wire(self) -> dict[str, int]:
        return {
            "offset": self.offset,
            "len": self.len,
            "generation": self.generation,
            "epoch": self.epoch,
            "checksum": self.checksum,
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> ShmBlobRef:
        return cls(
            offset=d["offset"],
            len=d["len"],
            generation=d["generation"],
            epoch=d["epoch"],
            checksum=d["checksum"],
        )


# ---------------------------------------------------------------------------
# NodeState (externally-tagged enum: Payload | SharedBlob | Opaque)
# ---------------------------------------------------------------------------


class NodeState:
    """Serializable state for one allowlisted node in a Snapshot or ``NodeAdd``.

    A tagged union with three variants:

    - :class:`NodeState_Payload` — concrete serialized value bytes
    - :class:`NodeState_SharedBlob` — descriptor into a shared-memory arena
    - :class:`NodeState_Opaque` — a known node whose value cannot be serialized
    """

    def to_wire(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    @staticmethod
    def from_wire(value: Any) -> NodeState:
        # Unit variant `Opaque` serializes as a bare string.
        if isinstance(value, str):
            if value == "Opaque":
                return NodeState_Opaque()
            raise ValueError(f"unknown NodeState unit variant: {value!r}")
        if isinstance(value, dict) and len(value) == 1:
            tag, body = next(iter(value.items()))
            if tag == "Payload":
                return NodeState_Payload(_bytes_from_wire(body))
            if tag == "SharedBlob":
                return NodeState_SharedBlob(ShmBlobRef.from_wire(body))
            if tag == "Opaque":
                return NodeState_Opaque()
            raise ValueError(f"unknown NodeState variant: {tag!r}")
        raise ValueError(f"malformed NodeState wire value: {value!r}")


@dataclass(frozen=True)
class NodeState_Payload(NodeState):
    """Concrete serialized value bytes."""

    data: bytes

    def to_wire(self) -> dict[str, list[int]]:
        return {"Payload": _bytes_to_wire(self.data)}


@dataclass(frozen=True)
class NodeState_SharedBlob(NodeState):
    """Descriptor for a value stored in a shared-memory blob arena."""

    blob: ShmBlobRef

    def to_wire(self) -> dict[str, dict[str, int]]:
        return {"SharedBlob": self.blob.to_wire()}


@dataclass(frozen=True)
class NodeState_Opaque(NodeState):
    """A known node whose value cannot be serialized."""

    def to_wire(self) -> str:
        return "Opaque"


# ---------------------------------------------------------------------------
# IpcValue (externally-tagged enum: Inline | SharedBlob)
# ---------------------------------------------------------------------------


class IpcValue:
    """A delta value carried inline or by shared-memory blob reference."""

    def to_wire(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    @staticmethod
    def from_wire(value: Any) -> IpcValue:
        if isinstance(value, dict) and len(value) == 1:
            tag, body = next(iter(value.items()))
            if tag == "Inline":
                return IpcValue_Inline(_bytes_from_wire(body))
            if tag == "SharedBlob":
                return IpcValue_SharedBlob(ShmBlobRef.from_wire(body))
            raise ValueError(f"unknown IpcValue variant: {tag!r}")
        raise ValueError(f"malformed IpcValue wire value: {value!r}")

    @staticmethod
    def of(value: IpcValue | ShmBlobRef | bytes | bytearray) -> IpcValue:
        """Coerce bytes / a blob ref into an :class:`IpcValue`."""
        if isinstance(value, IpcValue):
            return value
        if isinstance(value, ShmBlobRef):
            return IpcValue_SharedBlob(value)
        if isinstance(value, (bytes, bytearray)):
            return IpcValue_Inline(bytes(value))
        raise TypeError(f"cannot coerce {type(value).__name__} into IpcValue")


@dataclass(frozen=True)
class IpcValue_Inline(IpcValue):
    """Inline serialized bytes."""

    data: bytes

    def to_wire(self) -> dict[str, list[int]]:
        return {"Inline": _bytes_to_wire(self.data)}


@dataclass(frozen=True)
class IpcValue_SharedBlob(IpcValue):
    """Descriptor for bytes stored in a shared-memory blob arena."""

    blob: ShmBlobRef

    def to_wire(self) -> dict[str, dict[str, int]]:
        return {"SharedBlob": self.blob.to_wire()}


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeSnapshot:
    """Full state for one node in a snapshot."""

    node: NodeId
    type_tag: str
    state: NodeState

    @classmethod
    def payload(cls, node: NodeId, type_tag: str, data: bytes) -> NodeSnapshot:
        """A visible node carrying serialized value bytes."""
        return cls(node, type_tag, NodeState_Payload(bytes(data)))

    @classmethod
    def opaque(cls, node: NodeId, type_tag: str) -> NodeSnapshot:
        """A visible node whose value cannot be serialized."""
        return cls(node, type_tag, NodeState_Opaque())

    @classmethod
    def shared_blob(
        cls, node: NodeId, type_tag: str, blob: ShmBlobRef
    ) -> NodeSnapshot:
        """A visible node whose value lives in a shared-memory blob arena."""
        return cls(node, type_tag, NodeState_SharedBlob(blob))

    def to_wire(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "type_tag": self.type_tag,
            "state": self.state.to_wire(),
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> NodeSnapshot:
        return cls(
            node=d["node"],
            type_tag=d["type_tag"],
            state=NodeState.from_wire(d["state"]),
        )


@dataclass(frozen=True)
class EdgeSnapshot:
    """Directed dependency edge (``dependent`` → ``dependency``)."""

    dependent: NodeId
    dependency: NodeId

    def to_wire(self) -> dict[str, NodeId]:
        return {"dependent": self.dependent, "dependency": self.dependency}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> EdgeSnapshot:
        return cls(dependent=d["dependent"], dependency=d["dependency"])

    def _is_readable_by(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        return permissions.can_read(peer, self.dependent) and permissions.can_read(
            peer, self.dependency
        )


@dataclass(frozen=True)
class Snapshot:
    """Full graph image sent on connect or resync."""

    epoch: int
    nodes: list[NodeSnapshot] = field(default_factory=list)
    edges: list[EdgeSnapshot] = field(default_factory=list)
    roots: list[NodeId] = field(default_factory=list)

    def to_wire(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "nodes": [n.to_wire() for n in self.nodes],
            "edges": [e.to_wire() for e in self.edges],
            "roots": list(self.roots),
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> Snapshot:
        return cls(
            epoch=d["epoch"],
            nodes=[NodeSnapshot.from_wire(n) for n in d.get("nodes", [])],
            edges=[EdgeSnapshot.from_wire(e) for e in d.get("edges", [])],
            roots=list(d.get("roots", [])),
        )

    def filter_readable(
        self, permissions: PeerPermissions, peer: PeerId
    ) -> Snapshot:
        """Peer-specific snapshot that **omits** non-readable nodes entirely.

        Edges are retained only when both endpoints are readable; roots preserve
        their input order after filtering. Non-readable nodes are dropped, not
        redacted in place, so a peer cannot infer their existence.
        """
        nodes = [n for n in self.nodes if permissions.can_read(peer, n.node)]
        edges = [e for e in self.edges if e._is_readable_by(permissions, peer)]
        roots = permissions.filter_readable(peer, self.roots)
        return Snapshot(epoch=self.epoch, nodes=nodes, edges=edges, roots=roots)


# ---------------------------------------------------------------------------
# Delta ops (externally-tagged enum, 7 variants)
# ---------------------------------------------------------------------------


class DeltaOp:
    """One incremental graph mutation in a :class:`Delta`."""

    def to_wire(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        raise NotImplementedError

    # --- constructors mirroring the Rust helper surface ---

    @staticmethod
    def cell_set(node: NodeId, payload: IpcValue | ShmBlobRef | bytes) -> DeltaOp:
        return DeltaOp_CellSet(node, IpcValue.of(payload))

    @staticmethod
    def slot_value(node: NodeId, payload: IpcValue | ShmBlobRef | bytes) -> DeltaOp:
        return DeltaOp_SlotValue(node, IpcValue.of(payload))

    @staticmethod
    def invalidate(node: NodeId) -> DeltaOp:
        return DeltaOp_Invalidate(node)

    @staticmethod
    def node_add(node: NodeId, type_tag: str, state: NodeState) -> DeltaOp:
        return DeltaOp_NodeAdd(node, type_tag, state)

    @staticmethod
    def node_remove(node: NodeId) -> DeltaOp:
        return DeltaOp_NodeRemove(node)

    @staticmethod
    def edge_add(dependent: NodeId, dependency: NodeId) -> DeltaOp:
        return DeltaOp_EdgeAdd(dependent, dependency)

    @staticmethod
    def edge_remove(dependent: NodeId, dependency: NodeId) -> DeltaOp:
        return DeltaOp_EdgeRemove(dependent, dependency)

    @staticmethod
    def from_wire(value: Any) -> DeltaOp:
        if not (isinstance(value, dict) and len(value) == 1):
            raise ValueError(f"malformed DeltaOp wire value: {value!r}")
        tag, body = next(iter(value.items()))
        if tag == "CellSet":
            return DeltaOp_CellSet(body["node"], IpcValue.from_wire(body["payload"]))
        if tag == "SlotValue":
            return DeltaOp_SlotValue(body["node"], IpcValue.from_wire(body["payload"]))
        if tag == "Invalidate":
            return DeltaOp_Invalidate(body["node"])
        if tag == "NodeAdd":
            return DeltaOp_NodeAdd(
                body["node"], body["type_tag"], NodeState.from_wire(body["state"])
            )
        if tag == "NodeRemove":
            return DeltaOp_NodeRemove(body["node"])
        if tag == "EdgeAdd":
            return DeltaOp_EdgeAdd(body["dependent"], body["dependency"])
        if tag == "EdgeRemove":
            return DeltaOp_EdgeRemove(body["dependent"], body["dependency"])
        raise ValueError(f"unknown DeltaOp variant: {tag!r}")


@dataclass(frozen=True)
class DeltaOp_CellSet(DeltaOp):
    """A source cell was changed to ``payload`` (PartialEq-guarded)."""

    node: NodeId
    payload: IpcValue

    def to_wire(self) -> dict[str, Any]:
        return {"CellSet": {"node": self.node, "payload": self.payload.to_wire()}}

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        return permissions.can_read(peer, self.node)


@dataclass(frozen=True)
class DeltaOp_SlotValue(DeltaOp):
    """A lazily recomputed slot published a concrete value."""

    node: NodeId
    payload: IpcValue

    def to_wire(self) -> dict[str, Any]:
        return {"SlotValue": {"node": self.node, "payload": self.payload.to_wire()}}

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        return permissions.can_read(peer, self.node)


@dataclass(frozen=True)
class DeltaOp_Invalidate(DeltaOp):
    """A node was dirtied without publishing a concrete value (lazy)."""

    node: NodeId

    def to_wire(self) -> dict[str, Any]:
        return {"Invalidate": {"node": self.node}}

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        return permissions.can_read(peer, self.node)


@dataclass(frozen=True)
class DeltaOp_NodeAdd(DeltaOp):
    """A new node became visible (use ``NodeState_Opaque`` for an unset node)."""

    node: NodeId
    type_tag: str
    state: NodeState

    def to_wire(self) -> dict[str, Any]:
        return {
            "NodeAdd": {
                "node": self.node,
                "type_tag": self.type_tag,
                "state": self.state.to_wire(),
            }
        }

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        return permissions.can_read(peer, self.node)


@dataclass(frozen=True)
class DeltaOp_NodeRemove(DeltaOp):
    """A node was removed (free-list reuse: Remove then Add)."""

    node: NodeId

    def to_wire(self) -> dict[str, Any]:
        return {"NodeRemove": {"node": self.node}}

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        return permissions.can_read(peer, self.node)


@dataclass(frozen=True)
class DeltaOp_EdgeAdd(DeltaOp):
    """A dependency edge was added."""

    dependent: NodeId
    dependency: NodeId

    def to_wire(self) -> dict[str, Any]:
        return {
            "EdgeAdd": {"dependent": self.dependent, "dependency": self.dependency}
        }

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        return permissions.can_read(peer, self.dependent) and permissions.can_read(
            peer, self.dependency
        )


@dataclass(frozen=True)
class DeltaOp_EdgeRemove(DeltaOp):
    """A dependency edge was removed."""

    dependent: NodeId
    dependency: NodeId

    def to_wire(self) -> dict[str, Any]:
        return {
            "EdgeRemove": {"dependent": self.dependent, "dependency": self.dependency}
        }

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        return permissions.can_read(peer, self.dependent) and permissions.can_read(
            peer, self.dependency
        )


# ---------------------------------------------------------------------------
# Delta + receiver decision
# ---------------------------------------------------------------------------


class DeltaApplyStatusKind(Enum):
    APPLY = "apply"
    RESYNC_REQUIRED = "resync_required"


@dataclass(frozen=True)
class DeltaApplyStatus:
    """Receiver decision for an incoming :class:`Delta`."""

    kind: DeltaApplyStatusKind
    last_epoch: int | None = None
    base_epoch: int | None = None
    epoch: int | None = None

    @classmethod
    def apply(cls) -> DeltaApplyStatus:
        return cls(DeltaApplyStatusKind.APPLY)

    @classmethod
    def resync_required(
        cls, last_epoch: int, base_epoch: int, epoch: int
    ) -> DeltaApplyStatus:
        return cls(
            DeltaApplyStatusKind.RESYNC_REQUIRED,
            last_epoch=last_epoch,
            base_epoch=base_epoch,
            epoch=epoch,
        )

    @property
    def is_apply(self) -> bool:
        return self.kind is DeltaApplyStatusKind.APPLY

    @property
    def is_resync_required(self) -> bool:
        return self.kind is DeltaApplyStatusKind.RESYNC_REQUIRED


@dataclass(frozen=True)
class Delta:
    """Incremental change set emitted after one outermost batch flush."""

    base_epoch: int
    epoch: int
    ops: list[DeltaOp] = field(default_factory=list)

    @classmethod
    def next(cls, base_epoch: int, ops: list[DeltaOp]) -> Delta:
        """Strictly sequential delta with ``epoch == base_epoch + 1``."""
        return cls(base_epoch=base_epoch, epoch=base_epoch + 1, ops=list(ops))

    @classmethod
    def new(cls, base_epoch: int, epoch: int, ops: list[DeltaOp]) -> Delta:
        return cls(base_epoch=base_epoch, epoch=epoch, ops=list(ops))

    def is_next_after(self, last_epoch: int) -> bool:
        """Whether this delta is exactly the next delta after ``last_epoch``."""
        return self.base_epoch == last_epoch and self.epoch == self.base_epoch + 1

    def apply_status(self, last_epoch: int) -> DeltaApplyStatus:
        """The receiver action for this delta given its current ``last_epoch``."""
        if self.is_next_after(last_epoch):
            return DeltaApplyStatus.apply()
        return DeltaApplyStatus.resync_required(
            last_epoch=last_epoch, base_epoch=self.base_epoch, epoch=self.epoch
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "base_epoch": self.base_epoch,
            "epoch": self.epoch,
            "ops": [op.to_wire() for op in self.ops],
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> Delta:
        return cls(
            base_epoch=d["base_epoch"],
            epoch=d["epoch"],
            ops=[DeltaOp.from_wire(op) for op in d.get("ops", [])],
        )

    def filter_readable(self, permissions: PeerPermissions, peer: PeerId) -> Delta:
        """Peer-specific delta that omits non-readable operations entirely."""
        ops = [op for op in self.ops if op._target_readable(permissions, peer)]
        return Delta(base_epoch=self.base_epoch, epoch=self.epoch, ops=ops)


# ---------------------------------------------------------------------------
# IpcMessage (externally-tagged enum: Snapshot | Delta)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IpcMessage:
    """Tagged IPC protocol message — a :class:`Snapshot` or a :class:`Delta`."""

    snapshot: Snapshot | None = None
    delta: Delta | None = None

    @classmethod
    def of_snapshot(cls, snapshot: Snapshot) -> IpcMessage:
        return cls(snapshot=snapshot)

    @classmethod
    def of_delta(cls, delta: Delta) -> IpcMessage:
        return cls(delta=delta)

    @property
    def is_snapshot(self) -> bool:
        return self.snapshot is not None

    @property
    def is_delta(self) -> bool:
        return self.delta is not None

    def to_wire(self) -> dict[str, Any]:
        if self.snapshot is not None:
            return {"Snapshot": self.snapshot.to_wire()}
        if self.delta is not None:
            return {"Delta": self.delta.to_wire()}
        raise ValueError("IpcMessage carries neither a Snapshot nor a Delta")

    @classmethod
    def from_wire(cls, value: Any) -> IpcMessage:
        if not (isinstance(value, dict) and len(value) == 1):
            raise ValueError(f"malformed IpcMessage wire value: {value!r}")
        tag, body = next(iter(value.items()))
        if tag == "Snapshot":
            return cls(snapshot=Snapshot.from_wire(body))
        if tag == "Delta":
            return cls(delta=Delta.from_wire(body))
        raise ValueError(f"unknown IpcMessage variant: {tag!r}")

    def encode_json(self) -> bytes:
        """Serialize to transport-agnostic JSON bytes."""
        return json.dumps(self.to_wire(), separators=(",", ":")).encode("utf-8")

    @classmethod
    def decode_json(cls, data: bytes | str) -> IpcMessage:
        """Parse JSON bytes (or str) produced by any lazily binding."""
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8")
        return cls.from_wire(json.loads(data))


# ---------------------------------------------------------------------------
# Permission boundary (RemoteOp allowlist)
# ---------------------------------------------------------------------------


class OpKind(Enum):
    """The category of access a :class:`RemoteOp` requests.

    The three kinds are gated **independently**: a read grant never implies write
    or effect-trigger.
    """

    READ = "read"
    WRITE = "write"
    TRIGGER_EFFECT = "trigger_effect"


@dataclass(frozen=True)
class RemoteOp:
    """A single operation a remote peer may request against the shared graph."""

    kind: OpKind
    node: NodeId

    @classmethod
    def read(cls, node: NodeId) -> RemoteOp:
        return cls(OpKind.READ, node)

    @classmethod
    def write(cls, node: NodeId) -> RemoteOp:
        return cls(OpKind.WRITE, node)

    @classmethod
    def trigger_effect(cls, node: NodeId) -> RemoteOp:
        return cls(OpKind.TRIGGER_EFFECT, node)


@dataclass(frozen=True)
class PermissionDenied(Exception):
    """Raised/returned when a peer requests an operation outside its allowlist."""

    peer: PeerId
    op: RemoteOp

    def __str__(self) -> str:
        return f"peer {self.peer} denied {self.op.kind.value} on node {self.op.node}"


class PeerPermissions:
    """Default-deny per-peer allowlist gating reads, writes, and effect triggers.

    Only nodes on a peer's read allowlist are serialized into a snapshot or
    delta; non-allowlisted nodes are omitted entirely.
    """

    __slots__ = ("_peers",)

    def __init__(self) -> None:
        # peer -> {OpKind -> set[NodeId]}
        self._peers: dict[PeerId, dict[OpKind, set[NodeId]]] = {}

    def allow(self, peer: PeerId, op: RemoteOp) -> bool:
        """Grant ``peer`` permission to perform ``op``.

        Returns ``True`` if newly added, ``False`` if already held.
        """
        nodes = self._peers.setdefault(peer, {}).setdefault(op.kind, set())
        if op.node in nodes:
            return False
        nodes.add(op.node)
        return True

    def allow_many(
        self, peer: PeerId, kind: OpKind, nodes: Iterable[NodeId]
    ) -> None:
        """Grant ``peer`` ``kind`` access over many nodes at once."""
        target = self._peers.setdefault(peer, {}).setdefault(kind, set())
        target.update(nodes)

    def revoke(self, peer: PeerId, op: RemoteOp) -> bool:
        """Revoke ``op`` from ``peer``. Returns ``True`` if it was present."""
        peer_perms = self._peers.get(peer)
        if peer_perms is None:
            return False
        nodes = peer_perms.get(op.kind)
        if nodes is None or op.node not in nodes:
            return False
        nodes.discard(op.node)
        self._prune(peer)
        return True

    def revoke_peer(self, peer: PeerId) -> bool:
        """Remove every permission held by ``peer`` (e.g. on disconnect)."""
        return self._peers.pop(peer, None) is not None

    def is_allowed(self, peer: PeerId, op: RemoteOp) -> bool:
        """Whether ``peer`` may perform ``op``. Default-deny."""
        peer_perms = self._peers.get(peer)
        if peer_perms is None:
            return False
        return op.node in peer_perms.get(op.kind, ())

    def check(self, peer: PeerId, op: RemoteOp) -> None:
        """Fail-closed permission check.

        Raises :class:`PermissionDenied` when ``peer`` may not perform ``op``.
        """
        if not self.is_allowed(peer, op):
            raise PermissionDenied(peer, op)

    def can_read(self, peer: PeerId, node: NodeId) -> bool:
        return self.is_allowed(peer, RemoteOp.read(node))

    def filter_readable(
        self, peer: PeerId, nodes: Iterable[NodeId]
    ) -> list[NodeId]:
        """Retain only the nodes ``peer`` may read, preserving input order."""
        peer_perms = self._peers.get(peer)
        if peer_perms is None:
            return []
        readable = peer_perms.get(OpKind.READ, set())
        return [node for node in nodes if node in readable]

    def peer_count(self) -> int:
        """Number of peers with at least one permission."""
        return len(self._peers)

    def _prune(self, peer: PeerId) -> None:
        peer_perms = self._peers.get(peer)
        if peer_perms is None:
            return
        for kind in [k for k, v in peer_perms.items() if not v]:
            del peer_perms[kind]
        if not peer_perms:
            del self._peers[peer]

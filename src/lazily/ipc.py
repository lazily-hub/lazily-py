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
    "NODE_KEY_MAX_LEN",
    "NODE_KEY_MAX_SEGMENTS",
    "PROTOCOL_ID",
    "PROTOCOL_MAJOR_VERSION",
    "SHM_BLOB_HEADER_LEN",
    "BlobBackendKind",
    "CapabilityHandshake",
    "CapabilityNegotiationResult",
    "CausalReceipt",
    "CausalReceipts",
    "CrdtOp",
    "CrdtSync",
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
    "DeltaOp_QueueClose",
    "DeltaOp_QueuePop",
    "DeltaOp_QueuePush",
    "DeltaOp_SlotValue",
    "EdgeSnapshot",
    "IpcMessage",
    "IpcValue",
    "IpcValue_Inline",
    "IpcValue_SharedBlob",
    "NodeId",
    "NodeKey",
    "NodeKeyError",
    "NodeSnapshot",
    "NodeState",
    "NodeState_Opaque",
    "NodeState_Payload",
    "NodeState_SharedBlob",
    "OpKind",
    "OutboxAck",
    "PeerId",
    "PeerPermissions",
    "PermissionDenied",
    "ReceiptApplyResult",
    "ReceiptOutcome",
    "ReceiptProjection",
    "RemoteOp",
    "ResyncRequest",
    "ShmBlobArena",
    "ShmBlobArenaError",
    "ShmBlobCapacityTooSmall",
    "ShmBlobChecksumMismatch",
    "ShmBlobDescriptorMismatch",
    "ShmBlobDescriptorOutOfBounds",
    "ShmBlobGenerationOverflow",
    "ShmBlobRef",
    "ShmBlobTooLarge",
    "Snapshot",
    "WireStamp",
]

import json
import struct
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any

from ._delta_wire_gen import (
    Delta,
    DeltaOp,
    DeltaOp_CellSet,
    DeltaOp_EdgeAdd,
    DeltaOp_EdgeRemove,
    DeltaOp_Invalidate,
    DeltaOp_NodeAdd,
    DeltaOp_NodeRemove,
    DeltaOp_QueueClose,
    DeltaOp_QueuePop,
    DeltaOp_QueuePush,
    DeltaOp_SlotValue,
    IpcValue,
    IpcValue_Inline,
    IpcValue_SharedBlob,
    NodeId,
    NodeState,
    NodeState_Opaque,
    NodeState_Payload,
    NodeState_SharedBlob,
)
from ._receipts_wire_gen import CausalReceipt, CausalReceipts, ReceiptOutcome
from ._wire_scalars import (
    NODE_KEY_MAX_LEN,
    NODE_KEY_MAX_SEGMENTS,
    BlobBackendKind,
    NodeKey,
    NodeKeyError,
    ShmBlobRef,
)
from .msgpack_codec import msgpack_pack, msgpack_unpack


if TYPE_CHECKING:
    from collections.abc import Iterable


# ---------------------------------------------------------------------------
# Wire-stable identifiers
# ---------------------------------------------------------------------------

# ``NodeId`` (a bare JSON number, u64) is declared by the generated wire types.

#: Identifies a remote peer participating in a distributed session.
PeerId = int


# ---------------------------------------------------------------------------
# Shared-memory blob arena (parity with lazily-rs / lazily-zig)
# ---------------------------------------------------------------------------

#: Bytes reserved before every shared-memory blob payload. Matches the 40-byte
#: header written by ``lazily-rs`` ``ShmBlobArena`` and ``lazily-zig``
#: ``ShmBlobArena`` so descriptors interoperate across siblings.
SHM_BLOB_HEADER_LEN = 40

_SHM_BLOB_MAGIC = 0x4C5A5348  # "LZSH"
_SHM_BLOB_VERSION = 1
_FNV_OFFSET_BASIS = 0xCBF29CE484222325
_FNV_PRIME = 0x00000100000001B3
_U64_MASK = (1 << 64) - 1
_SHM_BLOB_MIN_CAPACITY = SHM_BLOB_HEADER_LEN + 1


class ShmBlobArenaError(Exception):
    """Base class for errors raised by :class:`ShmBlobArena`.

    Each failure mode has a concrete subclass; catch
    :class:`ShmBlobArenaError` to handle any arena failure. The variants mirror
    the ``lazily-rs`` ``ShmBlobArenaError`` enum and the ``lazily-zig``
    ``ShmBlobArenaError`` error set.
    """


class ShmBlobCapacityTooSmall(ShmBlobArenaError):
    """The backing buffer cannot hold one header plus one payload byte."""

    def __init__(self, capacity: int, min_capacity: int) -> None:
        self.capacity = capacity
        self.min_capacity = min_capacity
        super().__init__(
            f"SHM blob arena capacity {capacity} is smaller than minimum {min_capacity}"
        )


class ShmBlobTooLarge(ShmBlobArenaError):
    """Payload is larger than the largest single blob this arena can hold."""

    def __init__(self, length: int, max_length: int) -> None:
        self.length = length
        self.max_length = max_length
        super().__init__(f"SHM blob length {length} exceeds maximum {max_length}")


class ShmBlobDescriptorOutOfBounds(ShmBlobArenaError):
    """Descriptor points outside this arena."""

    def __init__(self, offset: int, length: int, capacity: int) -> None:
        self.offset = offset
        self.length = length
        self.capacity = capacity
        super().__init__(
            f"SHM blob descriptor offset={offset} len={length} exceeds arena "
            f"capacity {capacity}"
        )


class ShmBlobDescriptorMismatch(ShmBlobArenaError):
    """Descriptor/header metadata did not match (e.g. stale after wraparound)."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"SHM blob descriptor mismatch for {field}")


class ShmBlobChecksumMismatch(ShmBlobArenaError):
    """Payload checksum did not match the descriptor/header checksum."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"SHM blob checksum mismatch: expected {expected:#x}, got {actual:#x}"
        )


class ShmBlobGenerationOverflow(ShmBlobArenaError):
    """The arena generation counter overflowed ``u64``."""

    def __init__(self) -> None:
        super().__init__("SHM blob generation counter overflowed")


def _fnv1a_64(payload: bytes | bytearray | memoryview) -> int:
    """FNV-1a (64-bit) non-cryptographic checksum, matching lazily-rs/zig."""
    hash_value = _FNV_OFFSET_BASIS
    for byte in payload:
        hash_value = ((hash_value ^ byte) * _FNV_PRIME) & _U64_MASK
    return hash_value


def _write_blob_header(buffer: bytearray, offset: int, descriptor: ShmBlobRef) -> None:
    struct.pack_into(
        "<IHHQQQQ",
        buffer,
        offset,
        _SHM_BLOB_MAGIC,
        _SHM_BLOB_VERSION,
        SHM_BLOB_HEADER_LEN,
        descriptor.generation,
        descriptor.epoch,
        descriptor.len,
        descriptor.checksum,
    )


def _read_blob_header(buffer: bytearray, offset: int) -> ShmBlobRef:
    magic, version, header_len, generation, epoch, length, checksum = (
        struct.unpack_from("<IHHQQQQ", buffer, offset)
    )
    if magic != _SHM_BLOB_MAGIC:
        raise ShmBlobDescriptorMismatch("magic")
    if version != _SHM_BLOB_VERSION:
        raise ShmBlobDescriptorMismatch("version")
    if header_len != SHM_BLOB_HEADER_LEN:
        raise ShmBlobDescriptorMismatch("header_len")
    return ShmBlobRef(
        offset=offset,
        generation=generation,
        epoch=epoch,
        len=length,
        checksum=checksum,
    )


def _blob_mismatch_field(actual: ShmBlobRef, expected: ShmBlobRef) -> str:
    if actual.generation != expected.generation:
        return "generation"
    if actual.epoch != expected.epoch:
        return "epoch"
    if actual.len != expected.len:
        return "len"
    if actual.checksum != expected.checksum:
        return "checksum"
    return "offset"


class ShmBlobArena:
    """Fixed-size blob arena suitable for a shared-memory transport.

    Ports ``lazily-rs`` ``ShmBlobArena<B>`` (``ipc.rs``) and mirrors
    ``lazily-zig`` ``ShmBlobArena`` (``ipc.zig``): a flat byte buffer plus an
    append-only write cursor and fixed-size :class:`ShmBlobRef` descriptors.

    The arena writes a 40-byte header before each payload. Readers validate the
    header, generation, epoch, payload length, and FNV-1a checksum before
    returning a view. Writes are append-only with wraparound; each write bumps a
    generation counter so a stale descriptor that lands on an overwritten region
    fails validation instead of returning torn data.

    Backing is a :class:`bytearray` (no native extension — preserves the
    pure-Python install story). :meth:`from_buffer` wraps externally-owned
    storage; the caller remains responsible for that buffer's lifetime. True
    cross-process OS shared memory (``/dev/shm``, ``mmap``) is a follow-on that
    swaps the backing buffer.
    """

    __slots__ = ("_buffer", "_next_generation", "_write_offset")

    def __init__(self, buffer: bytearray) -> None:
        capacity = len(buffer)
        if capacity < _SHM_BLOB_MIN_CAPACITY:
            raise ShmBlobCapacityTooSmall(capacity, _SHM_BLOB_MIN_CAPACITY)
        self._buffer = buffer
        self._write_offset = 0
        self._next_generation = 1

    @classmethod
    def with_capacity(cls, capacity: int) -> ShmBlobArena:
        """Create a ``bytearray``-backed arena of ``capacity`` bytes."""
        if capacity < _SHM_BLOB_MIN_CAPACITY:
            raise ShmBlobCapacityTooSmall(capacity, _SHM_BLOB_MIN_CAPACITY)
        return cls(bytearray(capacity))

    @classmethod
    def from_buffer(cls, buffer: bytearray) -> ShmBlobArena:
        """Wrap an existing ``bytearray`` (externally-owned, not zeroed).

        The caller keeps ownership of ``buffer``; the arena reads and writes it
        in place. Use this to back an arena with OS shared memory once a
        transport swaps the buffer in.
        """
        return cls(buffer)

    @property
    def capacity(self) -> int:
        """Total arena capacity in bytes."""
        return len(self._buffer)

    @property
    def max_blob_len(self) -> int:
        """Maximum payload length this arena can hold in one blob."""
        return self.capacity - SHM_BLOB_HEADER_LEN

    @property
    def write_offset(self) -> int:
        """Current write cursor offset."""
        return self._write_offset

    def buffer(self) -> memoryview:
        """Read-only view of the backing bytes (for transport setup/inspection)."""
        return memoryview(self._buffer).toreadonly()

    def write_blob(
        self, epoch: int, payload: bytes | bytearray | memoryview
    ) -> ShmBlobRef:
        """Write a payload and return a descriptor suitable for an IPC message."""
        capacity = self.capacity
        length = len(payload)
        max_len = self.max_blob_len
        if length > max_len:
            raise ShmBlobTooLarge(length, max_len)

        total_len = SHM_BLOB_HEADER_LEN + length
        if self._write_offset + total_len > capacity:
            self._write_offset = 0

        generation = self._next_generation
        if generation == _U64_MASK:
            raise ShmBlobGenerationOverflow()
        self._next_generation = generation + 1

        offset = self._write_offset
        checksum = _fnv1a_64(payload)
        descriptor = ShmBlobRef(
            offset=offset,
            len=length,
            generation=generation,
            epoch=epoch,
            checksum=checksum,
        )

        payload_offset = offset + SHM_BLOB_HEADER_LEN
        _write_blob_header(self._buffer, offset, descriptor)
        self._buffer[payload_offset : payload_offset + length] = payload

        self._write_offset += total_len
        if self._write_offset == capacity:
            self._write_offset = 0

        return descriptor

    def read_blob(self, descriptor: ShmBlobRef) -> memoryview:
        """Read and validate a previously written blob; returns a zero-copy view."""
        capacity = self.capacity
        offset = descriptor.offset
        length = descriptor.len
        if offset < 0 or length < 0:
            raise ShmBlobDescriptorOutOfBounds(offset, length, capacity)
        total_len = SHM_BLOB_HEADER_LEN + length
        if offset > capacity or total_len > capacity or offset > capacity - total_len:
            raise ShmBlobDescriptorOutOfBounds(offset, length, capacity)

        header = _read_blob_header(self._buffer, offset)
        # The arena header does not store `backend`; align it to the descriptor
        # so a non-Shm descriptor validates against the backend-agnostic header.
        header = replace(header, backend=descriptor.backend)
        if header != descriptor:
            raise ShmBlobDescriptorMismatch(_blob_mismatch_field(header, descriptor))

        payload_offset = offset + SHM_BLOB_HEADER_LEN
        payload = memoryview(self._buffer)[payload_offset : payload_offset + length]
        actual = _fnv1a_64(payload)
        if actual != descriptor.checksum:
            raise ShmBlobChecksumMismatch(descriptor.checksum, actual)
        return payload.toreadonly()


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    """Full state for one node in a snapshot."""

    node: NodeId
    type_tag: str
    state: NodeState
    key: NodeKey | None = None

    @classmethod
    def payload(cls, node: NodeId, type_tag: str, data: bytes) -> NodeSnapshot:
        """A visible node carrying serialized value bytes."""
        return cls(node, type_tag, NodeState_Payload(bytes(data)))

    @classmethod
    def opaque(cls, node: NodeId, type_tag: str) -> NodeSnapshot:
        """A visible node whose value cannot be serialized."""
        return cls(node, type_tag, NodeState_Opaque())

    @classmethod
    def shared_blob(cls, node: NodeId, type_tag: str, blob: ShmBlobRef) -> NodeSnapshot:
        """A visible node whose value lives in a shared-memory blob arena."""
        return cls(node, type_tag, NodeState_SharedBlob(blob))

    def with_key(self, key: NodeKey) -> NodeSnapshot:
        """Return a copy carrying a wire-stable :class:`NodeKey` (builder style)."""
        return NodeSnapshot(self.node, self.type_tag, self.state, key)

    def to_wire(self) -> dict[str, Any]:
        # Self-describing codecs (JSON, MessagePack) omit a `None` key so
        # pre-`key` encoders and existing conformance fixtures round-trip
        # unchanged.
        wire: dict[str, Any] = {
            "node": self.node,
            "type_tag": self.type_tag,
            "state": self.state.to_wire(),
        }
        if self.key is not None:
            wire["key"] = self.key.to_wire()
        return wire

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> NodeSnapshot:
        return cls(
            node=d["node"],
            type_tag=d["type_tag"],
            state=NodeState.from_wire(d["state"]),
            # Omit-when-absent is an ENCODER rule (protocol.md § NodeKey,
            # ``#lzkeynullstrict``). A conforming peer may still send an explicit
            # ``key: null`` — a serde-based encoder that simply did not apply
            # ``skip_serializing_if`` does exactly that — so a decoder MUST read
            # both forms as absent. ``"key" in d`` was true for the null form and
            # sent ``None`` into ``NodeKey.from_wire``, which raised. Note
            # :meth:`CrdtOp.from_wire` one field over already reads it correctly,
            # because a ``CrdtOp`` ALWAYS writes ``key: null`` when unset.
            key=NodeKey.from_wire(d["key"]) if d.get("key") is not None else None,
        )


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
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

    def filter_readable(self, permissions: PeerPermissions, peer: PeerId) -> Snapshot:
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
# Delta + receiver decision
# ---------------------------------------------------------------------------


class DeltaApplyStatusKind(Enum):
    APPLY = "apply"
    RESYNC_REQUIRED = "resync_required"


@dataclass(frozen=True, slots=True)
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


# ---------------------------------------------------------------------------
# Distributed: CRDT cell plane (CrdtSync)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WireStamp:
    """Wire mirror of the runtime HLC stamp — a total order ``(wall, logical, peer)``.

    All plain integers so the wire format is codec-stable whether or not a peer
    compiles the CRDT runtime in. Round-trips across all codecs (JSON,
    MessagePack, Postcard).
    """

    wall_time: int
    logical: int
    peer: int

    def to_wire(self) -> dict[str, int]:
        return {
            "wall_time": self.wall_time,
            "logical": self.logical,
            "peer": self.peer,
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> WireStamp:
        return cls(
            wall_time=d["wall_time"],
            logical=d["logical"],
            peer=d["peer"],
        )


@dataclass(frozen=True, slots=True)
class CrdtOp:
    """One CRDT cell op on the wire (state-based / CvRDT).

    The converged register, sequence, or text ``state`` for ``node``, tagged
    with the :class:`WireStamp` that produced it and an optional wire-stable
    :class:`NodeKey` that survives NodeId churn. The receiver merges ``state``
    into its local replica; because every cell CRDT merge is commutative,
    associative, and idempotent, out-of-order, duplicated, or batched delivery
    all converge — so a :class:`CrdtOp` is safe to resend.
    """

    node: NodeId
    key: NodeKey | None
    stamp: WireStamp
    state: IpcValue

    @classmethod
    def new(
        cls, node: NodeId, stamp: WireStamp, state: IpcValue | ShmBlobRef | bytes
    ) -> CrdtOp:
        """Construct a keyless op (addressed only by ``node``)."""
        return cls(node, None, stamp, IpcValue.of(state))

    @classmethod
    def keyed(
        cls,
        node: NodeId,
        key: NodeKey,
        stamp: WireStamp,
        state: IpcValue | ShmBlobRef | bytes,
    ) -> CrdtOp:
        """Construct an op carrying a wire-stable :class:`NodeKey`."""
        return cls(node, key, stamp, IpcValue.of(state))

    def to_wire(self) -> dict[str, Any]:
        # Mirrors the lazily-rs derived serde struct: `key` is always present
        # (null when unset) so byte output matches the Rust reference. A decoder
        # also accepts an absent field.
        return {
            "node": self.node,
            "key": self.key.to_wire() if self.key is not None else None,
            "stamp": self.stamp.to_wire(),
            "state": self.state.to_wire(),
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CrdtOp:
        key = d.get("key")
        return cls(
            node=d["node"],
            key=NodeKey.from_wire(key) if key is not None else None,
            stamp=WireStamp.from_wire(d["stamp"]),
            state=IpcValue.from_wire(d["state"]),
        )


@dataclass(frozen=True, slots=True)
class CrdtSync:
    """A CRDT anti-entropy sync frame (the multi-writer plane).

    The sender advertises its per-peer **stamp frontier** (the highest
    :class:`WireStamp` it has observed from each peer) and ships a batch of
    :class:`CrdtOp` s. The frontier exchange is bounded, idempotent, and
    resumable; re-sending a frame the receiver already has is a no-op.
    """

    frontier: list[tuple[int, WireStamp]] = field(default_factory=list)
    ops: list[CrdtOp] = field(default_factory=list)

    @classmethod
    def new(cls, frontier: list[tuple[int, WireStamp]], ops: list[CrdtOp]) -> CrdtSync:
        """Construct a sync frame from a frontier advertisement and an op batch."""
        return cls(frontier=list(frontier), ops=list(ops))

    def to_wire(self) -> dict[str, Any]:
        return {
            "frontier": [[peer, stamp.to_wire()] for peer, stamp in self.frontier],
            "ops": [op.to_wire() for op in self.ops],
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CrdtSync:
        frontier = [
            (int(entry[0]), WireStamp.from_wire(entry[1]))
            for entry in d.get("frontier", [])
        ]
        ops = [CrdtOp.from_wire(op) for op in d.get("ops", [])]
        return cls(frontier=frontier, ops=ops)

    def filter_readable(self, permissions: PeerPermissions, peer: PeerId) -> CrdtSync:
        """Peer-specific frame that **omits** ops for non-readable nodes entirely.

        Omission, not redaction — mirroring :meth:`Delta.filter_readable`. The
        ``frontier`` advertisement is retained: it names peers and stamps, not
        node content, and the receiver needs the whole frontier to compute a
        sound causal-stability watermark.
        """
        ops = [op for op in self.ops if permissions.can_read(peer, op.node)]
        return CrdtSync(frontier=list(self.frontier), ops=ops)


# ---------------------------------------------------------------------------
# Reliable sync (#lzsync): reverse-channel control frames
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResyncRequest:
    """Reliable-sync reverse-channel control frame: request a covering
    :class:`Snapshot` on a detected gap (``#lzsync``, spec § ResyncCoordinator).

    Carries no node content, so it is permission-filter- and blob-spill-
    transparent. The requesting receiver's ``from_epoch`` is its ``last_epoch``;
    the sender replies with a ``Snapshot { epoch >= from_epoch }``.
    """

    from_epoch: int

    def to_wire(self) -> dict[str, int]:
        return {"from_epoch": self.from_epoch}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> ResyncRequest:
        return cls(from_epoch=d["from_epoch"])


@dataclass(frozen=True, slots=True)
class OutboxAck:
    """Reliable-sync reverse-channel control frame: prove receipt through
    ``through_epoch`` (``#lzsync``, spec § DurableOutbox).

    Advances the sender's outbox retention cursor and doubles as the reconnect
    resume cursor. Carries no node content.
    """

    through_epoch: int

    def to_wire(self) -> dict[str, int]:
        return {"through_epoch": self.through_epoch}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> OutboxAck:
        return cls(through_epoch=d["through_epoch"])


# ---------------------------------------------------------------------------
# IpcMessage (externally-tagged enum:
#   Snapshot | Delta | CrdtSync | ResyncRequest | OutboxAck)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IpcMessage:
    """Tagged IPC protocol message — a :class:`Snapshot`, :class:`Delta`,
    :class:`CrdtSync`, or a reliable-sync control frame (:class:`ResyncRequest`
    / :class:`OutboxAck`)."""

    snapshot: Snapshot | None = None
    delta: Delta | None = None
    crdt_sync: CrdtSync | None = None
    resync_request: ResyncRequest | None = None
    outbox_ack: OutboxAck | None = None

    @classmethod
    def of_snapshot(cls, snapshot: Snapshot) -> IpcMessage:
        return cls(snapshot=snapshot)

    @classmethod
    def of_delta(cls, delta: Delta) -> IpcMessage:
        return cls(delta=delta)

    @classmethod
    def of_crdt_sync(cls, crdt_sync: CrdtSync) -> IpcMessage:
        return cls(crdt_sync=crdt_sync)

    @classmethod
    def of_resync_request(cls, resync_request: ResyncRequest) -> IpcMessage:
        return cls(resync_request=resync_request)

    @classmethod
    def of_outbox_ack(cls, outbox_ack: OutboxAck) -> IpcMessage:
        return cls(outbox_ack=outbox_ack)

    @property
    def is_snapshot(self) -> bool:
        return self.snapshot is not None

    @property
    def is_delta(self) -> bool:
        return self.delta is not None

    @property
    def is_crdt_sync(self) -> bool:
        return self.crdt_sync is not None

    @property
    def is_resync_request(self) -> bool:
        return self.resync_request is not None

    @property
    def is_outbox_ack(self) -> bool:
        return self.outbox_ack is not None

    @property
    def is_control(self) -> bool:
        """Whether this is a reliable-sync reverse-channel control frame
        (:class:`ResyncRequest` / :class:`OutboxAck`) — no node content, so
        permission filtering and blob spilling are the identity on it."""
        return self.resync_request is not None or self.outbox_ack is not None

    def to_wire(self) -> dict[str, Any]:
        if self.snapshot is not None:
            return {"Snapshot": self.snapshot.to_wire()}
        if self.delta is not None:
            return {"Delta": self.delta.to_wire()}
        if self.crdt_sync is not None:
            return {"CrdtSync": self.crdt_sync.to_wire()}
        if self.resync_request is not None:
            return {"ResyncRequest": self.resync_request.to_wire()}
        if self.outbox_ack is not None:
            return {"OutboxAck": self.outbox_ack.to_wire()}
        raise ValueError(
            "IpcMessage carries no Snapshot, Delta, CrdtSync, "
            "ResyncRequest, nor OutboxAck"
        )

    @classmethod
    def from_wire(cls, value: Any) -> IpcMessage:
        if not (isinstance(value, dict) and len(value) == 1):
            raise ValueError(f"malformed IpcMessage wire value: {value!r}")
        tag, body = next(iter(value.items()))
        if tag == "Snapshot":
            return cls(snapshot=Snapshot.from_wire(body))
        if tag == "Delta":
            return cls(delta=Delta.from_wire(body))
        if tag == "CrdtSync":
            return cls(crdt_sync=CrdtSync.from_wire(body))
        if tag == "ResyncRequest":
            return cls(resync_request=ResyncRequest.from_wire(body))
        if tag == "OutboxAck":
            return cls(outbox_ack=OutboxAck.from_wire(body))
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

    def encode_msgpack(self) -> bytes:
        """Serialize to ``msgpack`` frame bytes — the cross-language binary
        default (protocol.md § Frame codecs).

        Deliberately serializes the very tree :meth:`to_wire` builds for the
        ``json`` reference codec rather than describing the frame a second
        time. The codec token names one wire — externally tagged envelope over
        named-field maps keyed by the ``json`` field names, with the same
        omit-when-absent rule for the optional ``NodeKey`` — and deriving it
        from ``to_wire()`` makes those properties identical to the reference
        codec by construction. Not byte-canonical: MessagePack map key order is
        encoder-defined, so conformance is ``decode(encode(m)) == m``.
        """
        return msgpack_pack(self.to_wire())

    @classmethod
    def decode_msgpack(cls, data: bytes | bytearray | memoryview) -> IpcMessage:
        """Parse ``msgpack`` frame bytes produced by any lazily binding."""
        return cls.from_wire(msgpack_unpack(data))


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


@dataclass(frozen=True, slots=True)
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

    def allow_many(self, peer: PeerId, kind: OpKind, nodes: Iterable[NodeId]) -> None:
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

    def filter_readable(self, peer: PeerId, nodes: Iterable[NodeId]) -> list[NodeId]:
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


# ---------------------------------------------------------------------------
# Capability negotiation (handshake)
# ---------------------------------------------------------------------------


#: The protocol identifier every ``lazily-ipc`` peer must advertise.
PROTOCOL_ID = "lazily-ipc"
#: The current protocol major version.
PROTOCOL_MAJOR_VERSION = 1


@dataclass(frozen=True, slots=True)
class CapabilityNegotiationResult:
    """Result of reconciling two capability handshakes.

    Compatible results retain the effective frame ceiling and fragmentation
    capability. Incompatible results name the canonical wire field that failed.
    """

    compatible: bool
    max_frame_size: int | None = None
    fragmentation_supported: bool | None = None
    field: str | None = None

    @classmethod
    def success(
        cls, max_frame_size: int, fragmentation_supported: bool
    ) -> CapabilityNegotiationResult:
        return cls(
            compatible=True,
            max_frame_size=max_frame_size,
            fragmentation_supported=fragmentation_supported,
        )

    @classmethod
    def failure(cls, field: str) -> CapabilityNegotiationResult:
        return cls(compatible=False, field=field)


@dataclass(frozen=True, slots=True)
class CapabilityHandshake:
    """Compatibility handshake exchanged before any graph state flows.

    Each non-local session starts with this frame. Serialized as a plain JSON
    object (it is a standalone frame, not an :class:`IpcMessage` variant).
    Peers that disagree on ``protocol_major_version``, ``codec``, or
    ``ordered_reliable`` fail closed before applying any :class:`Snapshot` or
    :class:`Delta`.

    ``fragmentation_supported`` and ``features`` default to off/empty and are
    omitted when absent only if explicitly cleared; the frame otherwise carries
    every field so a peer sees the full advertisement.
    """

    protocol_id: str
    protocol_major_version: int
    codec: str
    max_frame_size: int
    fragmentation_supported: bool = False
    ordered_reliable: bool = True
    peer_id: PeerId = 0
    session_id: str = ""
    features: list[str] = field(default_factory=list)

    @classmethod
    def new(cls, peer_id: PeerId, session_id: str) -> CapabilityHandshake:
        """Create a handshake with protocol defaults (JSON codec, 1 MiB frame,
        ordered-reliable, no features)."""
        return cls(
            protocol_id=PROTOCOL_ID,
            protocol_major_version=PROTOCOL_MAJOR_VERSION,
            codec="json",
            max_frame_size=1_048_576,
            fragmentation_supported=False,
            ordered_reliable=True,
            peer_id=peer_id,
            session_id=session_id,
            features=[],
        )

    def with_codec(self, codec: str) -> CapabilityHandshake:
        """Return a copy with the codec negotiation token set."""
        return replace(self, codec=codec)

    def with_max_frame_size(self, max_frame_size: int) -> CapabilityHandshake:
        """Return a copy with the max frame size set."""
        return replace(self, max_frame_size=max_frame_size)

    def with_features(self, features: Iterable[str]) -> CapabilityHandshake:
        """Return a copy with the features list set."""
        return replace(self, features=list(features))

    def with_fragmentation(self, supported: bool) -> CapabilityHandshake:
        """Return a copy with fragmentation support set."""
        return replace(self, fragmentation_supported=supported)

    def has_feature(self, feature: str) -> bool:
        """Whether this peer advertises ``feature``."""
        return feature in self.features

    def negotiate_with(self, other: CapabilityHandshake) -> CapabilityNegotiationResult:
        """Negotiate a session with ``other`` and retain its effective limits.

        Peers are compatible when both advertise :data:`PROTOCOL_ID`, both
        advertise :data:`PROTOCOL_MAJOR_VERSION`, their major versions and
        codecs agree, both require ordered reliable delivery, both frame
        ceilings are positive, and both name the same non-empty session.

        Frame size reconciles to the smaller receive ceiling. Fragmentation is
        available only when both peers advertise support. Feature negotiation
        remains caller-driven via :attr:`features` / :meth:`has_feature`.
        """
        if self.protocol_id != PROTOCOL_ID or other.protocol_id != PROTOCOL_ID:
            return CapabilityNegotiationResult.failure("protocol_id")
        if (
            self.protocol_major_version != PROTOCOL_MAJOR_VERSION
            or other.protocol_major_version != PROTOCOL_MAJOR_VERSION
            or self.protocol_major_version != other.protocol_major_version
        ):
            return CapabilityNegotiationResult.failure("protocol_major_version")
        if self.codec != other.codec:
            return CapabilityNegotiationResult.failure("codec")
        if not self.ordered_reliable or not other.ordered_reliable:
            return CapabilityNegotiationResult.failure("ordered_reliable")
        if self.max_frame_size <= 0 or other.max_frame_size <= 0:
            return CapabilityNegotiationResult.failure("max_frame_size")
        if (
            not self.session_id
            or not other.session_id
            or self.session_id != other.session_id
        ):
            return CapabilityNegotiationResult.failure("session_id")

        return CapabilityNegotiationResult.success(
            min(self.max_frame_size, other.max_frame_size),
            self.fragmentation_supported and other.fragmentation_supported,
        )

    def is_compatible_with(self, other: CapabilityHandshake) -> bool:
        """Compatibility-only wrapper around :meth:`negotiate_with`."""
        return self.negotiate_with(other).compatible

    def to_wire(self) -> dict[str, Any]:
        return {
            "protocol_id": self.protocol_id,
            "protocol_major_version": self.protocol_major_version,
            "codec": self.codec,
            "max_frame_size": self.max_frame_size,
            "fragmentation_supported": self.fragmentation_supported,
            "ordered_reliable": self.ordered_reliable,
            "peer_id": self.peer_id,
            "session_id": self.session_id,
            "features": list(self.features),
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CapabilityHandshake:
        """Decode a handshake frame.

        **Deliberate leniency on the three optional fields.** The identity
        fields (``protocol_id``, ``protocol_major_version``, ``codec``,
        ``max_frame_size``, ``peer_id``, ``session_id``) are required and a
        missing one raises ``KeyError``; the capability fields are optional
        because the handshake exists to let two builds of different ages agree,
        and a peer that predates a capability cannot name it. The defaults are
        each the conservative reading of silence:

        * ``fragmentation_supported`` defaults to **False** — a peer that never
          claimed fragmentation is assumed unable to reassemble, so this side
          keeps every frame under ``max_frame_size``.
        * ``ordered_reliable`` defaults to **True** — the base transport
          contract, the assumption every pre-capability peer was written under.
          A peer running an unordered transport must say so.
        * ``features`` defaults to **empty** — an unnamed feature is an ungated
          feature, and every feature-gated plane (e.g.
          ``lazily.command.COMMAND_PLANE_FEATURE``) refuses to run without its
          token.

        Unknown *extra* keys are ignored for the same reason: a newer peer may
        advertise capabilities this build has no arm for, and refusing the
        handshake over them would make every capability addition a breaking
        change. Pinned by ``tests/test_library_leniency.py``.
        """
        return cls(
            protocol_id=d["protocol_id"],
            protocol_major_version=d["protocol_major_version"],
            codec=d["codec"],
            max_frame_size=d["max_frame_size"],
            fragmentation_supported=d.get("fragmentation_supported", False),
            ordered_reliable=d.get("ordered_reliable", True),
            peer_id=d["peer_id"],
            session_id=d["session_id"],
            features=list(d.get("features", [])),
        )


# ---------------------------------------------------------------------------
# Causal receipts (generic outcome projection — NOT a transport ACK)
#
# ``ReceiptOutcome``, ``CausalReceipt`` and ``CausalReceipts`` are generated
# from lazily-spec ``schemas/receipts.json`` into ``_receipts_wire_gen`` and
# re-exported here; their non-wire behaviour lives in ``_receipt_semantics``.
# ---------------------------------------------------------------------------


class ReceiptApplyResult(Enum):
    """Result of applying one receipt to a :class:`ReceiptProjection`.

    Mirrors ``LazilyFormal.Receipt.ApplyResult``. Only ``RECORDED`` mutates the
    authoritative terminal projection; the other variants are no-ops on the
    terminal state (the receipt may still be retained as audit/debug data).
    """

    RECORDED = "recorded"
    DUPLICATE = "duplicate"
    STALE_GENERATION = "stale_generation"
    TERMINAL_CONFLICT = "terminal_conflict"


class ReceiptProjection:
    """Authoritative outcome projection for one ``causation_id``.

    A pure reducer that folds :class:`CausalReceipt` events into the current
    outcome for a causation id, mirroring ``LazilyFormal.Receipt.apply``. The
    rules from ``lazily-spec/protocol.md § Causal Receipts``:

    * ``observed`` and ``accepted`` are **non-terminal**. They never complete
      the causation and never conflict with a terminal outcome.
    * ``applied`` and ``rejected`` are **terminal**. The first terminal receipt
      for a generation fixes the outcome; a second terminal receipt with a
      *different* outcome is a **terminal conflict** (fail closed, no winner).
    * A receipt whose ``generation`` differs from the authority's current
      generation is **stale** and ignored by the current projection.
    * A duplicate ``receipt_id`` is an idempotent no-op.

    The authority ``current_generation`` is supplied by the caller (the consumer
    that knows the producer/editor generation for the causation id). When
    projecting a frame without external authority, :meth:`from_receipts`
    defaults ``current_generation`` to the maximum generation seen among the
    receipts for the causation id — the natural choice that makes older
    generations stale.
    """

    __slots__ = (
        "_conflicts",
        "_recorded",
        "_seen",
        "_stale",
        "_terminal",
        "causation_id",
        "current_generation",
    )

    def __init__(self, causation_id: str, current_generation: int) -> None:
        if not causation_id:
            raise ValueError("causation_id must be a non-empty string")
        if current_generation < 0:
            raise ValueError(
                f"current_generation must be >= 0, got {current_generation}"
            )
        self.causation_id = causation_id
        self.current_generation = current_generation
        self._seen: set[str] = set()
        self._terminal: ReceiptOutcome | None = None
        self._recorded: list[CausalReceipt] = []
        self._stale: list[CausalReceipt] = []
        self._conflicts: list[CausalReceipt] = []

    @classmethod
    def from_receipts(
        cls,
        causation_id: str,
        receipts: Iterable[CausalReceipt],
        current_generation: int | None = None,
    ) -> ReceiptProjection:
        """Build a projection by folding ``receipts`` for one causation id.

        Only receipts whose ``causation_id`` matches are applied (others are
        ignored). When ``current_generation`` is ``None`` it defaults to the
        maximum generation among the matching receipts (or ``0`` when empty),
        the natural authority for a frame-replay without external state.
        """
        matching = [r for r in receipts if r.causation_id == causation_id]
        if current_generation is None:
            current_generation = (
                max((r.generation for r in matching), default=0) if matching else 0
            )
        projection = cls(causation_id, current_generation)
        for receipt in matching:
            projection.apply(receipt)
        return projection

    def apply(self, receipt: CausalReceipt) -> ReceiptApplyResult:
        """Fold one receipt into the projection.

        Returns the :class:`ReceiptApplyResult`. ``RECORDED`` updates the
        authoritative projection (and the recorded/audit trail); every other
        result leaves the terminal outcome untouched but still classifies the
        receipt into the appropriate audit bucket (stale / conflict) so a caller
        can retain it as debug data per the spec.
        """
        if receipt.receipt_id in self._seen:
            return ReceiptApplyResult.DUPLICATE
        self._seen.add(receipt.receipt_id)
        if receipt.generation != self.current_generation:
            self._stale.append(receipt)
            return ReceiptApplyResult.STALE_GENERATION
        if receipt.outcome.is_terminal:
            if self._terminal is None:
                self._terminal = receipt.outcome
                self._recorded.append(receipt)
                return ReceiptApplyResult.RECORDED
            if self._terminal is receipt.outcome:
                self._recorded.append(receipt)
                return ReceiptApplyResult.RECORDED
            self._conflicts.append(receipt)
            return ReceiptApplyResult.TERMINAL_CONFLICT
        self._recorded.append(receipt)
        return ReceiptApplyResult.RECORDED

    @property
    def terminal_outcome(self) -> ReceiptOutcome | None:
        """The current terminal outcome for the causation id, or ``None``."""
        return self._terminal

    @property
    def is_terminal(self) -> bool:
        """Whether a terminal outcome has been recorded for the causation id."""
        return self._terminal is not None

    @property
    def in_conflict(self) -> bool:
        """Whether a conflicting terminal outcome was observed (fail closed)."""
        return bool(self._conflicts)

    def recorded(self) -> list[CausalReceipt]:
        """The receipts the authority projection retained (non-stale)."""
        return list(self._recorded)

    def nonterminal_outcomes(self) -> list[ReceiptOutcome]:
        """Non-terminal outcomes currently recorded, in first-seen order."""
        seen: set[ReceiptOutcome] = set()
        ordered: list[ReceiptOutcome] = []
        for receipt in self._recorded:
            if not receipt.outcome.is_terminal and receipt.outcome not in seen:
                seen.add(receipt.outcome)
                ordered.append(receipt.outcome)
        return ordered

    def stale_receipt_ids(self) -> list[str]:
        """``receipt_id`` s discarded as stale (audit/debug trail only)."""
        return [r.receipt_id for r in self._stale]

    def conflicting_receipt_ids(self) -> list[str]:
        """``receipt_id`` s that hit a terminal conflict (audit trail only)."""
        return [r.receipt_id for r in self._conflicts]

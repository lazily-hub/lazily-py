"""Cross-process zero-copy transport — pluggable blob backends (``#lzzcpy``).

Spec: ``lazily-spec/docs/zero-copy-transport.md``.
Formal: ``lazily-formal/LazilyFormal/ZeroCopyTransport.lean``.
Rust reference: ``lazily-rs/src/transport.rs``.

A large payload is not copied through the wire codec. The producer **spills** it
to a blob backend (the backend mints a :class:`~lazily.ipc.ShmBlobRef` descriptor)
and ships only the descriptor; the receiver **resolves** the descriptor against
the same backend and reads the bytes in place — zero copy. :class:`BlobBackend`
is the adapter seam:

- :class:`InProcessBackend` wraps :class:`~lazily.ipc.ShmBlobArena` — single
  address space (the FFI host / an editor plugin loaded in the same process).
- :class:`ArrowBackend` holds Apache Arrow IPC stream bytes — the descriptor's
  bytes are an Arrow IPC stream the receiver imports as an ``Array`` /
  ``RecordBatch`` with no copy (bring your own ``pyarrow`` around the resolved
  view).
- :class:`ShmBackend` is a POSIX ``shm_open`` + ``mmap`` region (via
  :mod:`multiprocessing.shared_memory`) — the cross-process backend (same host).

Because the formal laws (spill-then-resolve identity, backend isolation, ABA
generation safety, checksum integrity) are stated only over a backend's
issued-blob table, they hold uniformly for every backend that maintains the
:class:`BlobBackend` contract.
"""

from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import TYPE_CHECKING, cast

from .ipc import (
    BlobBackendKind,
    CrdtSync,
    Delta,
    DeltaOp_CellSet,
    DeltaOp_NodeAdd,
    DeltaOp_SlotValue,
    IpcMessage,
    IpcValue,
    IpcValue_Inline,
    IpcValue_SharedBlob,
    NodeState,
    NodeState_Payload,
    NodeState_SharedBlob,
    ShmBlobArena,
    ShmBlobArenaError,
    ShmBlobRef,
    Snapshot,
)


if TYPE_CHECKING:
    from collections.abc import Mapping
    from multiprocessing.shared_memory import SharedMemory

__all__ = [
    "ARROW_DEFAULT_CAPACITY",
    "IN_PROCESS_DEFAULT_CAPACITY",
    "SHM_DEFAULT_CAPACITY",
    "ArrowBackend",
    "BlobBackend",
    "BlobRouter",
    "BlobView",
    "InProcessBackend",
    "ShmBackend",
    "resolve_value",
    "spill_message",
    "spill_state",
    "spill_value",
]

# A zero-copy view into a backend's resolved bytes, or ``None`` when the
# descriptor did not resolve (unknown / stale-generation / corrupt-checksum /
# wrong-backend). An empty payload that resolves correctly is an empty
# ``memoryview``, never ``None``.
type BlobView = memoryview | None

#: Default in-process backing capacity (1 MiB).
IN_PROCESS_DEFAULT_CAPACITY = 1 << 20
#: Default Arrow backing capacity (4 MiB — analytics payloads tend to be larger).
ARROW_DEFAULT_CAPACITY = 1 << 22
#: Default POSIX shared-memory region capacity (1 MiB).
SHM_DEFAULT_CAPACITY = 1 << 20


class BlobBackend(ABC):
    """The adapter seam: a backend mints descriptors via :meth:`write` and
    resolves them zero-copy via :meth:`read_view`.

    Entries are immutable and stable-addressed for any descriptor's lifetime.
    The formal laws (``resolve_write`` identity, backend isolation, ABA
    generation safety, checksum rejection) hold for every backend that maintains
    this contract.
    """

    @abstractmethod
    def kind(self) -> BlobBackendKind:
        """Which backend discriminator this adapter serves."""

    @abstractmethod
    def write(self, data: bytes | bytearray | memoryview) -> ShmBlobRef:
        """Mint a fresh descriptor for ``data``: store the bytes immutably and
        return a descriptor whose checksum is the bytes' FNV-1a-64.

        Raises :class:`~lazily.ipc.ShmBlobArenaError` if the backend cannot store
        the payload (e.g. capacity exhausted).
        """

    @abstractmethod
    def read_view(self, descriptor: ShmBlobRef) -> memoryview | None:
        """Resolve ``descriptor`` zero-copy — return the stored bytes iff
        ``generation + epoch + len + checksum`` all match; ``None`` otherwise.
        **No copy, no checksum recompute** beyond the arena's read-time
        validation.
        """

    @abstractmethod
    def advance_epoch(self) -> None:
        """Advance the validity epoch. Descriptors minted before an epoch advance
        no longer resolve (models compaction / restart).
        """


class _ArenaBackend(BlobBackend):
    """Shared implementation for backends that store bytes in a
    :class:`~lazily.ipc.ShmBlobArena` and stamp a fixed backend discriminator.

    :class:`InProcessBackend` and :class:`ArrowBackend` differ only in the
    discriminator they mint and their default capacity — the store, the epoch
    guard, and zero-copy resolution are identical.
    """

    _kind: BlobBackendKind

    def __init__(self, arena: ShmBlobArena) -> None:
        self._arena = arena
        self._epoch = 0

    @property
    def arena(self) -> ShmBlobArena:
        """Borrow the backing arena."""
        return self._arena

    @property
    def epoch(self) -> int:
        """Current validity epoch."""
        return self._epoch

    def kind(self) -> BlobBackendKind:
        return self._kind

    def write(self, data: bytes | bytearray | memoryview) -> ShmBlobRef:
        descriptor = self._arena.write_blob(self._epoch, data)
        return replace(descriptor, backend=self._kind)

    def read_view(self, descriptor: ShmBlobRef) -> memoryview | None:
        # Immediate epoch invalidation: a descriptor minted before an epoch
        # advance does not resolve even if its slot bytes are still intact.
        if descriptor.epoch != self._epoch:
            return None
        try:
            return self._arena.read_blob(descriptor)
        except ShmBlobArenaError:
            return None

    def advance_epoch(self) -> None:
        self._epoch += 1


class InProcessBackend(_ArenaBackend):
    """Default in-process backend: wraps :class:`~lazily.ipc.ShmBlobArena` for the
    single-address-space case (the FFI host ↔ a binding loaded in the same
    process, an editor plugin).

    Descriptors carry ``backend = IN_PROCESS``. The backing arena is a
    fixed-capacity append buffer with wraparound; the generation/epoch/checksum
    guards reject stale descriptors after wraparound or an epoch advance. For an
    unbounded cross-process store, spill to a :class:`ShmBackend` instead.
    """

    _kind = BlobBackendKind.IN_PROCESS

    def __init__(self, capacity: int = IN_PROCESS_DEFAULT_CAPACITY) -> None:
        super().__init__(ShmBlobArena.with_capacity(capacity))

    @classmethod
    def from_arena(cls, arena: ShmBlobArena) -> InProcessBackend:
        """Wrap an existing arena at epoch 0."""
        backend = cls.__new__(cls)
        _ArenaBackend.__init__(backend, arena)
        return backend


class ArrowBackend(_ArenaBackend):
    """Apache Arrow blob backend: holds spilled payloads as Arrow IPC stream bytes
    and resolves a descriptor to the buffer's raw bytes with no copy.

    The descriptor's bytes **are** an Arrow IPC stream — a columnar consumer
    imports them as an ``Array`` / ``RecordBatch`` zero-copy (the Arrow IPC format
    is itself zero-copy across a shared buffer). This adapter stores the raw
    stream bytes and tags the descriptor ``backend = ARROW``; bring your own
    ``pyarrow`` to wrap the resolved :class:`memoryview` into typed Arrow.

    Because Arrow's IPC format is zero-copy over a shared buffer, ``shm`` and
    ``arrow`` compose: an Arrow batch can live in a :class:`ShmBackend` region and
    be resolved by either backend. New backends (RDMA/verbs, CUDA IPC) plug in by
    subclassing :class:`BlobBackend` and adding a :class:`BlobBackendKind` value.
    """

    _kind = BlobBackendKind.ARROW

    def __init__(self, capacity: int = ARROW_DEFAULT_CAPACITY) -> None:
        super().__init__(ShmBlobArena.with_capacity(capacity))


class ShmBackend(BlobBackend):
    """POSIX shared-memory backend: a named :mod:`multiprocessing.shared_memory`
    region (``shm_open`` + ``mmap``) shared across processes on the same host.

    The producer :meth:`create`\\ s a named region and :meth:`write`\\ s payloads
    into it; a distinct process :meth:`open`\\ s the region by name and resolves
    descriptors minted by the producer **zero-copy** — :meth:`read_view` returns a
    view into the mapped region, not a copy. Descriptors carry ``backend = SHM``
    (the default), so a legacy shared-memory descriptor resolves here unchanged.

    Lifecycle mirrors POSIX shared memory: the creator owns :meth:`unlink` timing.
    Call :meth:`close` on every handle when done and :meth:`unlink` once no
    further readers/writers remain.

    The in-region byte layout is :class:`~lazily.ipc.ShmBlobArena`'s (a 40-byte
    header per blob); it is a Python-binding implementation detail. The **wire
    descriptor** is the cross-language contract, not the region layout.
    """

    def __init__(self, shm: SharedMemory, arena: ShmBlobArena, *, owner: bool) -> None:
        # Constructed via `create` / `open`.
        self._shm = shm
        self._arena = arena
        self._owner = owner
        self._epoch = 0
        self._closed = False

    @classmethod
    def create(cls, name: str, capacity: int = SHM_DEFAULT_CAPACITY) -> ShmBackend:
        """Create (or replace) a named POSIX shared-memory region of ``capacity``
        bytes and map it as a fresh arena. The caller owns :meth:`unlink` timing.
        """
        from multiprocessing import shared_memory

        try:
            shm = shared_memory.SharedMemory(name=name, create=True, size=capacity)
        except FileExistsError:
            # Replace a stale region left by a crashed producer.
            stale = shared_memory.SharedMemory(name=name)
            stale.close()
            stale.unlink()
            shm = shared_memory.SharedMemory(name=name, create=True, size=capacity)
        except OSError as exc:  # pragma: no cover - platform-dependent
            raise ShmBlobArenaError(f"shm create failed: {exc}") from exc
        # Zero the region so the arena starts from a clean header state.
        buf = cast("bytearray", shm.buf)
        buf[:] = bytes(len(buf))
        arena = ShmBlobArena.from_buffer(buf)
        return cls(shm, arena, owner=True)

    @classmethod
    def open(cls, name: str) -> ShmBackend:
        """Open (without creating) an existing named POSIX shared-memory region.

        A distinct process uses this to resolve descriptors minted by the
        creator. The returned backend is read-oriented: :meth:`write` appends
        from its own cursor and would collide with the creator's, so a reader
        opens for :meth:`read_view` only.
        """
        from multiprocessing import shared_memory

        try:
            shm = shared_memory.SharedMemory(name=name)
        except OSError as exc:
            raise ShmBlobArenaError(f"shm open failed: {exc}") from exc
        arena = ShmBlobArena.from_buffer(cast("bytearray", shm.buf))
        return cls(shm, arena, owner=False)

    @property
    def name(self) -> str:
        """The region's OS name (share this with peers to :meth:`open`)."""
        return self._shm.name

    @property
    def epoch(self) -> int:
        """The backend's validity epoch."""
        return self._epoch

    def kind(self) -> BlobBackendKind:
        return BlobBackendKind.SHM

    def write(self, data: bytes | bytearray | memoryview) -> ShmBlobRef:
        if self._closed:
            raise ShmBlobArenaError("shm backend is closed")
        descriptor = self._arena.write_blob(self._epoch, data)
        return replace(descriptor, backend=BlobBackendKind.SHM)

    def read_view(self, descriptor: ShmBlobRef) -> memoryview | None:
        if self._closed or descriptor.epoch != self._epoch:
            return None
        try:
            return self._arena.read_blob(descriptor)
        except ShmBlobArenaError:
            return None

    def advance_epoch(self) -> None:
        self._epoch += 1

    def close(self) -> None:
        """Release this handle's mapping. Idempotent."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(OSError, BufferError):  # best-effort
            self._shm.close()

    def unlink(self) -> None:
        """Remove the named region so the OS reclaims it once all handles unmap.

        Only the creating handle should unlink.
        """
        with contextlib.suppress(OSError):  # already gone
            self._shm.unlink()

    def __enter__(self) -> ShmBackend:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
        if self._owner:
            self.unlink()


# ---------------------------------------------------------------------------
# Spill policy: replace large Inline payloads with a SharedBlob descriptor.
# ---------------------------------------------------------------------------


def spill_value(
    value: IpcValue, backend: BlobBackend, threshold: int
) -> tuple[IpcValue, int]:
    """If ``value`` is :class:`~lazily.ipc.IpcValue_Inline` and ``>= threshold``
    bytes, write it to ``backend`` and return a
    :class:`~lazily.ipc.IpcValue_SharedBlob` descriptor plus the number of bytes
    spilled. Otherwise return ``value`` unchanged with ``0``.

    Payloads below the threshold stay inline — cheaper than a backend round-trip
    for tiny values. The threshold is a session/deployment knob, not a protocol
    constant. Because :class:`~lazily.ipc.IpcValue` is immutable, this returns the
    (possibly new) value rather than mutating in place as the Rust reference does.
    """
    if isinstance(value, IpcValue_Inline) and len(value.data) >= threshold:
        try:
            descriptor = backend.write(value.data)
        except ShmBlobArenaError:
            return value, 0
        return IpcValue_SharedBlob(descriptor), len(value.data)
    return value, 0


def spill_state(
    state: NodeState, backend: BlobBackend, threshold: int
) -> tuple[NodeState, int]:
    """Spill a :class:`~lazily.ipc.NodeState_Payload` above ``threshold`` to a
    :class:`~lazily.ipc.NodeState_SharedBlob` descriptor. Returns the (possibly
    new) state and the number of bytes spilled.
    """
    if isinstance(state, NodeState_Payload) and len(state.data) >= threshold:
        try:
            descriptor = backend.write(state.data)
        except ShmBlobArenaError:
            return state, 0
        return NodeState_SharedBlob(descriptor), len(state.data)
    return state, 0


def spill_message(message: IpcMessage, backend: BlobBackend, threshold: int) -> int:
    """Spill large payloads across an :class:`~lazily.ipc.IpcMessage`'s
    value/state sites and return the total bytes spilled.

    Snapshot node states, Delta ``CellSet`` / ``SlotValue`` payloads + ``NodeAdd``
    states, and ``CrdtSync`` op states are each written to ``backend`` when above
    ``threshold`` and replaced with a descriptor — the message stays small on the
    wire. Sites already carrying a descriptor are left untouched.

    The message's op/node lists are mutated in place (their frozen elements are
    replaced), matching the Rust reference's in-place spill semantics.
    """
    total = 0
    snapshot = message.snapshot
    delta = message.delta
    crdt_sync = message.crdt_sync
    if snapshot is not None:
        total += _spill_snapshot(snapshot, backend, threshold)
    elif delta is not None:
        total += _spill_delta(delta, backend, threshold)
    elif crdt_sync is not None:
        total += _spill_crdt_sync(crdt_sync, backend, threshold)
    return total


def _spill_snapshot(snapshot: Snapshot, backend: BlobBackend, threshold: int) -> int:
    total = 0
    for i, node in enumerate(snapshot.nodes):
        new_state, spilled = spill_state(node.state, backend, threshold)
        if spilled:
            snapshot.nodes[i] = replace(node, state=new_state)
            total += spilled
    return total


def _spill_delta(delta: Delta, backend: BlobBackend, threshold: int) -> int:
    total = 0
    for i, op in enumerate(delta.ops):
        if isinstance(op, (DeltaOp_CellSet, DeltaOp_SlotValue)):
            new_payload, spilled = spill_value(op.payload, backend, threshold)
            if spilled:
                delta.ops[i] = replace(op, payload=new_payload)
                total += spilled
        elif isinstance(op, DeltaOp_NodeAdd):
            new_state, spilled = spill_state(op.state, backend, threshold)
            if spilled:
                delta.ops[i] = replace(op, state=new_state)
                total += spilled
    return total


def _spill_crdt_sync(sync: CrdtSync, backend: BlobBackend, threshold: int) -> int:
    total = 0
    for i, op in enumerate(sync.ops):
        new_state, spilled = spill_value(op.state, backend, threshold)
        if spilled:
            sync.ops[i] = replace(op, state=new_state)
            total += spilled
    return total


# ---------------------------------------------------------------------------
# Resolve: inline bytes returned directly, SharedBlob resolved zero-copy.
# ---------------------------------------------------------------------------


def resolve_value(value: IpcValue, backend: BlobBackend) -> memoryview | None:
    """Resolve an :class:`~lazily.ipc.IpcValue` against a single backend: inline
    bytes returned directly, :class:`~lazily.ipc.IpcValue_SharedBlob` resolved
    zero-copy. Returns ``None`` if a SharedBlob fails to resolve
    (unknown/stale/corrupt).
    """
    if isinstance(value, IpcValue_Inline):
        return memoryview(value.data)
    if isinstance(value, IpcValue_SharedBlob):
        return backend.read_view(value.blob)
    raise TypeError(f"cannot resolve {type(value).__name__} as an IpcValue")


class BlobRouter:
    """Receiver-side multi-backend resolver.

    Holds backends by :class:`BlobBackendKind` and resolves any descriptor by its
    ``backend`` discriminator — a ``shm`` descriptor routes to the shm backend, an
    ``arrow`` descriptor to the arrow backend, etc. (the ``resolve_wrong_backend``
    theorem: a descriptor never resolves against a backend of the wrong kind).
    """

    def __init__(self) -> None:
        self._backends: dict[BlobBackendKind, BlobBackend] = {}

    def register(self, backend: BlobBackend) -> BlobRouter:
        """Register a backend for its :meth:`~BlobBackend.kind`. Replaces any
        previously-registered backend of the same kind. Returns ``self`` for
        chaining.
        """
        self._backends[backend.kind()] = backend
        return self

    @property
    def backends(self) -> Mapping[BlobBackendKind, BlobBackend]:
        """The registered backends by kind."""
        return self._backends

    def read_view(self, descriptor: ShmBlobRef) -> memoryview | None:
        """Resolve a descriptor by routing to its ``backend`` kind. Returns
        ``None`` if no backend is registered for this kind, or the descriptor did
        not resolve.
        """
        backend = self._backends.get(descriptor.backend)
        if backend is None:
            return None
        return backend.read_view(descriptor)

    def resolve(self, value: IpcValue) -> memoryview | None:
        """Resolve an :class:`~lazily.ipc.IpcValue`: inline bytes returned
        directly, SharedBlob routed by the descriptor's ``backend`` discriminator.
        """
        if isinstance(value, IpcValue_Inline):
            return memoryview(value.data)
        if isinstance(value, IpcValue_SharedBlob):
            return self.read_view(value.blob)
        raise TypeError(f"cannot resolve {type(value).__name__} as an IpcValue")

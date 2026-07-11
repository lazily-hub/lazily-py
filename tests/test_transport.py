"""Cross-process zero-copy transport tests (``#lzzcpy``).

Mirrors the ``lazily-rs`` ``transport.rs`` test suite: the backend-agnostic
formal laws (``resolve_write`` identity, backend isolation, ABA generation
safety, checksum integrity, epoch invalidation) and the end-to-end spill/resolve
round-trip across every ``IpcMessage`` payload site.
"""

from __future__ import annotations

import dataclasses
import os

import pytest

from lazily.ipc import (
    BlobBackendKind,
    CrdtOp,
    CrdtSync,
    Delta,
    DeltaOp,
    IpcMessage,
    IpcValue_Inline,
    IpcValue_SharedBlob,
    NodeId,
    NodeSnapshot,
    NodeState_SharedBlob,
    ShmBlobArena,
    ShmBlobArenaError,
    Snapshot,
    WireStamp,
)
from lazily.transport import (
    ArrowBackend,
    BlobRouter,
    InProcessBackend,
    ShmBackend,
    resolve_value,
    spill_message,
    spill_value,
)


# ---------------------------------------------------------------------------
# resolve_write identity (transport_roundtrip)
# ---------------------------------------------------------------------------


def test_in_process_resolve_write() -> None:
    backend = InProcessBackend()
    payload = bytes([1, 2, 3, 4, 5, 6, 7, 8])
    desc = backend.write(payload)
    assert desc.backend is BlobBackendKind.IN_PROCESS
    assert bytes(backend.read_view(desc)) == payload


def test_arrow_resolve_write() -> None:
    backend = ArrowBackend()
    payload = bytes([10, 20, 30, 40])
    desc = backend.write(payload)
    assert desc.backend is BlobBackendKind.ARROW
    assert bytes(backend.read_view(desc)) == payload


def test_empty_payload_resolves_to_empty_view_not_none() -> None:
    backend = InProcessBackend()
    desc = backend.write(b"")
    view = backend.read_view(desc)
    assert view is not None
    assert bytes(view) == b""


# ---------------------------------------------------------------------------
# Backend isolation (resolve_wrong_backend)
# ---------------------------------------------------------------------------


def test_backend_isolation() -> None:
    inproc = InProcessBackend()
    desc = inproc.write(bytes([9, 9, 9]))

    # No backends registered → does not resolve.
    empty = BlobRouter()
    assert empty.read_view(desc) is None

    router = BlobRouter().register(inproc)
    assert router.read_view(desc) is not None

    # A shm-kind descriptor with no shm backend registered → None.
    shm_desc = dataclasses.replace(desc, backend=BlobBackendKind.SHM)
    assert router.read_view(shm_desc) is None


def test_multi_backend_routing() -> None:
    inproc = InProcessBackend()
    arrow = ArrowBackend()
    inproc_desc = inproc.write(b"inproc bytes")
    arrow_desc = arrow.write(b"arrow bytes")

    router = BlobRouter().register(inproc).register(arrow)
    assert bytes(router.read_view(inproc_desc)) == b"inproc bytes"
    assert bytes(router.read_view(arrow_desc)) == b"arrow bytes"

    # An arrow descriptor never resolves against the in_process table and vice
    # versa — the router routes strictly by the `backend` discriminator.
    assert bytes(arrow.read_view(arrow_desc)) == b"arrow bytes"
    assert (
        inproc.read_view(
            dataclasses.replace(arrow_desc, backend=BlobBackendKind.IN_PROCESS)
        )
        is None
    )


# ---------------------------------------------------------------------------
# ABA generation safety + checksum integrity + epoch
# ---------------------------------------------------------------------------


def test_stale_generation_rejects() -> None:
    backend = InProcessBackend()
    desc = backend.write(bytes([1, 2, 3]))
    stale = dataclasses.replace(desc, generation=desc.generation + 1)
    assert backend.read_view(stale) is None


def test_corrupt_checksum_rejects() -> None:
    backend = InProcessBackend()
    desc = backend.write(bytes([4, 5, 6]))
    corrupt = dataclasses.replace(desc, checksum=desc.checksum + 1)
    assert backend.read_view(corrupt) is None


def test_epoch_advance_invalidates() -> None:
    backend = InProcessBackend()
    desc = backend.write(bytes([7, 8]))
    assert backend.read_view(desc) is not None
    backend.advance_epoch()
    assert backend.read_view(desc) is None


# ---------------------------------------------------------------------------
# Spill policy end-to-end (transport_roundtrip across message sites)
# ---------------------------------------------------------------------------


def test_spill_resolve_round_trip() -> None:
    backend = InProcessBackend()
    big = bytes([0x5A]) * 500
    msg = IpcMessage.of_delta(Delta.next(1, [DeltaOp.slot_value(NodeId(7), big)]))

    spilled = spill_message(msg, backend, 64)
    assert spilled == len(big)

    op = msg.delta.ops[0]
    assert isinstance(op.payload, IpcValue_SharedBlob)

    router = BlobRouter().register(backend)
    assert bytes(router.resolve(op.payload)) == big


def test_spill_snapshot_and_crdt() -> None:
    backend = InProcessBackend()
    big = bytes([0xAB]) * 300

    snap = IpcMessage.of_snapshot(
        Snapshot(epoch=1, nodes=[NodeSnapshot.payload(NodeId(1), "blob", big)])
    )
    assert spill_message(snap, backend, 64) == len(big)
    assert isinstance(snap.snapshot.nodes[0].state, NodeState_SharedBlob)

    stamp = WireStamp(wall_time=1, logical=0, peer=1)
    crdt = IpcMessage.of_crdt_sync(
        CrdtSync.new([(1, stamp)], [CrdtOp.new(NodeId(1), stamp, big)])
    )
    assert spill_message(crdt, backend, 64) == len(big)
    assert isinstance(crdt.crdt_sync.ops[0].state, IpcValue_SharedBlob)


def test_sub_threshold_stays_inline() -> None:
    backend = InProcessBackend()
    msg = IpcMessage.of_delta(
        Delta.next(1, [DeltaOp.slot_value(NodeId(1), bytes([1, 2, 3]))])
    )
    assert spill_message(msg, backend, 64) == 0
    assert isinstance(msg.delta.ops[0].payload, IpcValue_Inline)


def test_spill_value_leaves_inline_when_backend_full() -> None:
    # A backend too small to hold the payload leaves the value inline (0 spilled)
    # rather than raising — mirrors the Rust `Err(_) => return 0` fallback.
    tiny = InProcessBackend(capacity=64)
    big = IpcValue_Inline(bytes([1]) * 200)
    value, spilled = spill_value(big, tiny, 8)
    assert spilled == 0
    assert value is big


def test_resolve_value_inline_passthrough() -> None:
    backend = InProcessBackend()
    value = IpcValue_Inline(b"inline data")
    assert bytes(resolve_value(value, backend)) == b"inline data"


# ---------------------------------------------------------------------------
# POSIX shared-memory backend (cross-process, same host)
# ---------------------------------------------------------------------------


def test_shm_backend_round_trip() -> None:
    name = f"lazily_shm_test_{os.getpid()}"
    backend = ShmBackend.create(name, 1 << 20)
    try:
        payload = bytes((i * 7 + 1) & 0xFF for i in range(1000))
        desc = backend.write(payload)
        assert desc.backend is BlobBackendKind.SHM
        assert bytes(backend.read_view(desc)) == payload
        backend.advance_epoch()
        assert backend.read_view(desc) is None  # epoch advance invalidates
    finally:
        backend.close()
        backend.unlink()


def test_shm_backend_resolves_across_handles() -> None:
    # A second handle opened by name resolves a descriptor minted by the creator
    # zero-copy — the cross-process resolution path (here two handles in one
    # process against the same shared region).
    name = f"lazily_shm_xhandle_{os.getpid()}"
    writer = ShmBackend.create(name, 1 << 20)
    try:
        payload = bytes((i * 3 + 5) & 0xFF for i in range(2048))
        desc = writer.write(payload)

        reader = ShmBackend.open(name)
        try:
            assert bytes(reader.read_view(desc)) == payload
            # A corrupted descriptor is rejected by the reader, not misread.
            corrupt = dataclasses.replace(desc, checksum=desc.checksum ^ 0xFF)
            assert reader.read_view(corrupt) is None
        finally:
            reader.close()
    finally:
        writer.close()
        writer.unlink()


def test_shm_backend_context_manager_unlinks() -> None:
    name = f"lazily_shm_ctx_{os.getpid()}"
    with ShmBackend.create(name, 1 << 16) as backend:
        desc = backend.write(b"scoped payload")
        assert bytes(backend.read_view(desc)) == b"scoped payload"
    # After the context exits the owning region is unlinked; opening fails.
    with pytest.raises(ShmBlobArenaError):
        ShmBackend.open(name)


# ---------------------------------------------------------------------------
# Arrow IPC-stream composition
# ---------------------------------------------------------------------------


def test_arrow_ipc_stream_bytes() -> None:
    # The descriptor's bytes ARE an Arrow IPC stream (here a stand-in magic
    # prefix); the backend stores and resolves the raw bytes with no copy — a
    # columnar consumer wraps pyarrow around the resolved view.
    arrow = ArrowBackend()
    ipc_stream = bytes([0x41, 0x52, 0x52, 0x4F, 0x57, 0x31, 0x00, 0x00])  # "ARROW1\0\0"
    desc = arrow.write(ipc_stream)
    assert desc.backend is BlobBackendKind.ARROW
    assert bytes(arrow.read_view(desc)) == ipc_stream


def test_in_process_and_shm_descriptors_share_arena_contract() -> None:
    # An arena-backed backend and a directly-built arena agree on the header
    # contract: a descriptor written through InProcessBackend resolves against
    # the underlying arena once its backend tag is normalized away.
    backend = InProcessBackend()
    desc = backend.write(b"arena contract")
    arena: ShmBlobArena = backend.arena
    assert bytes(arena.read_blob(desc)) == b"arena contract"

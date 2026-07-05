"""
lazily — a lazy/reactive evaluation library for Python with context-aware
callable wrappers.

This package provides a simple and elegant way to implement lazy evaluation and
dependency injection patterns in Python applications. The core reactive family is
``Slot`` (lazy memo) → ``Cell`` (mutable source) → ``Signal`` (eager derived).

The :mod:`lazily.ipc` submodule implements the language-agnostic ``lazily-spec``
wire protocol (Snapshot / Delta) so a Python graph's state can be mirrored to
remote observers across processes and languages.
"""

__all__ = [
    "FFI_HEADER_LEN",
    "NODE_KEY_MAX_LEN",
    "NODE_KEY_MAX_SEGMENTS",
    "PROTOCOL_ID",
    "PROTOCOL_MAJOR_VERSION",
    "SHM_BLOB_HEADER_LEN",
    "AsyncEffect",
    "AsyncSlot",
    "BaseSlot",
    "CapabilityHandshake",
    "CausalReceipt",
    "CausalReceipts",
    "Cell",
    "CellFamily",
    "CellMap",
    "CellSlot",
    "CellTree",
    "ChartDef",
    "CrdtOp",
    "CrdtSync",
    "Delta",
    "DeltaApplyStatus",
    "DeltaOp",
    "EdgeSnapshot",
    "EffectEvent",
    "EffectState",
    "IpcMessage",
    "IpcValue",
    "LazilyCallable",
    "LazilyFfiBytes",
    "LazilyFfiMessageKind",
    "LazilyFfiStatus",
    "Level",
    "NodeId",
    "NodeKey",
    "NodeKeyError",
    "NodeSnapshot",
    "NodeState",
    "OpKind",
    "PeerId",
    "PeerPermissions",
    "PermissionDenied",
    "ReceiptApplyResult",
    "ReceiptOutcome",
    "ReceiptProjection",
    "ReconcileOp",
    "RemoteOp",
    "ShmBlobArena",
    "ShmBlobArenaError",
    "ShmBlobRef",
    "Signal",
    "Slot",
    "SlotEvent",
    "SlotState",
    "Snapshot",
    "StateChart",
    "StateMachine",
    "ThreadSafeContext",
    "TreeNode",
    "WireStamp",
    "async_slot",
    "cell",
    "cell_def",
    "decode_message",
    "encode_message",
    "ffi",
    "ffi_bytes_of",
    "ipc",
    "kind_of",
    "reconcile_ops",
    "signal",
    "signal_def",
    "slot",
    "slot_def",
    "tree",
]
__version__ = "0.15.0"

from . import async_slot, ffi, ipc, tree
from .async_effect import AsyncEffect, EffectEvent, EffectState
from .async_slot import AsyncSlot, SlotEvent, SlotState
from .cell import Cell, CellSlot, cell, cell_def
from .collection import CellFamily, CellMap
from .ffi import (
    FFI_HEADER_LEN,
    LazilyFfiBytes,
    LazilyFfiMessageKind,
    LazilyFfiStatus,
    decode_message,
    encode_message,
    ffi_bytes_of,
    kind_of,
)
from .ipc import (
    NODE_KEY_MAX_LEN,
    NODE_KEY_MAX_SEGMENTS,
    PROTOCOL_ID,
    PROTOCOL_MAJOR_VERSION,
    SHM_BLOB_HEADER_LEN,
    CapabilityHandshake,
    CausalReceipt,
    CausalReceipts,
    CrdtOp,
    CrdtSync,
    Delta,
    DeltaApplyStatus,
    DeltaOp,
    EdgeSnapshot,
    IpcMessage,
    IpcValue,
    NodeId,
    NodeKey,
    NodeKeyError,
    NodeSnapshot,
    NodeState,
    OpKind,
    PeerId,
    PeerPermissions,
    PermissionDenied,
    ReceiptApplyResult,
    ReceiptOutcome,
    ReceiptProjection,
    RemoteOp,
    ShmBlobArena,
    ShmBlobArenaError,
    ShmBlobRef,
    Snapshot,
    WireStamp,
)
from .reconciliation import Level, ReconcileOp, reconcile_ops
from .signal import Signal, signal, signal_def
from .slot import BaseSlot, Slot, slot, slot_def
from .state_machine import StateMachine
from .statechart import ChartDef, StateChart
from .thread_safe import ThreadSafeContext
from .tree import CellTree, TreeNode
from .types import LazilyCallable

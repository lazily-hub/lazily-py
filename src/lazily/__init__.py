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
    "NODE_KEY_MAX_LEN",
    "NODE_KEY_MAX_SEGMENTS",
    "PROTOCOL_ID",
    "PROTOCOL_MAJOR_VERSION",
    "SHM_BLOB_HEADER_LEN",
    "BaseSlot",
    "CapabilityHandshake",
    "Cell",
    "CellSlot",
    "ChartDef",
    "CrdtOp",
    "CrdtSync",
    "Delta",
    "DeltaApplyStatus",
    "DeltaOp",
    "EdgeSnapshot",
    "IpcMessage",
    "IpcValue",
    "LazilyCallable",
    "NodeId",
    "NodeKey",
    "NodeKeyError",
    "NodeSnapshot",
    "NodeState",
    "OpKind",
    "PeerId",
    "PeerPermissions",
    "PermissionDenied",
    "RemoteOp",
    "ShmBlobArena",
    "ShmBlobArenaError",
    "ShmBlobRef",
    "Signal",
    "Slot",
    "Snapshot",
    "StateChart",
    "StateMachine",
    "WireStamp",
    "cell",
    "cell_def",
    "ipc",
    "signal",
    "signal_def",
    "slot",
    "slot_def",
]
__version__ = "0.13.0"

from . import ipc
from .cell import Cell, CellSlot, cell, cell_def
from .ipc import (
    NODE_KEY_MAX_LEN,
    NODE_KEY_MAX_SEGMENTS,
    PROTOCOL_ID,
    PROTOCOL_MAJOR_VERSION,
    SHM_BLOB_HEADER_LEN,
    CapabilityHandshake,
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
    RemoteOp,
    ShmBlobArena,
    ShmBlobArenaError,
    ShmBlobRef,
    Snapshot,
    WireStamp,
)
from .signal import Signal, signal, signal_def
from .slot import BaseSlot, Slot, slot, slot_def
from .state_machine import StateMachine
from .statechart import ChartDef, StateChart
from .types import LazilyCallable

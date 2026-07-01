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
    "SHM_BLOB_HEADER_LEN",
    "BaseSlot",
    "Cell",
    "CellSlot",
    "ChartDef",
    "Delta",
    "DeltaApplyStatus",
    "DeltaOp",
    "EdgeSnapshot",
    "IpcMessage",
    "IpcValue",
    "LazilyCallable",
    "NodeId",
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
    "cell",
    "cell_def",
    "ipc",
    "signal",
    "signal_def",
    "slot",
    "slot_def",
]
__version__ = "0.12.0"

from . import ipc
from .cell import Cell, CellSlot, cell, cell_def
from .ipc import (
    SHM_BLOB_HEADER_LEN,
    Delta,
    DeltaApplyStatus,
    DeltaOp,
    EdgeSnapshot,
    IpcMessage,
    IpcValue,
    NodeId,
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
)
from .signal import Signal, signal, signal_def
from .slot import BaseSlot, Slot, slot, slot_def
from .state_machine import StateMachine
from .statechart import ChartDef, StateChart
from .types import LazilyCallable

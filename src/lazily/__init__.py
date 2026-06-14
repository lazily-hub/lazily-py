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
    "BaseSlot",
    "Cell",
    "CellSlot",
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
    "ShmBlobRef",
    "Signal",
    "Slot",
    "Snapshot",
    "cell",
    "cell_def",
    "ipc",
    "signal",
    "signal_def",
    "slot",
    "slot_def",
]
__version__ = "0.11.0"

from . import ipc
from .cell import Cell, CellSlot, cell, cell_def
from .ipc import (
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
    ShmBlobRef,
    Snapshot,
)
from .signal import Signal, signal, signal_def
from .slot import BaseSlot, Slot, slot, slot_def
from .types import LazilyCallable

"""Lossless mergeable document-tree contract (``#lzcrdttree``)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


__all__ = ["CrdtTree"]


@runtime_checkable
class CrdtTree[VersionVectorT, DeltaT, ValueT](Protocol):
    """A lossless document CRDT with one identity-preserving delta format.

    Implementations make :meth:`merge_from` and delta application commutative,
    associative, and idempotent. ``delta_since(empty_frontier)`` is the snapshot,
    so full and incremental replication preserve the same operation identities.
    """

    def version_vector(self) -> VersionVectorT: ...

    def delta_since(self, version: VersionVectorT) -> DeltaT: ...

    def apply_delta(self, delta: DeltaT) -> bool: ...

    def text(self) -> str: ...

    def value(self) -> ValueT: ...

    def merge_from(self, other: CrdtTree[VersionVectorT, DeltaT, ValueT]) -> bool: ...

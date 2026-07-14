"""Stream windowing (``#lzwindow``) — Phase 5 of the realtime + distributed
primitives plan.

See ``lazily-spec/docs/windowing.md`` and the formal model
``lazily-formal/LazilyFormal/Windowing.lean``. Window aggregation *is* a merge,
so the :class:`~lazily.merge.MergePolicy` algebra (``Sum``/``Max``/``SetUnion``/
custom) composes: the aggregate of a window equals the associative fold of its
elements. Each primitive is a pure compute **core** (window bookkeeping plus a
``MergePolicy`` fold) split from a reactive **cell** projecting the last emitted
aggregate onto an internal :class:`~lazily.cell.Cell`. The cell's ``!=`` store
guard gives emit-only invalidation — a step that emits nothing leaves the output
reader cached.
"""

from __future__ import annotations


__all__ = [
    "SessionCell",
    "SessionCore",
    "SlidingCell",
    "SlidingCore",
    "TumblingCountCell",
    "TumblingCountCore",
    "TumblingTimeCell",
    "TumblingTimeCore",
]

from collections import deque
from typing import TYPE_CHECKING

from .cell import Cell


if TYPE_CHECKING:
    from collections.abc import Iterable

    from .merge import MergePolicy


def _merge_into[T](acc: T | None, v: T, policy: MergePolicy[T]) -> T:
    """Fold ``v`` into an optional accumulator under ``policy`` (identity when
    the accumulator is empty)."""
    if acc is None:
        return v
    return policy.merge(acc, v)


def _fold_window[T](items: Iterable[T], policy: MergePolicy[T]) -> T | None:
    """Fold an iterable of elements under ``policy`` (``None`` for an empty
    window)."""
    acc: T | None = None
    for v in items:
        acc = _merge_into(acc, v, policy)
    return acc


# ===========================================================================
# Tumbling (count)
# ===========================================================================


class TumblingCountCore[T]:
    """Count-based tumbling window compute core.

    Accumulates elements under ``policy``; on the ``n``-th push it emits the
    window fold and resets. Windows are fixed and non-overlapping.
    """

    __slots__ = ("_acc", "_count", "_n", "_policy")

    def __init__(self, n: int, policy: MergePolicy[T]) -> None:
        self._n = max(n, 1)
        self._policy = policy
        self._acc: T | None = None
        self._count = 0

    def push(self, v: T) -> T | None:
        """Push an element; emit the window aggregate on the ``n``-th and reset."""
        self._acc = _merge_into(self._acc, v, self._policy)
        self._count += 1
        if self._count >= self._n:
            self._count = 0
            emit = self._acc
            self._acc = None
            return emit
        return None


# ===========================================================================
# Tumbling (time)
# ===========================================================================


class TumblingTimeCore[T]:
    """Time-based tumbling window compute core.

    Elements accumulate into the current window via :meth:`push`; at each period
    boundary :meth:`tick` emits the window fold and opens the next window. An
    empty window emits ``None``.
    """

    __slots__ = ("_acc", "_next", "_period", "_policy")

    def __init__(self, period: int, policy: MergePolicy[T]) -> None:
        period = max(period, 1)
        self._period = period
        self._next = period
        self._policy = policy
        self._acc: T | None = None

    def push(self, now: int, v: T) -> None:
        """Accumulate an element into the current window (``now`` unused)."""
        self._acc = _merge_into(self._acc, v, self._policy)

    def tick(self, now: int) -> T | None:
        """At a period boundary emit the window aggregate (empty window →
        ``None``)."""
        if now < self._next:
            return None
        while self._next <= now:
            self._next += self._period
        emit = self._acc
        self._acc = None
        return emit


# ===========================================================================
# Sliding (count)
# ===========================================================================


class SlidingCore[T]:
    """Count-based sliding window compute core (fold-recompute, correct for any
    associative merge).

    Retains the last ``size`` elements; every ``slide`` pushes it emits the fold
    over the current (overlapping) window.
    """

    __slots__ = ("_buffer", "_policy", "_since", "_size", "_slide")

    def __init__(self, size: int, slide: int, policy: MergePolicy[T]) -> None:
        self._size = max(size, 1)
        self._slide = max(slide, 1)
        self._policy = policy
        self._buffer: deque[T] = deque()
        self._since = 0

    def push(self, v: T) -> T | None:
        """Push an element; every ``slide`` pushes emit the fold over the last
        ``size`` elements."""
        self._buffer.append(v)
        while len(self._buffer) > self._size:
            self._buffer.popleft()
        self._since += 1
        if self._since >= self._slide:
            self._since = 0
            return _fold_window(self._buffer, self._policy)
        return None


# ===========================================================================
# Session (gap-based)
# ===========================================================================


class SessionCore[T]:
    """Gap-based sessionization compute core.

    Consecutive elements within ``gap`` accumulate; an element arriving more than
    ``gap`` after the previous closes the open session (emitting its fold) and
    opens a new one. :meth:`flush` closes an idle-open session.
    """

    __slots__ = ("_acc", "_gap", "_last", "_policy")

    def __init__(self, gap: int, policy: MergePolicy[T]) -> None:
        self._gap = gap
        self._policy = policy
        self._acc: T | None = None
        self._last: int | None = None

    def _idle(self, now: int) -> bool:
        return (
            self._last is not None
            and max(now - self._last, 0) > self._gap
            and self._acc is not None
        )

    def push(self, now: int, v: T) -> T | None:
        """Push an element; a gap larger than ``gap`` closes the session
        (emitting its aggregate) and opens a new one."""
        if self._idle(now):
            emit = self._acc
            self._acc = v
            self._last = now
            return emit
        self._acc = _merge_into(self._acc, v, self._policy)
        self._last = now
        return None

    def flush(self, now: int) -> T | None:
        """Close the open session if it has been idle longer than ``gap``."""
        if self._idle(now):
            emit = self._acc
            self._acc = None
            return emit
        return None


# ===========================================================================
# Reactive cells
# ===========================================================================


class TumblingCountCell[T]:
    """Reactive count-tumbling window; projects the last emitted aggregate onto
    an internal :class:`~lazily.cell.Cell`. Emit-only invalidation."""

    __slots__ = ("_cell", "_core")

    def __init__(self, ctx: dict, n: int, policy: MergePolicy[T]) -> None:
        self._core: TumblingCountCore[T] = TumblingCountCore(n, policy)
        self._cell: Cell[T | None] = Cell(ctx, None)

    def push(self, v: T) -> T | None:
        emit = self._core.push(v)
        if emit is not None:
            self._cell.value = emit
        return emit

    def output(self) -> T | None:
        """Reactive read of the last emitted aggregate (``None`` before the first
        emit)."""
        return self._cell.value

    def output_cell(self) -> Cell[T | None]:
        """The underlying output cell (for wiring derived readers)."""
        return self._cell


class TumblingTimeCell[T]:
    """Reactive time-tumbling window (``push(now, v)`` + ``tick(now)``); projects
    the last emitted aggregate. Emit-only invalidation."""

    __slots__ = ("_cell", "_core")

    def __init__(self, ctx: dict, period: int, policy: MergePolicy[T]) -> None:
        self._core: TumblingTimeCore[T] = TumblingTimeCore(period, policy)
        self._cell: Cell[T | None] = Cell(ctx, None)

    def push(self, now: int, v: T) -> None:
        self._core.push(now, v)

    def tick(self, now: int) -> T | None:
        emit = self._core.tick(now)
        if emit is not None:
            self._cell.value = emit
        return emit

    def output(self) -> T | None:
        return self._cell.value

    def output_cell(self) -> Cell[T | None]:
        return self._cell


class SlidingCell[T]:
    """Reactive sliding window; projects the last emitted aggregate. Emit-only
    invalidation."""

    __slots__ = ("_cell", "_core")

    def __init__(
        self, ctx: dict, size: int, slide: int, policy: MergePolicy[T]
    ) -> None:
        self._core: SlidingCore[T] = SlidingCore(size, slide, policy)
        self._cell: Cell[T | None] = Cell(ctx, None)

    def push(self, v: T) -> T | None:
        emit = self._core.push(v)
        if emit is not None:
            self._cell.value = emit
        return emit

    def output(self) -> T | None:
        return self._cell.value

    def output_cell(self) -> Cell[T | None]:
        return self._cell


class SessionCell[T]:
    """Reactive session window (``push(now, v)`` + ``flush(now)``); projects the
    last emitted aggregate. Emit-only invalidation."""

    __slots__ = ("_cell", "_core")

    def __init__(self, ctx: dict, gap: int, policy: MergePolicy[T]) -> None:
        self._core: SessionCore[T] = SessionCore(gap, policy)
        self._cell: Cell[T | None] = Cell(ctx, None)

    def push(self, now: int, v: T) -> T | None:
        emit = self._core.push(now, v)
        if emit is not None:
            self._cell.value = emit
        return emit

    def flush(self, now: int) -> T | None:
        emit = self._core.flush(now)
        if emit is not None:
            self._cell.value = emit
        return emit

    def output(self) -> T | None:
        return self._cell.value

    def output_cell(self) -> Cell[T | None]:
        return self._cell

"""Reactive queue — ``QueueCell`` SPSC/MPSC + pluggable ``QueueStorage`` backend.

The Python counterpart of ``lazily-spec/cell-model.md`` § "Reactive queues" and
the Lean ``LazilyFormal.QueueCell`` formal model. A :class:`QueueCell` is a FIFO
collection composed of reactive cells — **not a new cell kind** — that adds
queue semantics (push to tail, pop from head) to the reactive graph.

It is specified as a **single-producer, single-consumer (SPSC)** primitive;
**MPSC** (multi-producer) is a *usage rule* on the same primitive — multiple
producers push inside a :func:`lazily.batch` boundary, and the batch serializes
the pushes into a deterministic order. There is no separate ``MPSCQueueCell``
type.

Shell vs storage
----------------

The reactive shell owns the reader-kind version cells (``head`` / ``len`` /
``is_empty`` / ``is_full`` / ``closed``) and the invalidation logic; it is
storage-agnostic. The storage backend owns the actual FIFO data structure and is
pluggable via the :class:`QueueStorage` protocol. The default
:class:`VecDequeStorage` is an unbounded deque; a bounded variant exposes
reactive backpressure via ``is_full``.

Reader-kind invalidation
------------------------

Invalidation is scoped to **reader kind**, not to individual positions. A push
invalidates ``len`` / ``is_empty`` readers (and ``head`` when transitioning from
empty, and ``is_full`` when transitioning to capacity); a pop invalidates
``head`` / ``len`` / ``is_empty`` readers (and ``is_full`` when transitioning off
capacity). The head reader observes the *current* head value — after a pop, the
head reader sees the next element (or ``None``), not a stale value.

This reader-kind independence is implemented for free by the existing ``!=``
(PartialEq) guard on :class:`lazily.cell.Cell`: after each op the shell re-derives
each reader-kind cell from the storage and writes it back, and a cell whose value
did not change is not invalidated.

Example
-------

>>> from lazily import QueueCell, batch
>>> ctx = {}
>>> q: QueueCell[str] = QueueCell(ctx)
>>> _ = q.try_push("a")
>>> _ = q.try_push("b")
>>> q.head()
'a'
>>> q.len()
2
>>> q.try_pop()
'a'
>>> q.try_pop()
'b'
>>> q.is_empty()
True
>>> # MPSC: multiple producers push inside one batch → one invalidation pass.
>>> batch(lambda: (q.try_push("p1"), q.try_push("p2"), q.try_push("p3")))
>>> q.len()
3
"""

from __future__ import annotations


__all__ = [
    "QueueCell",
    "QueuePopError",
    "QueuePushError",
    "QueueReaderHandles",
    "QueueStorage",
    "VecDequeStorage",
]

from collections import deque
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .batch import batch
from .cell import Cell


if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


class QueuePushError:
    """Failure reasons for :meth:`QueueStorage.try_push` / :meth:`QueueCell.try_push`.

    ``Full`` and ``Closed`` are the two observable rejection reasons
    distinguished by the shell's contract (``lazily-spec/cell-model.md`` §
    "Storage backend contract"). Neither changes queue state, so neither
    invalidates any reader.

    Used as singletons: ``QueuePushError.Full`` / ``QueuePushError.Closed``.
    """

    Full: QueuePushError
    Closed: QueuePushError

    __slots__ = ("label",)

    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> str:
        return f"QueuePushError.{self.label}"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, QueuePushError) and other.label == self.label

    def __hash__(self) -> int:
        return hash(("QueuePushError", self.label))


class QueuePopError:
    """Failure reasons for :meth:`QueueStorage.try_pop` / :meth:`QueueCell.try_pop`.

    ``Empty`` and ``Closed`` are distinct observable signals: ``Empty`` means
    "try again later," ``Closed`` means "the producer is done and the queue is
    drained."

    Used as singletons: ``QueuePopError.Empty`` / ``QueuePopError.Closed``.
    """

    Empty: QueuePopError
    Closed: QueuePopError

    __slots__ = ("label",)

    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> str:
        return f"QueuePopError.{self.label}"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, QueuePopError) and other.label == self.label

    def __hash__(self) -> int:
        return hash(("QueuePopError", self.label))


QueuePushError.Full = QueuePushError("Full")
QueuePushError.Closed = QueuePushError("Closed")
QueuePopError.Empty = QueuePopError("Empty")
QueuePopError.Closed = QueuePopError("Closed")


@runtime_checkable
class QueueStorage[T](Protocol):
    """Pluggable FIFO storage backend for a :class:`QueueCell`.

    The shell / storage split keeps the reactive shell storage-agnostic: the
    shell owns the reader-kind version cells and invalidation logic, the backend
    owns the actual FIFO data structure. The default backend is
    :class:`VecDequeStorage` (unbounded deque); future backends include a
    consensus-backed store or external-broker adapters.

    A conforming backend MUST:

    1. **FIFO order** — ``try_pop`` returns elements in ``try_push`` order.
    2. **Cardinality compatibility** — its native producer/consumer shape is a
       superset of the shell's required shape.
    3. **Bounded contract (optional)** — a bounded backend exposes
       :meth:`capacity` as a non-``None`` int and ``try_push`` returns
       :attr:`QueuePushError.Full` at capacity.
    4. **Position identity** — invalidation is phrased over reader kind, not
       storage indices.
    """

    def try_push(self, value: T) -> QueuePushError | None:
        """Append ``value`` to the tail. Returns ``Full`` if bounded and at
        capacity, or ``Closed`` if the queue is closed. Returns ``None`` on
        success. On error the queue state is unchanged."""
        ...

    def try_pop(self) -> Any:
        """Remove and return the head element. Returns :attr:`QueuePopError.Empty`
        if open and empty, or :attr:`QueuePopError.Closed` if closed and empty.
        Pop on a closed *non-empty* queue drains (returns the next element)."""
        ...

    def peek(self) -> Any:
        """Peek the current head element without removing it. ``None`` when
        empty. The shell reads this to materialize its ``head`` reader-kind
        cell."""
        ...

    def len(self) -> int:
        """Current number of buffered elements."""
        ...

    def capacity(self) -> int | None:
        """Bounded capacity, or ``None`` for an unbounded backend."""
        ...

    def is_closed(self) -> bool:
        """Whether the queue has been closed. Close is terminal."""
        ...

    def close(self) -> None:
        """Close the queue. Idempotent. After close, ``try_push`` returns
        ``Closed``; ``try_pop`` continues to drain and returns ``Closed`` only
        once empty."""
        ...


class VecDequeStorage[T]:
    """The reference :class:`QueueStorage` backend — a deque-backed FIFO,
    optionally bounded.

    The unbounded form (the default) is what :class:`QueueCell` uses; the bounded
    form (:meth:`with_capacity`) exposes reactive backpressure via the shell's
    ``is_full`` reader. The overflow policy is **reject** — ``try_push`` at
    capacity returns :attr:`QueuePushError.Full` (elements are never silently
    dropped); other backends may choose block / drop-oldest / drop-newest.
    """

    __slots__ = ("_capacity", "_closed", "_elements")

    def __init__(self, capacity: int | None = None) -> None:
        if capacity is not None and capacity <= 0:
            raise ValueError("VecDequeStorage capacity must be > 0")
        self._elements: deque[T] = deque()
        self._capacity = capacity
        self._closed = False

    @classmethod
    def unbounded(cls) -> VecDequeStorage[T]:
        """Create an unbounded storage (no capacity limit)."""
        return cls()

    @classmethod
    def with_capacity(cls, capacity: int) -> VecDequeStorage[T]:
        """Create a bounded storage that rejects pushes once it holds
        ``capacity`` elements."""
        return cls(capacity=capacity)

    def try_push(self, value: T) -> QueuePushError | None:
        if self._closed:
            return QueuePushError.Closed
        if self._capacity is not None and len(self._elements) >= self._capacity:
            return QueuePushError.Full
        self._elements.append(value)
        return None

    def try_pop(self) -> T | QueuePopError:
        if self._elements:
            return self._elements.popleft()
        return QueuePopError.Closed if self._closed else QueuePopError.Empty

    def peek(self) -> T | None:
        return self._elements[0] if self._elements else None

    def len(self) -> int:
        return len(self._elements)

    def capacity(self) -> int | None:
        return self._capacity

    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True

    def elements(self) -> list[T]:
        """Snapshot the buffered elements in FIFO order. Non-reactive — for
        snapshot/serde and conformance-fixture verification."""
        return list(self._elements)


class QueueReaderHandles[T]:
    """Handles to all five reader-kind cells of a :class:`QueueCell`, for effects
    that need to subscribe to several reader kinds at once."""

    __slots__ = ("closed", "head", "is_empty", "is_full", "len")

    def __init__(
        self,
        head: Cell[T | None],
        len: Cell[int],
        is_empty: Cell[bool],
        is_full: Cell[bool],
        closed: Cell[bool],
    ) -> None:
        self.head = head
        self.len = len
        self.is_empty = is_empty
        self.is_full = is_full
        self.closed = closed


class QueueCell[T]:
    """A reactive FIFO queue — SPSC primitive with an MPSC usage rule.

    The reactive shell wraps a pluggable :class:`QueueStorage` backend (default
    :class:`VecDequeStorage`); the shell owns the reader-kind version cells
    (``head`` / ``len`` / ``is_empty`` / ``is_full`` / ``closed``) and
    invalidates by reader kind — a push to a non-empty queue does NOT invalidate
    the ``head`` reader, a pop does.

    All reactive reads (``head`` / ``len`` / ``is_empty`` / ``is_full`` /
    ``closed``) subscribe to the corresponding reader-kind cell when read inside
    a :class:`~lazily.slot.Slot` / :class:`~lazily.signal.Signal` /
    :class:`~lazily.effect.Effect`, so derived computeds recompute exactly when
    their reader kind changes (reader-kind independence).
    """

    __slots__ = ("_closed", "_head", "_is_empty", "_is_full", "_len", "_storage", "ctx")

    ctx: dict

    def __init__(
        self,
        ctx: dict,
        *,
        capacity: int | None = None,
        storage: QueueStorage[T] | None = None,
    ) -> None:
        self.ctx = ctx
        self._storage: QueueStorage[T] = (
            storage if storage is not None else VecDequeStorage(capacity=capacity)
        )
        # Reader-kind version cells — initialized from the backend's current
        # state. The `!=` (PartialEq) guard on `Cell.value` means a cell whose
        # value does not change on a subsequent write is not invalidated — this
        # is what implements reader-kind independence for free.
        len_val = self._storage.len()
        cap = self._storage.capacity()
        is_full_val = cap is not None and len_val >= cap
        self._head: Cell[T | None] = Cell(ctx, self._storage.peek())
        self._len: Cell[int] = Cell(ctx, len_val)
        self._is_empty: Cell[bool] = Cell(ctx, len_val == 0)
        self._is_full: Cell[bool] = Cell(ctx, is_full_val)
        self._closed: Cell[bool] = Cell(ctx, self._storage.is_closed())

    @classmethod
    def with_capacity(cls, ctx: dict, capacity: int) -> QueueCell[T]:
        """Create a bounded queue with ``capacity``. Exposes reactive
        backpressure via :meth:`is_full`: a pop that transitions full → not-full
        invalidates ``is_full`` readers."""
        return cls(ctx, storage=VecDequeStorage.with_capacity(capacity))

    @classmethod
    def with_storage(cls, ctx: dict, storage: QueueStorage[T]) -> QueueCell[T]:
        """Build a queue over an arbitrary :class:`QueueStorage` backend. The
        shell initializes its reader-kind cells from the backend's current
        state."""
        return cls(ctx, storage=storage)

    def _sync_content(self) -> None:
        """Re-derive the reader-kind cells from storage and write them back, in
        one atomic invalidation pass (a :func:`~lazily.batch` groups the writes
        so an observer never sees a partial state). The ``!=`` guard on
        ``Cell.value`` suppresses invalidation for any cell whose value did not
        change — this is the reader-kind independence law. ``closed`` is
        intentionally NOT touched here: it only changes via :meth:`close`."""
        len_val = self._storage.len()
        cap = self._storage.capacity()
        head_val = self._storage.peek()
        is_empty_val = len_val == 0
        is_full_val = cap is not None and len_val >= cap

        def writes() -> None:
            self._head.value = head_val
            self._len.value = len_val
            self._is_empty.value = is_empty_val
            self._is_full.value = is_full_val

        batch(writes)

    # -- mutators ------------------------------------------------------- #

    def try_push(self, value: T) -> QueuePushError | None:
        """Append ``value`` to the tail of the queue.

        Returns :attr:`QueuePushError.Full` if bounded and at capacity (reject
        policy), or :attr:`QueuePushError.Closed` if the queue is closed. Returns
        ``None`` on success. On error the queue state is unchanged and no reader
        is invalidated.
        """
        result = self._storage.try_push(value)
        if result is None:
            self._sync_content()
        return result

    def try_pop(self) -> T | QueuePopError:
        """Remove and return the head element.

        Returns :attr:`QueuePopError.Empty` if open and empty, or
        :attr:`QueuePopError.Closed` if closed and empty. Pop on a closed
        *non-empty* queue drains (returns the next element).
        """
        result = self._storage.try_pop()
        if not isinstance(result, QueuePopError):
            self._sync_content()
        return result

    def close(self) -> None:
        """Close the queue. Idempotent — closing an already-closed queue is a
        no-op (no invalidation). Terminal — once closed, a queue cannot be
        reopened. Invalidates the ``closed`` reader only on the false → true
        transition."""
        if self._storage.is_closed():
            return
        self._storage.close()
        self._closed.value = True

    # -- reactive reader-kind reads ------------------------------------- #

    def head(self) -> T | None:
        """Reactive read of the current head value. ``None`` when the queue is
        empty. A reader is invalidated when the head value *changes* — every
        pop, and a push only when transitioning from empty."""
        return self._head.value

    def len(self) -> int:
        """Reactive read of the number of buffered elements. Invalidated
        whenever the count changes (every successful push/pop)."""
        return self._len.value

    def is_empty(self) -> bool:
        """Reactive emptiness check. Invalidated only on the empty ↔ non-empty
        transition."""
        return self._is_empty.value

    def is_full(self) -> bool:
        """Reactive fullness check (only meaningful when the backend is bounded).
        Invalidated on the full ↔ not-full transition — this is the backpressure
        signal. For an unbounded backend this is always ``False`` and never
        invalidates."""
        return self._is_full.value

    def is_closed(self) -> bool:
        """Reactive read of the closed flag. Invalidated only on the open →
        closed transition."""
        return self._closed.value

    # -- reader-kind cell handles (advanced wiring) --------------------- #

    def reader_handles(self) -> QueueReaderHandles[T]:
        """Handles to the reader-kind cells, for effects that subscribe to
        multiple reader kinds at once."""
        return QueueReaderHandles(
            head=self._head,
            len=self._len,
            is_empty=self._is_empty,
            is_full=self._is_full,
            closed=self._closed,
        )

    # -- non-reactive storage access ------------------------------------ #

    def capacity(self) -> int | None:
        """The backend's capacity, or ``None`` if unbounded."""
        return self._storage.capacity()

    def elements(self) -> list[T]:
        """Snapshot the buffered elements in FIFO order. Non-reactive — for
        debugging, snapshot/serde, and conformance-fixture verification. There
        is no reactive random-access ``queue[N]`` reader; per-position
        reactivity is the domain of :class:`~lazily.collection.CellMap`, not
        :class:`QueueCell`."""
        storage = self._storage
        getter: Callable[[], list[T]] | None = getattr(storage, "elements", None)
        if getter is not None:
            return getter()
        raise TypeError(
            f"storage backend {type(storage).__name__} does not expose a non-reactive "
            "`elements()` snapshot; use a VecDequeStorage-backed QueueCell for fixture "
            "verification"
        )

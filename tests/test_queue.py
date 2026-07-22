"""Direct unit tests for the reactive queue (``QueueCell``).

The cross-language conformance fixtures are replayed in
``test_queue_conformance.py``; this file covers the direct API surface, the
pluggable-storage adapter seam, and the reactive-backpressure effect wiring —
the spec's signature property.
"""

from __future__ import annotations

from collections import deque

from lazily import (
    QueueCell,
    QueuePopError,
    QueuePushError,
    QueueReaderHandles,
    QueueStorage,
    Slot,
    VecDequeStorage,
    batch,
    effect,
)


# ---------------------------------------------------------------------------
# SPSC FIFO
# ---------------------------------------------------------------------------


def test_spsc_fifo_basic() -> None:
    ctx: dict = {}
    q: QueueCell[int] = QueueCell(ctx)
    assert q.is_empty()
    assert q.head() is None

    assert q.try_push(1) is None
    assert q.try_push(2) is None
    assert q.try_push(3) is None

    assert q.len() == 3
    assert q.head() == 1
    assert q.elements() == [1, 2, 3]

    assert q.try_pop() == 1
    assert q.try_pop() == 2
    assert q.try_pop() == 3
    assert q.try_pop() == QueuePopError.Empty


def test_capacity_none_for_unbounded() -> None:
    q: QueueCell[int] = QueueCell({})
    assert q.capacity() is None
    assert q.is_full() is False  # unbounded is never full


# ---------------------------------------------------------------------------
# Bounded backpressure
# ---------------------------------------------------------------------------


def test_bounded_rejects_at_capacity() -> None:
    ctx: dict = {}
    q = QueueCell[int].with_capacity(ctx, 2)
    assert q.capacity() == 2
    assert not q.is_full()

    q.try_push(1)
    q.try_push(2)
    assert q.is_full()
    assert q.try_push(3) == QueuePushError.Full

    # pop frees a slot → is_full flips → reactive backpressure signal
    assert q.try_pop() == 1
    assert not q.is_full()
    q.try_push(3)
    assert q.is_full()


def test_vecdeque_storage_zero_capacity_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        VecDequeStorage(capacity=0)


# ---------------------------------------------------------------------------
# Closure lifecycle
# ---------------------------------------------------------------------------


def test_closure_lifecycle() -> None:
    ctx: dict = {}
    q: QueueCell[str] = QueueCell(ctx)
    q.try_push("a")
    q.try_push("b")

    q.close()
    assert q.is_closed()

    # push on closed is an error
    assert q.try_push("c") == QueuePushError.Closed

    # pop on closed+non-empty drains
    assert q.try_pop() == "a"
    assert q.try_pop() == "b"

    # pop on closed+empty returns Closed (distinct from Empty)
    assert q.try_pop() == QueuePopError.Closed

    # idempotent close — no-op
    q.close()
    assert q.is_closed()


# ---------------------------------------------------------------------------
# Reader-kind independence
# ---------------------------------------------------------------------------


def _reader(ctx: dict, fn) -> Slot:  # type: ignore[type-arg]
    @Slot
    def r(ctx):
        return fn()

    return r


def test_head_not_invalidated_on_push_to_nonempty() -> None:
    ctx: dict = {}
    q: QueueCell[int] = QueueCell(ctx)
    head_reader = _reader(ctx, q.head)

    assert head_reader(ctx) is None

    q.try_push(1)
    # push to empty changes head → invalidated, now Some(1)
    assert head_reader(ctx) == 1

    q.try_push(2)
    q.try_push(3)
    # head reader still cached (head unchanged) — reader-kind independence
    assert head_reader.is_in(ctx), "push to non-empty must not invalidate head reader"

    q.try_pop()
    # pop changes head → invalidated
    assert head_reader(ctx) == 2


def test_len_reader_invalidated_on_every_push_and_pop() -> None:
    ctx: dict = {}
    q: QueueCell[int] = QueueCell(ctx)
    len_reader = _reader(ctx, q.len)

    assert len_reader(ctx) == 0

    q.try_push(1)
    assert not len_reader.is_in(ctx)
    assert len_reader(ctx) == 1

    q.try_push(2)
    assert not len_reader.is_in(ctx)
    assert len_reader(ctx) == 2

    q.try_pop()
    assert not len_reader.is_in(ctx)
    assert len_reader(ctx) == 1


def test_is_empty_only_invalidated_on_transition() -> None:
    ctx: dict = {}
    q: QueueCell[int] = QueueCell(ctx)
    empty_reader = _reader(ctx, q.is_empty)

    assert empty_reader(ctx) is True

    q.try_push(1)
    # empty → non-empty transition: invalidated
    assert not empty_reader.is_in(ctx)
    assert empty_reader(ctx) is False

    q.try_push(2)
    # stays non-empty: NOT invalidated
    assert empty_reader.is_in(ctx)

    q.try_pop()
    # still non-empty: NOT invalidated
    assert empty_reader.is_in(ctx)

    q.try_pop()
    # non-empty → empty transition: invalidated
    assert not empty_reader.is_in(ctx)
    assert empty_reader(ctx) is True


def test_closed_only_invalidated_on_close() -> None:
    ctx: dict = {}
    q: QueueCell[int] = QueueCell(ctx)
    closed_reader = _reader(ctx, q.is_closed)

    assert closed_reader(ctx) is False

    q.try_push(1)
    q.try_pop()
    # push/pop do not touch closed
    assert closed_reader.is_in(ctx)

    q.close()
    assert not closed_reader.is_in(ctx)
    assert closed_reader(ctx) is True

    # idempotent close — no invalidation
    q.close()
    assert closed_reader.is_in(ctx)


def test_is_full_not_invalidated_on_unbounded() -> None:
    ctx: dict = {}
    q: QueueCell[int] = QueueCell(ctx)
    full_reader = _reader(ctx, q.is_full)

    assert full_reader(ctx) is False

    q.try_push(1)
    q.try_push(2)
    q.try_pop()
    # unbounded: is_full never changes → never invalidated
    assert full_reader.is_in(ctx)


# ---------------------------------------------------------------------------
# MPSC via batch
# ---------------------------------------------------------------------------


def test_mpsc_via_batch_is_one_invalidation_pass() -> None:
    ctx: dict = {}
    q: QueueCell[int] = QueueCell(ctx)
    len_reader = _reader(ctx, q.len)

    assert len_reader(ctx) == 0

    batch(lambda: (q.try_push(10), q.try_push(20), q.try_push(30)))

    # After the batch the len reader is invalidated exactly once and sees 3.
    assert not len_reader.is_in(ctx), (
        "batch should have invalidated the len reader once"
    )
    assert len_reader(ctx) == 3
    assert q.elements() == [10, 20, 30]


def test_per_producer_fifo_in_batch() -> None:
    ctx: dict = {}
    q: QueueCell[str] = QueueCell(ctx)

    # Two producers push inside separate batches.
    batch(lambda: (q.try_push("a1"), q.try_push("a2")))
    batch(lambda: (q.try_push("b1"), q.try_push("b2")))

    drained = [q.try_pop(), q.try_pop(), q.try_pop(), q.try_pop()]
    assert drained == ["a1", "a2", "b1", "b2"]


# ---------------------------------------------------------------------------
# Pluggable storage adapter seam
# ---------------------------------------------------------------------------


class _BoundedRing:
    """A minimal custom backend proving the QueueStorage adapter seam works."""

    def __init__(self, cap: int) -> None:
        self.buf: deque[int] = deque()
        self.cap = cap
        self.closed = False

    def try_push(self, value: int) -> QueuePushError | None:
        if self.closed:
            return QueuePushError.Closed
        if len(self.buf) >= self.cap:
            return QueuePushError.Full
        self.buf.append(value)
        return None

    def try_pop(self) -> int | QueuePopError:
        if self.buf:
            return self.buf.popleft()
        return QueuePopError.Closed if self.closed else QueuePopError.Empty

    def peek(self) -> int | None:
        return self.buf[0] if self.buf else None

    def len(self) -> int:
        return len(self.buf)

    def capacity(self) -> int | None:
        return self.cap

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


def test_pluggable_storage_via_protocol() -> None:
    ctx: dict = {}
    storage = _BoundedRing(2)
    # The duck-typed backend conforms to the QueueStorage protocol.
    assert isinstance(storage, QueueStorage)

    q = QueueCell[int].with_storage(ctx, storage)

    assert q.try_push(1) is None
    assert q.try_push(2) is None
    assert q.is_full()
    assert q.try_push(3) == QueuePushError.Full
    assert q.try_pop() == 1
    assert not q.is_full()
    assert q.len() == 1
    assert q.head() == 2


# ---------------------------------------------------------------------------
# Backpressure effect wiring (the spec's signature property)
# ---------------------------------------------------------------------------


def test_backpressure_pop_wakes_push_side_effect() -> None:
    ctx: dict = {}
    q = QueueCell[int].with_capacity(ctx, 1)

    log: list[tuple[bool, int]] = []

    @effect
    def push_side(ctx) -> None:
        full = q.is_full()
        n = q.len()
        log.append((full, n))

    push_side(ctx)
    # After effect setup, the initial sample is (False, 0).
    assert log == [(False, 0)]

    # Fill the queue → is_full flips → effect reruns and records (True, 1).
    q.try_push(1)
    assert log == [(False, 0), (True, 1)]

    # A consumer pop transitions full → not-full. The effect's is_full
    # subscription is invalidated (True → False) and the effect reruns without
    # polling — the reactive backpressure signal.
    q.try_pop()
    assert log == [(False, 0), (True, 1), (False, 0)]


# ---------------------------------------------------------------------------
# Reader handles
# ---------------------------------------------------------------------------


def test_reader_handles_expose_all_cells() -> None:
    ctx: dict = {}
    q: QueueCell[int] = QueueCell(ctx)
    handles: QueueReaderHandles[int] = q.reader_handles()
    # head/len/is_empty/is_full are demand-driven Slots (call with ctx);
    # closed stays a Cell (a direct input).
    assert handles.head(ctx) is None
    assert handles.len(ctx) == 0
    assert handles.is_empty(ctx) is True
    assert handles.is_full(ctx) is False
    assert handles.closed.value is False

    q.try_push(7)
    assert handles.head(ctx) == 7
    assert handles.len(ctx) == 1


# ---------------------------------------------------------------------------
# VecDequeStorage standalone
# ---------------------------------------------------------------------------


def test_vecdeque_storage_unbounded_and_bounded() -> None:
    u = VecDequeStorage.unbounded()
    assert u.capacity() is None
    assert u.try_push(1) is None
    assert u.try_push(2) is None
    assert u.len() == 2
    assert u.peek() == 1
    assert u.try_pop() == 1
    assert u.elements() == [2]

    b = VecDequeStorage[int].with_capacity(1)
    assert b.capacity() == 1
    assert b.try_push(1) is None
    assert b.try_push(2) == QueuePushError.Full
    b.close()
    assert b.is_closed()
    assert b.try_push(3) == QueuePushError.Closed
    assert b.try_pop() == 1
    assert b.try_pop() == QueuePopError.Closed


def test_vecdeque_storage_with_capacity_classmethod() -> None:
    s = VecDequeStorage.with_capacity(3)
    assert s.capacity() == 3
    s.try_push("x")
    assert s.elements() == ["x"]


# ---------------------------------------------------------------------------
# Minimal contract (Phase 0, #relaycell): a raw-channel-style backend that
# implements ONLY try_push / try_pop / len / is_closed / close — no peek, no
# capacity — is fully conforming; it just has no head/is_full reader.
# ---------------------------------------------------------------------------


class MinimalFifoStorage:
    """Raw-channel-style backend: only the five required methods."""

    def __init__(self) -> None:
        self._elements: deque = deque()
        self._closed = False

    def try_push(self, value):
        if self._closed:
            return QueuePushError.Closed
        self._elements.append(value)
        return None

    def try_pop(self):
        if self._elements:
            return self._elements.popleft()
        return QueuePopError.Closed if self._closed else QueuePopError.Empty

    def len(self) -> int:
        return len(self._elements)

    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True

    # NB: no peek(), no capacity().


def test_raw_channel_backend_conforms_to_minimal_contract() -> None:
    ctx: dict = {}
    q: QueueCell[int] = QueueCell(ctx, storage=MinimalFifoStorage())

    assert q.is_empty() is True
    assert q.try_push(1) is None
    assert q.try_push(2) is None
    assert q.len() == 2
    assert q.is_empty() is False

    # No peek capability → head is trivially None; no capacity → never full.
    assert q.head() is None
    assert q.is_full() is False
    assert q.capacity() is None

    # FIFO drain from try_pop alone.
    assert q.try_pop() == 1
    assert q.try_pop() == 2
    assert q.is_empty() is True

    # Closure lifecycle: Closed distinct from Empty; push-after-close rejected.
    q.close()
    assert q.is_closed() is True
    assert q.try_push(3) == QueuePushError.Closed
    assert q.try_pop() == QueuePopError.Closed


def test_raw_channel_reader_kinds_stay_reactive() -> None:
    ctx: dict = {}
    q: QueueCell[int] = QueueCell(ctx, storage=MinimalFifoStorage())

    log: list[int] = []

    @effect
    def len_watch(ctx) -> None:
        log.append(q.len())

    len_watch(ctx)
    assert log == [0]
    q.try_push(10)
    assert log == [0, 1]
    q.try_pop()
    assert log == [0, 1, 0]

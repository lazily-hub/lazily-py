"""Reactive competing-consumer work queue.

``WorkQueueCell`` is a process-local serialization point.  Distributed or
high-availability deployments must put a consensus-backed leader/adapter in
front of it; the class deliberately does not pretend that local mutation is a
distributed claim protocol.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from .batch import batch
from .slot import Slot


__all__ = [
    "WorkQueueCell",
    "WorkQueueDeadLetter",
    "WorkQueueDeadLetterReason",
    "WorkQueueDelivery",
    "WorkQueueItem",
    "WorkQueueReaderHandles",
]


@dataclass(frozen=True, slots=True)
class WorkQueueItem[T]:
    item_id: int
    value: T
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class WorkQueueDelivery[T]:
    delivery_id: int
    item_id: int
    value: T
    worker: str
    attempt: int
    deadline: int


class WorkQueueDeadLetterReason(StrEnum):
    NACK = "nack"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class WorkQueueDeadLetter[T]:
    item_id: int
    value: T
    attempts: int
    reason: WorkQueueDeadLetterReason


@dataclass(frozen=True, slots=True)
class WorkQueueReaderHandles:
    pending_len: Slot[dict, dict, int]
    is_empty: Slot[dict, dict, bool]
    in_flight_len: Slot[dict, dict, int]
    dead_letter_len: Slot[dict, dict, int]


class WorkQueueCell[T]:
    """FIFO work queue with exclusive leased deliveries and poison handling.

    Item ids remain stable across retries.  Each claim gets a fresh delivery
    id, so acknowledgements are scoped to the current delivery and worker.
    Failed deliveries requeue at the tail until ``max_deliveries`` is reached,
    after which the item moves to the dead-letter list.
    """

    __slots__ = (
        "_dead_letter_len",
        "_dead_letters",
        "_in_flight",
        "_in_flight_len",
        "_is_empty",
        "_next_delivery_id",
        "_next_item_id",
        "_pending",
        "_pending_len",
        "ctx",
        "max_deliveries",
        "visibility_timeout",
    )

    def __init__(
        self, ctx: dict, *, visibility_timeout: int, max_deliveries: int
    ) -> None:
        if visibility_timeout <= 0:
            raise ValueError("visibility_timeout must be positive")
        if max_deliveries < 1:
            raise ValueError("max_deliveries must be at least one")
        self.ctx = ctx
        self.visibility_timeout = visibility_timeout
        self.max_deliveries = max_deliveries
        self._pending: deque[WorkQueueItem[T]] = deque()
        self._in_flight: dict[int, WorkQueueDelivery[T]] = {}
        self._dead_letters: list[WorkQueueDeadLetter[T]] = []
        self._next_item_id = 0
        self._next_delivery_id = 0
        self._pending_len: Slot[dict, dict, int] = Slot(
            callable=lambda _ctx: len(self._pending)
        )
        self._is_empty: Slot[dict, dict, bool] = Slot(
            callable=lambda _ctx: not self._pending
        )
        self._in_flight_len: Slot[dict, dict, int] = Slot(
            callable=lambda _ctx: len(self._in_flight)
        )
        self._dead_letter_len: Slot[dict, dict, int] = Slot(
            callable=lambda _ctx: len(self._dead_letters)
        )

    def _invalidate(
        self,
        *,
        pending_before: int,
        in_flight_before: int,
        dead_letter_before: int,
    ) -> None:
        pending_after = len(self._pending)
        in_flight_after = len(self._in_flight)
        dead_letter_after = len(self._dead_letters)

        def resets() -> None:
            if pending_before != pending_after:
                self._pending_len.reset(self.ctx)
            if (pending_before == 0) != (pending_after == 0):
                self._is_empty.reset(self.ctx)
            if in_flight_before != in_flight_after:
                self._in_flight_len.reset(self.ctx)
            if dead_letter_before != dead_letter_after:
                self._dead_letter_len.reset(self.ctx)

        batch(resets)

    def _counts(self) -> tuple[int, int, int]:
        return (len(self._pending), len(self._in_flight), len(self._dead_letters))

    def push(self, value: T) -> int:
        before = self._counts()
        item_id = self._next_item_id
        self._next_item_id += 1
        self._pending.append(WorkQueueItem(item_id, value))
        self._invalidate(
            pending_before=before[0],
            in_flight_before=before[1],
            dead_letter_before=before[2],
        )
        return item_id

    def claim(self, worker: str, now: int) -> WorkQueueDelivery[T] | None:
        if now < 0:
            raise ValueError("now must be non-negative")
        if not self._pending:
            return None
        before = self._counts()
        item = self._pending.popleft()
        delivery_id = self._next_delivery_id
        self._next_delivery_id += 1
        delivery = WorkQueueDelivery(
            delivery_id=delivery_id,
            item_id=item.item_id,
            value=item.value,
            worker=worker,
            attempt=item.attempts + 1,
            deadline=now + self.visibility_timeout,
        )
        self._in_flight[delivery_id] = delivery
        self._invalidate(
            pending_before=before[0],
            in_flight_before=before[1],
            dead_letter_before=before[2],
        )
        return delivery

    def ack(self, worker: str, delivery_id: int) -> bool:
        delivery = self._in_flight.get(delivery_id)
        if delivery is None or delivery.worker != worker:
            return False
        before = self._counts()
        del self._in_flight[delivery_id]
        self._invalidate(
            pending_before=before[0],
            in_flight_before=before[1],
            dead_letter_before=before[2],
        )
        return True

    def _fail(
        self, delivery: WorkQueueDelivery[T], reason: WorkQueueDeadLetterReason
    ) -> None:
        if delivery.attempt >= self.max_deliveries:
            self._dead_letters.append(
                WorkQueueDeadLetter(
                    delivery.item_id,
                    delivery.value,
                    delivery.attempt,
                    reason,
                )
            )
        else:
            self._pending.append(
                WorkQueueItem(delivery.item_id, delivery.value, delivery.attempt)
            )

    def nack(self, worker: str, delivery_id: int) -> bool:
        delivery = self._in_flight.get(delivery_id)
        if delivery is None or delivery.worker != worker:
            return False
        before = self._counts()
        del self._in_flight[delivery_id]
        self._fail(delivery, WorkQueueDeadLetterReason.NACK)
        self._invalidate(
            pending_before=before[0],
            in_flight_before=before[1],
            dead_letter_before=before[2],
        )
        return True

    def reap_expired(self, now: int) -> int:
        if now < 0:
            raise ValueError("now must be non-negative")
        expired = sorted(
            delivery_id
            for delivery_id, delivery in self._in_flight.items()
            if delivery.deadline < now
        )
        if not expired:
            return 0
        before = self._counts()
        for delivery_id in expired:
            delivery = self._in_flight.pop(delivery_id)
            self._fail(delivery, WorkQueueDeadLetterReason.EXPIRED)
        self._invalidate(
            pending_before=before[0],
            in_flight_before=before[1],
            dead_letter_before=before[2],
        )
        return len(expired)

    def pending_len(self) -> int:
        return self._pending_len(self.ctx)

    def is_empty(self) -> bool:
        return self._is_empty(self.ctx)

    def in_flight_len(self) -> int:
        return self._in_flight_len(self.ctx)

    def dead_letter_len(self) -> int:
        return self._dead_letter_len(self.ctx)

    def reader_handles(self) -> WorkQueueReaderHandles:
        return WorkQueueReaderHandles(
            self._pending_len,
            self._is_empty,
            self._in_flight_len,
            self._dead_letter_len,
        )

    def pending_items(self) -> list[WorkQueueItem[T]]:
        return list(self._pending)

    def in_flight_deliveries(self) -> list[WorkQueueDelivery[T]]:
        return sorted(
            self._in_flight.values(), key=lambda delivery: delivery.delivery_id
        )

    def dead_letter_items(self) -> list[WorkQueueDeadLetter[T]]:
        return list(self._dead_letters)

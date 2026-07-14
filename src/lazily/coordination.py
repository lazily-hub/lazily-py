"""Distributed coordination primitives (``#lzcoord``).

The Python counterpart of ``lazily-rs/src/coordination.rs`` — see
``lazily-spec/docs/coordination.md`` and the formal model
``lazily-formal/LazilyFormal/Coordination.lean``. Lease / leader / lock /
semaphore / barrier + quorum primitives, each a pure compute **core** (a state
machine over integers / peer ids) split from a reactive **cell** projecting the
salient reader onto a :class:`~lazily.cell.Cell`. Time is the logical clock;
``expiry`` is a tick value the runtime drives.

Each cell holds one internal :class:`~lazily.cell.Cell` per asserted reader. On
every op the cell recomputes the reader and assigns it to that ``Cell``; the
cell's ``!=`` (PartialEq) guard drives invalidation, so a reader invalidates
only when its projected value actually changes.
"""

from __future__ import annotations


__all__ = [
    "BarrierCell",
    "BarrierCore",
    "LeaderCell",
    "LeaderRole",
    "LeaseCell",
    "LeaseCore",
    "LockCell",
    "SemaphoreCell",
    "SemaphoreCore",
]

from enum import Enum

from .cell import Cell


# ===========================================================================
# Lease + fencing token
# ===========================================================================


class LeaseCore[P]:
    """Single-writer lease authority with a monotone fencing token.

    A grant on a free/expired lease increments the fence; a renew by the current
    holder keeps the same fence; a lease held by another peer rejects.
    """

    __slots__ = ("_expiry", "_fence", "_holder")

    def __init__(self) -> None:
        self._holder: P | None = None
        self._expiry: int = 0
        self._fence: int = 0

    def _is_expired(self, now: int) -> bool:
        return self._holder is not None and now >= self._expiry

    def is_held(self, now: int) -> bool:
        """Whether the lease is currently held (and not expired at ``now``)."""
        return self._holder is not None and not self._is_expired(now)

    def holder(self, now: int) -> P | None:
        """The live holder at ``now`` (``None`` once free or expired)."""
        return self._holder if self.is_held(now) else None

    def fence(self) -> int:
        return self._fence

    def acquire(self, peer: P, now: int, ttl: int) -> int | None:
        """Grant if free/expired (a new grant increments ``fence``); a renew by
        the holder keeps the same fence; held by another → ``None``."""
        free = self._holder is None or self._is_expired(now)
        if free:
            self._fence += 1
            self._holder = peer
            self._expiry = now + ttl
            return self._fence
        if self._holder == peer:
            self._expiry = now + ttl  # renew keeps fence
            return self._fence
        return None

    def renew(self, peer: P, now: int, ttl: int) -> bool:
        """Extend the expiry if ``peer`` is the live holder."""
        if self.is_held(now) and self._holder == peer:
            self._expiry = now + ttl
            return True
        return False

    def release(self, peer: P) -> None:
        """Drop the grant if ``peer`` holds it."""
        if self._holder == peer:
            self._holder = None

    def tick(self, now: int) -> bool:
        """Expire the grant when ``now >= expiry``; returns the expiry edge."""
        if self._is_expired(now):
            self._holder = None
            return True
        return False


class LeaseCell[P]:
    """Reactive lease: projects the holder onto a :class:`~lazily.cell.Cell`
    (invalidates only on a holder change)."""

    __slots__ = ("_core", "_holder", "ctx")

    def __init__(self, ctx: dict) -> None:
        self.ctx = ctx
        self._core: LeaseCore[P] = LeaseCore()
        self._holder: Cell[P | None] = Cell(ctx, None)

    def _refresh(self, now: int) -> None:
        self._holder.value = self._core.holder(now)

    def acquire(self, peer: P, now: int, ttl: int) -> int | None:
        r = self._core.acquire(peer, now, ttl)
        self._refresh(now)
        return r

    def renew(self, peer: P, now: int, ttl: int) -> bool:
        r = self._core.renew(peer, now, ttl)
        self._refresh(now)
        return r

    def release(self, peer: P, now: int) -> None:
        self._core.release(peer)
        self._refresh(now)

    def tick(self, now: int) -> bool:
        r = self._core.tick(now)
        self._refresh(now)
        return r

    def holder(self, now: int) -> P | None:
        return self._core.holder(now)

    def is_held(self, now: int) -> bool:
        return self._core.is_held(now)

    def fence(self) -> int:
        return self._core.fence()

    def holder_cell(self) -> Cell[P | None]:
        return self._holder


# ===========================================================================
# Leader / follower / candidate
# ===========================================================================


class LeaderRole(Enum):
    """The local node's role, derived from lease ownership."""

    Leader = "Leader"
    Follower = "Follower"
    Candidate = "Candidate"


class LeaderCell[P]:
    """Reactive leadership over a lease from node ``me``'s perspective."""

    __slots__ = ("_core", "_current_leader", "_me", "ctx")

    def __init__(self, ctx: dict, me: P) -> None:
        self.ctx = ctx
        self._core: LeaseCore[P] = LeaseCore()
        self._me: P = me
        self._current_leader: Cell[P | None] = Cell(ctx, None)

    def _refresh(self, now: int) -> None:
        self._current_leader.value = self._core.holder(now)

    def campaign(self, now: int, ttl: int) -> LeaderRole:
        """Try to acquire leadership for ``me``."""
        self._core.acquire(self._me, now, ttl)
        self._refresh(now)
        return self.role(now)

    def contend(self, peer: P, now: int, ttl: int) -> LeaderRole:
        """Simulate another peer contending (for tests / co-hosted nodes)."""
        self._core.acquire(peer, now, ttl)
        self._refresh(now)
        return self.role(now)

    def tick(self, now: int) -> LeaderRole:
        self._core.tick(now)
        self._refresh(now)
        return self.role(now)

    def current_leader(self, now: int) -> P | None:
        return self._core.holder(now)

    def role(self, now: int) -> LeaderRole:
        h = self._core.holder(now)
        if h is None:
            return LeaderRole.Candidate
        if h == self._me:
            return LeaderRole.Leader
        return LeaderRole.Follower

    def current_leader_cell(self) -> Cell[P | None]:
        return self._current_leader


# ===========================================================================
# Distributed lock + fencing
# ===========================================================================


class LockCell[P]:
    """Reactive distributed mutex over a lease + fencing token."""

    __slots__ = ("_core", "_is_locked", "ctx")

    def __init__(self, ctx: dict) -> None:
        self.ctx = ctx
        self._core: LeaseCore[P] = LeaseCore()
        self._is_locked: Cell[bool] = Cell(ctx, False)

    def _refresh(self, now: int) -> None:
        self._is_locked.value = self._core.is_held(now)

    def acquire(self, peer: P, now: int, ttl: int) -> int | None:
        """Acquire the lock, returning a fencing token, or ``None`` if held."""
        r = self._core.acquire(peer, now, ttl)
        self._refresh(now)
        return r

    def release(self, peer: P, now: int) -> None:
        self._core.release(peer)
        self._refresh(now)

    def tick(self, now: int) -> bool:
        r = self._core.tick(now)
        self._refresh(now)
        return r

    def validate(self, fence: int) -> bool:
        """Whether ``fence`` is the current (non-stale) fencing token."""
        return self._core.fence() == fence

    def is_locked(self, now: int) -> bool:
        return self._core.is_held(now)

    def fence(self) -> int:
        return self._core.fence()

    def is_locked_cell(self) -> Cell[bool]:
        return self._is_locked


# ===========================================================================
# Semaphore
# ===========================================================================


class SemaphoreCore:
    """Bounded permit pool compute core."""

    __slots__ = ("_acquired", "_capacity")

    def __init__(self, capacity: int) -> None:
        self._capacity: int = capacity
        self._acquired: int = 0

    def available(self) -> int:
        return self._capacity - self._acquired

    def acquire(self) -> bool:
        if self._acquired < self._capacity:
            self._acquired += 1
            return True
        return False

    def release(self) -> None:
        if self._acquired > 0:
            self._acquired -= 1


class SemaphoreCell:
    """Reactive semaphore: projects ``permits_available`` onto a
    :class:`~lazily.cell.Cell`."""

    __slots__ = ("_available", "_core", "ctx")

    def __init__(self, ctx: dict, capacity: int) -> None:
        self.ctx = ctx
        self._core = SemaphoreCore(capacity)
        self._available: Cell[int] = Cell(ctx, capacity)

    def _refresh(self) -> None:
        self._available.value = self._core.available()

    def acquire(self) -> bool:
        r = self._core.acquire()
        self._refresh()
        return r

    def release(self) -> None:
        self._core.release()
        self._refresh()

    def permits_available(self) -> int:
        return self._available.value

    def permits_available_cell(self) -> Cell[int]:
        return self._available


# ===========================================================================
# Barrier / quorum
# ===========================================================================


class BarrierCore[P]:
    """Wait-for-N gate compute core over distinct arriving peers."""

    __slots__ = ("_arrived", "_required")

    def __init__(self, required: int) -> None:
        self._required: int = required
        self._arrived: set[P] = set()

    def arrive(self, peer: P) -> bool:
        """Register a distinct arrival; returns whether the gate is open after."""
        self._arrived.add(peer)
        return self.is_open()

    def count(self) -> int:
        return len(self._arrived)

    def is_open(self) -> bool:
        return self.count() >= self._required


class BarrierCell[P]:
    """Reactive wait-for-N gate. A quorum gate is a barrier with
    ``required = total // 2 + 1``."""

    __slots__ = ("_core", "_is_open", "ctx")

    def __init__(self, ctx: dict, required: int) -> None:
        self.ctx = ctx
        self._core: BarrierCore[P] = BarrierCore(required)
        self._is_open: Cell[bool] = Cell(ctx, self._core.is_open())

    @classmethod
    def quorum(cls, ctx: dict, total: int) -> BarrierCell[P]:
        """A quorum gate: opens at strict majority of ``total``."""
        return cls(ctx, total // 2 + 1)

    def _refresh(self) -> None:
        self._is_open.value = self._core.is_open()

    def arrive(self, peer: P) -> bool:
        """Register an arrival / vote; returns whether the gate is open after."""
        r = self._core.arrive(peer)
        self._refresh()
        return r

    def count(self) -> int:
        return self._core.count()

    def is_open(self) -> bool:
        return self._is_open.value

    def is_open_cell(self) -> Cell[bool]:
        return self._is_open

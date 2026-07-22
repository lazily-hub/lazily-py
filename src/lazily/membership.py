"""Membership + failure detection (``#lzmemb``).

The Python counterpart of ``lazily-rs``'s ``src/membership.rs`` — see
``lazily-spec/docs/membership.md`` and the formal model
``lazily-formal/LazilyFormal/Membership.lean``. A :class:`MembershipCell` is a
reactive view of the live peer set, backed by SWIM-style heartbeats + a
**Phi-accrual** failure detector. Per-peer state is ``Alive | Suspect | Dead |
Left``; the derived alive :meth:`~MembershipCell.peer_set` is the ``Alive``
peers.

The pure compute **core** (:class:`MembershipCore` + :class:`PhiAccrual`) is the
Phi-accrual math + SWIM state machine over plain state; the reactive cell
projects the alive set onto a :class:`~lazily.cell.Cell` so ``peer_set``
invalidates only when the set changes. The peer id is generic (any
:class:`~collections.abc.Hashable`).

The phi formula (the Akka-style logistic approximation of the normal CDF) is
byte-identical to the Rust source so the fixture's Alive→Suspect→Dead
transitions match across bindings.
"""

from __future__ import annotations


__all__ = [
    "MembershipCell",
    "MembershipConfig",
    "MembershipCore",
    "PeerChangeEvent",
    "PeerSet",
    "PeerState",
    "PhiAccrual",
]

import math
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from .cell import Cell


if TYPE_CHECKING:
    from collections.abc import Hashable


class PeerState(Enum):
    """Per-peer liveness state (SWIM). The value is the canonical wire name."""

    #: Heartbeats current; a valid CRDT sync target.
    ALIVE = "Alive"
    #: Phi crossed the threshold; awaiting a refuting heartbeat or the timeout.
    SUSPECT = "Suspect"
    #: Suspect long enough to declare failed.
    DEAD = "Dead"
    #: Gracefully departed.
    LEFT = "Left"


@dataclass(frozen=True)
class PeerChangeEvent:
    """A diff event over the membership cell.

    ``kind`` is one of ``"joined"`` / ``"left"`` / ``"state_changed"``. For a
    ``"state_changed"`` event both :attr:`from_state` and :attr:`to_state` are
    set; the other kinds leave them ``None``.
    """

    kind: str
    peer: Hashable
    from_state: PeerState | None = None
    to_state: PeerState | None = None

    @classmethod
    def joined(cls, peer: Hashable) -> PeerChangeEvent:
        """A newly-seen peer entered the membership as ``Alive``."""
        return cls("joined", peer)

    @classmethod
    def left(cls, peer: Hashable) -> PeerChangeEvent:
        """A peer gracefully departed."""
        return cls("left", peer)

    @classmethod
    def state_changed(
        cls, peer: Hashable, from_state: PeerState, to_state: PeerState
    ) -> PeerChangeEvent:
        """A known peer transitioned between two liveness states."""
        return cls("state_changed", peer, from_state, to_state)


@dataclass
class MembershipConfig:
    """Tunables for the failure detector + SWIM state machine."""

    #: ``phi > phi_threshold`` marks a peer ``Suspect``.
    phi_threshold: float = 8.0
    #: Ticks a peer stays ``Suspect`` before being declared ``Dead``.
    suspect_timeout: int = 5
    #: Sliding window size for heartbeat inter-arrival samples.
    max_samples: int = 100
    #: Floor on the sample standard deviation (avoids div-by-zero).
    min_std: float = 0.1


def _log10(x: float) -> float:
    """``log10`` matching Rust ``f64::log10`` on the domain edges: ``log10(0) =
    -inf`` and ``log10(x < 0) = NaN`` (Python's :func:`math.log10` raises)."""
    if x > 0.0:
        return math.log10(x)
    if x == 0.0:
        return -math.inf
    return math.nan


class PhiAccrual:
    """Phi-accrual failure detector over a sliding window of heartbeat
    inter-arrival times. ``phi`` is bit-portable across bindings via the
    Akka-style logistic approximation of the normal CDF."""

    __slots__ = ("_last_heartbeat", "_max_samples", "_min_std", "_window")

    def __init__(self, max_samples: int, min_std: float) -> None:
        self._max_samples = max(max_samples, 1)
        self._min_std = min_std
        self._window: deque[float] = deque(maxlen=self._max_samples)
        self._last_heartbeat: int | None = None

    def heartbeat(self, now: int) -> None:
        """Record a heartbeat arrival, appending its inter-arrival sample."""
        if self._last_heartbeat is not None:
            interval = float(max(0, now - self._last_heartbeat))
            # ``deque(maxlen=...)`` drops the front sample once full — the
            # sliding-window equivalent of Rust's ``pop_front`` loop.
            self._window.append(interval)
        self._last_heartbeat = now

    def _mean(self) -> float:
        n = float(len(self._window))
        return sum(self._window) / n

    def _std(self, mean: float) -> float:
        n = float(len(self._window))
        var = sum((x - mean) * (x - mean) for x in self._window) / n
        return max(math.sqrt(var), self._min_std)

    def phi(self, now: int) -> float:
        """The suspicion level at ``now``. ``0.0`` when there is no estimate
        yet."""
        last = self._last_heartbeat
        if last is None:
            return 0.0
        if not self._window:
            return 0.0
        elapsed = float(max(0, now - last))
        mean = self._mean()
        std = self._std(mean)
        y = (elapsed - mean) / std
        try:
            e = math.exp(-y * (1.5976 + 0.070566 * y * y))
        except OverflowError:
            # Rust ``f64::exp`` saturates to +inf rather than raising.
            e = math.inf
        if elapsed > mean:
            return -_log10(e / (1.0 + e))
        return -_log10(1.0 - 1.0 / (1.0 + e))


@dataclass
class _PeerRecord:
    state: PeerState
    detector: PhiAccrual
    suspect_since: int | None = None


class MembershipCore:
    """The pure membership compute core: the SWIM state machine over a keyed
    peer map, driven by heartbeats and a logical clock. Emits
    :class:`PeerChangeEvent`\\ s."""

    __slots__ = ("_config", "_peers")

    def __init__(self, config: MembershipConfig | None = None) -> None:
        self._config = config if config is not None else MembershipConfig()
        self._peers: dict[Hashable, _PeerRecord] = {}

    def _new_detector(self) -> PhiAccrual:
        return PhiAccrual(self._config.max_samples, self._config.min_std)

    def alive_set(self) -> set[Hashable]:
        """The current alive peer set (the reactive peer set)."""
        return {
            peer
            for peer, record in self._peers.items()
            if record.state is PeerState.ALIVE
        }

    def state(self, peer: Hashable) -> PeerState | None:
        """The state of a known peer, or ``None`` if unknown."""
        record = self._peers.get(peer)
        return record.state if record is not None else None

    def join(self, peer: Hashable, now: int) -> list[PeerChangeEvent]:
        """Join a peer (or refresh a re-joining one): ``Alive`` with a fresh
        detector."""
        detector = self._new_detector()
        detector.heartbeat(now)
        known = peer in self._peers
        prev = self._peers[peer].state if known else None
        self._peers[peer] = _PeerRecord(PeerState.ALIVE, detector, None)
        if not known:
            return [PeerChangeEvent.joined(peer)]
        if prev is PeerState.ALIVE:
            return []
        if prev is not None:
            return [PeerChangeEvent.state_changed(peer, prev, PeerState.ALIVE)]
        return []

    def heartbeat(self, peer: Hashable, now: int) -> list[PeerChangeEvent]:
        """Record a heartbeat. An unknown peer is a join; a ``Suspect`` / ``Dead``
        peer returns to ``Alive`` (SWIM refutation)."""
        record = self._peers.get(peer)
        if record is None:
            return self.join(peer, now)
        record.detector.heartbeat(now)
        from_state = record.state
        if from_state is not PeerState.ALIVE and from_state is not PeerState.LEFT:
            record.state = PeerState.ALIVE
            record.suspect_since = None
            return [PeerChangeEvent.state_changed(peer, from_state, PeerState.ALIVE)]
        return []

    def leave(self, peer: Hashable, now: int) -> list[PeerChangeEvent]:
        """Graceful departure."""
        record = self._peers.get(peer)
        if record is None:
            return []
        if record.state is PeerState.LEFT:
            return []
        record.state = PeerState.LEFT
        record.suspect_since = None
        return [PeerChangeEvent.left(peer)]

    def tick(self, now: int) -> list[PeerChangeEvent]:
        """Advance the clock: escalate ``Alive → Suspect`` (phi crossed) and
        ``Suspect → Dead`` (timeout elapsed)."""
        threshold = self._config.phi_threshold
        timeout = self._config.suspect_timeout
        events: list[PeerChangeEvent] = []
        for peer, record in self._peers.items():
            if record.state is PeerState.ALIVE:
                if record.detector.phi(now) > threshold:
                    record.state = PeerState.SUSPECT
                    record.suspect_since = now
                    events.append(
                        PeerChangeEvent.state_changed(
                            peer, PeerState.ALIVE, PeerState.SUSPECT
                        )
                    )
            elif record.state is PeerState.SUSPECT:
                since = record.suspect_since
                expired = since is not None and max(0, now - since) >= timeout
                if expired:
                    record.state = PeerState.DEAD
                    events.append(
                        PeerChangeEvent.state_changed(
                            peer, PeerState.SUSPECT, PeerState.DEAD
                        )
                    )
            # Dead / Left: terminal, nothing to escalate.
        return events


class MembershipCell:
    """Reactive membership: drives a :class:`MembershipCore` and projects the
    alive set onto a :class:`~lazily.cell.Cell` so :meth:`peer_set` invalidates
    only on a set change.

    The backing cell holds a ``frozenset`` so the cell's ``!=`` (PartialEq)
    guard reflects actual set-content change — a heartbeat that keeps the same
    alive set produces no invalidation wave.
    """

    __slots__ = ("_core", "_ctx", "_peer_set")

    def __init__(self, ctx: dict, config: MembershipConfig | None = None) -> None:
        self._ctx = ctx
        self._core = MembershipCore(config)
        self._peer_set: Cell[frozenset[Hashable]] = Cell(ctx, frozenset())

    def _refresh(self) -> None:
        # Assign a canonical comparable value; the Cell only touches (and thus
        # invalidates readers) when the frozenset actually changes.
        self._peer_set.value = frozenset(self._core.alive_set())

    def join(self, peer: Hashable, now: int) -> list[PeerChangeEvent]:
        events = self._core.join(peer, now)
        self._refresh()
        return events

    def heartbeat(self, peer: Hashable, now: int) -> list[PeerChangeEvent]:
        events = self._core.heartbeat(peer, now)
        self._refresh()
        return events

    def leave(self, peer: Hashable, now: int) -> list[PeerChangeEvent]:
        events = self._core.leave(peer, now)
        self._refresh()
        return events

    def tick(self, now: int) -> list[PeerChangeEvent]:
        events = self._core.tick(now)
        self._refresh()
        return events

    def peer_set(self) -> set[Hashable]:
        """The reactive alive peer set. Reading this inside a
        :class:`~lazily.slot.Slot` / :class:`~lazily.signal.Computed` /
        :class:`~lazily.effect.Effect` subscribes it to the backing cell, so the
        reader is invalidated exactly when the alive set changes."""
        return set(self._peer_set.value)

    def peer_set_cell(self) -> Cell[frozenset[Hashable]]:
        """The backing peer-set cell, for direct subscription."""
        return self._peer_set

    def state(self, peer: Hashable) -> PeerState | None:
        """The state of a known peer, or ``None`` if unknown."""
        return self._core.state(peer)


#: The derived reactive alive-peer set — a ``Cell`` handle exposed by
#: :meth:`MembershipCell.peer_set_cell`.
PeerSet = Cell

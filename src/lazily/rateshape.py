"""Rate-shaping source operators (``#lzrateshape``).

Phase 0 of the realtime + distributed primitives plan. See
``lazily-spec/docs/rate-shaping.md``. Debounce / throttle / time-sampling already
exist algorithmically inside the relay plane — trapped behind
:class:`~lazily.relay.RelayCell` egress as :class:`~lazily.relay.WindowPolicy` /
:class:`~lazily.relay.ExpiryPolicy` / :class:`~lazily.relay.RatePolicy`. This
module is the standalone home for four **source operators** so any reactive value
source can be rate-shaped, not just a relay. (The three relay policies stay in
:mod:`lazily.relay`; this module does NOT redefine them.)

Each operator is a pure compute **core** — the emit/drop decision over plain
state — split from a thin reactive **cell** that projects the emitted value onto
a :class:`~lazily.cell.Cell` holding ``Optional[T]`` so a dropped/held input never
invalidates dependents (emit-only invalidation, via the ``Cell`` ``!=`` guard).
Time is the same monotone logical clock as ``#lztime``.
"""

from __future__ import annotations


__all__ = [
    "DebounceCell",
    "DebounceCore",
    "Lcg",
    "ProbabilisticSampleCell",
    "ProbabilisticSampleCore",
    "SampleCell",
    "SampleCore",
    "SampleMode",
    "SampleRng",
    "ThrottleCell",
    "ThrottleCore",
    "ThrottleEdge",
]

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Protocol, runtime_checkable

from .cell import Cell


# ---------------------------------------------------------------------------
# Shared reactive-cell projection.
# ---------------------------------------------------------------------------


def _set_output[T](cell: Cell[T | None], emitted: T | None) -> None:
    """Project an emit onto the output cell. A dropped/held input (``None``)
    never touches the cell, so it never invalidates dependents; a real emit sets
    the cell, whose ``!=`` (PartialEq) guard gives emit-only invalidation."""
    if emitted is not None:
        cell.set(emitted)


# ---------------------------------------------------------------------------
# Debounce.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DebounceCore[T]:
    """Debounce compute core: coalesce inputs (KeepLatest) and emit the latest
    value only after ``quiet`` ticks with no new input — every input resets the
    deadline."""

    quiet: int
    _pending: T | None = None
    _fire_at: int = 0
    _armed: bool = False

    def input(self, now: int, value: T) -> None:
        """Record an input; resets the quiet deadline to ``now + quiet``."""
        self._pending = value
        self._fire_at = now + self.quiet
        self._armed = True

    def tick(self, now: int) -> T | None:
        """Advance; emit the latest value once the quiet period has elapsed."""
        if self._armed and self._pending is not None and self._fire_at <= now:
            self._armed = False
            emitted = self._pending
            self._pending = None
            return emitted
        return None


class DebounceCell[T]:
    """Reactive debounce over any reactive value source."""

    __slots__ = ("_cell", "_core", "ctx")

    def __init__(self, ctx: dict, quiet: int) -> None:
        self.ctx = ctx
        self._core: DebounceCore[T] = DebounceCore(quiet)
        self._cell: Cell[T | None] = Cell(ctx, None)

    def input(self, now: int, value: T) -> None:
        """Record an input. Never emits, so never invalidates the output."""
        self._core.input(now, value)

    def tick(self, now: int) -> T | None:
        """Advance the logical clock; emit + project the latest value if the
        quiet period has elapsed."""
        emitted = self._core.tick(now)
        _set_output(self._cell, emitted)
        return emitted

    def output(self) -> T | None:
        """Reactive read of the last emitted value."""
        return self._cell.value

    def output_cell(self) -> Cell[T | None]:
        """The underlying output cell (advanced wiring)."""
        return self._cell


# ---------------------------------------------------------------------------
# Throttle.
# ---------------------------------------------------------------------------


class ThrottleEdge(Enum):
    """Which edge of the window a :class:`ThrottleCore` emits on."""

    LEADING = auto()
    """First input of a window passes immediately; the rest are dropped."""
    TRAILING = auto()
    """First input opens the window; the latest is emitted at the boundary."""


@dataclass(slots=True)
class ThrottleCore[T]:
    """Throttle compute core: at most one emit per ``window``."""

    edge: ThrottleEdge
    window: int
    # Leading: end of the currently-open window.
    _window_end: int | None = None
    # Trailing: start of the currently-open window + coalesced latest.
    _window_start: int | None = None
    _pending: T | None = None

    def input(self, now: int, value: T) -> T | None:
        """Record an input. Leading emits (or drops); Trailing coalesces/holds."""
        if self.edge is ThrottleEdge.LEADING:
            if self._window_end is not None and now < self._window_end:
                return None
            self._window_end = now + self.window
            return value
        # Trailing.
        if self._window_start is None:
            self._window_start = now
        self._pending = value
        return None

    def tick(self, now: int) -> T | None:
        """Advance. Trailing emits the coalesced latest at the window boundary."""
        if self.edge is ThrottleEdge.LEADING:
            return None
        ws = self._window_start
        if ws is None:
            return None
        if now >= ws + self.window and self._pending is not None:
            self._window_start = None
            emitted = self._pending
            self._pending = None
            return emitted
        return None


class ThrottleCell[T]:
    """Reactive throttle over any reactive value source."""

    __slots__ = ("_cell", "_core", "ctx")

    def __init__(self, ctx: dict, edge: ThrottleEdge, window: int) -> None:
        self.ctx = ctx
        self._core: ThrottleCore[T] = ThrottleCore(edge, window)
        self._cell: Cell[T | None] = Cell(ctx, None)

    def input(self, now: int, value: T) -> T | None:
        """Record an input; project + return any emitted value."""
        emitted = self._core.input(now, value)
        _set_output(self._cell, emitted)
        return emitted

    def tick(self, now: int) -> T | None:
        """Advance the logical clock; project + return any emitted value."""
        emitted = self._core.tick(now)
        _set_output(self._cell, emitted)
        return emitted

    def output(self) -> T | None:
        """Reactive read of the last emitted value."""
        return self._cell.value

    def output_cell(self) -> Cell[T | None]:
        """The underlying output cell (advanced wiring)."""
        return self._cell


# ---------------------------------------------------------------------------
# Sample (deterministic).
# ---------------------------------------------------------------------------


class _SampleKind(Enum):
    COUNT = auto()
    TIME = auto()


@dataclass(frozen=True, slots=True)
class SampleMode:
    """Sampling mode for :class:`SampleCore`.

    Construct via :meth:`Count` (emit every ``n``-th input) or :meth:`Time`
    (emit the held latest at each ``period`` boundary), mirroring the Rust
    ``SampleMode::Count`` / ``SampleMode::Time`` variants.
    """

    kind: _SampleKind
    param: int

    @classmethod
    def Count(cls, n: int) -> SampleMode:
        """Emit every ``n``-th input (count-based)."""
        return cls(_SampleKind.COUNT, n)

    @classmethod
    def Time(cls, period: int) -> SampleMode:
        """Emit the held latest at each ``period`` boundary (time-based)."""
        return cls(_SampleKind.TIME, period)


@dataclass(slots=True)
class SampleCore[T]:
    """Deterministic sampling compute core."""

    mode: SampleMode
    _counter: int = 0
    _next: int = field(init=False)
    _held: T | None = None

    def __post_init__(self) -> None:
        if self.mode.kind is _SampleKind.TIME:
            self._next = max(self.mode.param, 1)
        else:
            self._next = 0

    def input(self, value: T) -> T | None:
        """Record an input. Count mode emits on every ``n``-th; Time mode holds
        the latest for the next boundary."""
        if self.mode.kind is _SampleKind.COUNT:
            n = max(self.mode.param, 1)
            self._counter += 1
            if self._counter % n == 0:
                return value
            return None
        # Time.
        self._held = value
        return None

    def tick(self, now: int) -> T | None:
        """Advance. Time mode emits the held latest once per boundary crossed."""
        if self.mode.kind is _SampleKind.COUNT:
            return None
        period = max(self.mode.param, 1)
        if now < self._next:
            return None
        fires = (now - self._next) // period + 1
        self._next += fires * period
        # Emit the held latest; it persists (sampling the current value).
        return self._held


class SampleCell[T]:
    """Reactive sampler over any reactive value source."""

    __slots__ = ("_cell", "_core", "ctx")

    def __init__(self, ctx: dict, mode: SampleMode) -> None:
        self.ctx = ctx
        self._core: SampleCore[T] = SampleCore(mode)
        self._cell: Cell[T | None] = Cell(ctx, None)

    def input(self, value: T) -> T | None:
        """Record an input; project + return any emitted value (Count mode)."""
        emitted = self._core.input(value)
        _set_output(self._cell, emitted)
        return emitted

    def tick(self, now: int) -> T | None:
        """Advance the logical clock; project + return any emitted value
        (Time mode)."""
        emitted = self._core.tick(now)
        _set_output(self._cell, emitted)
        return emitted

    def output(self) -> T | None:
        """Reactive read of the last emitted value."""
        return self._cell.value

    def output_cell(self) -> Cell[T | None]:
        """The underlying output cell (advanced wiring)."""
        return self._cell


# ---------------------------------------------------------------------------
# Probabilistic sample.
# ---------------------------------------------------------------------------

_U64_MASK = (1 << 64) - 1


@runtime_checkable
class SampleRng(Protocol):
    """An injectable RNG so probabilistic sampling is deterministic under a
    fixed seed. :meth:`next_f64` yields a draw in ``[0, 1)``."""

    def next_f64(self) -> float: ...


class Lcg:
    """A small deterministic SplitMix64 generator — no external dependency,
    reproducible for the distribution property test. Byte-identical to the
    Rust ``Lcg`` so ``probabilistic_sample`` draws match cross-language."""

    __slots__ = ("_state",)

    def __init__(self, seed: int) -> None:
        self._state = seed & _U64_MASK

    def next_f64(self) -> float:
        # SplitMix64.
        self._state = (self._state + 0x9E3779B97F4A7C15) & _U64_MASK
        z = self._state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _U64_MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _U64_MASK
        z ^= z >> 31
        # 53-bit mantissa → [0, 1).
        return (z >> 11) / float(1 << 53)


@dataclass(frozen=True, slots=True)
class ProbabilisticSampleCore:
    """Probabilistic (tail) sampling compute core — the plan's only new
    algorithm. A draw in ``[0, 1)`` passes iff ``draw < rate``."""

    rate: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "rate", min(max(self.rate, 0.0), 1.0))

    def decide(self, draw: float) -> bool:
        """Whether an input with this random ``draw`` is sampled (strict ``<``)."""
        return draw < self.rate


class ProbabilisticSampleCell[T]:
    """Reactive probabilistic sampler; owns an injectable :class:`SampleRng`."""

    __slots__ = ("_cell", "_core", "_rng", "ctx")

    def __init__(self, ctx: dict, rate: float, rng: SampleRng) -> None:
        self.ctx = ctx
        self._core = ProbabilisticSampleCore(rate)
        self._rng = rng
        self._cell: Cell[T | None] = Cell(ctx, None)

    def input(self, value: T) -> T | None:
        """Sample an input using the owned RNG."""
        draw = self._rng.next_f64()
        return self.input_with_draw(value, draw)

    def input_with_draw(self, value: T, draw: float) -> T | None:
        """Sample an input against an explicit ``draw`` (deterministic /
        conformance)."""
        if self._core.decide(draw):
            self._cell.set(value)
            return value
        return None

    def output(self) -> T | None:
        """Reactive read of the last emitted value."""
        return self._cell.value

    def output_cell(self) -> Cell[T | None]:
        """The underlying output cell (advanced wiring)."""
        return self._cell

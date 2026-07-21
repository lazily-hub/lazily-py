__all__ = [
    "Cell",
    "CellSlot",
    "Source",
    "SourceCell",
    "SourceCellSlot",
    "SourceSlot",
    "cell",
    "cell_def",
    "source",
    "source_def",
]

from collections.abc import Callable
from typing import Any, TypeVar

from .batch import notify_change as _notify_change
from .slot import (
    BaseSlot,
    DisposedError,
    Slot,
    _dirty_disposed_dependents,
    _drain_resets,
    _reset_work,
    mypyc_attr,
    slot_stack,
)


C_in = TypeVar("C_in", contravariant=True)
C_ctx = TypeVar("C_ctx", bound=dict)
T = TypeVar("T")


@mypyc_attr(allow_interpreted_subclasses=True)
class Cell[T]:
    """A **source cell**: a mutable value, written from outside, that other
    reactives depend on. The v2 Cell-kernel handle name (``#lzcellkernel``) is
    :data:`Source` — the value comes from *outside* (``set`` / ``merge``), the
    writable value kind. ``Cell`` is the value-node concept and stays the native
    class name (for mypyc native-struct reads and ``isinstance`` stability across
    the family); :data:`Source` is the concrete handle bound to it. A
    :class:`~lazily.merge.MergeCell` is a ``Source`` whose write folds under a
    non-``KeepLatest`` policy (``Cell ≡ Source(KeepLatest)``).

    **No reactive in this library exposes an observer API** — not Cell, not
    :class:`~lazily.signal.Signal`. Observation in this graph is a declared
    dependency edge, not a registered callback: read the cell from a
    :class:`~lazily.slot.Slot`, :class:`~lazily.signal.Signal`, or
    :class:`~lazily.effect.Effect` and that reader becomes a dependent, which is
    what makes batching and glitch-freedom hold. A callback registry bypasses
    all of that and costs memory on every reactive whether or not anyone
    subscribes.

    Where a caller genuinely needs a stream of *every* transition rather than
    the settled value, that is a :class:`~lazily.queue.Topic`.
    """

    __slots__ = ("_disposed", "_parents", "_value", "ctx")

    _parents: set[Slot[Any, Any, Any]] | None
    _disposed: bool
    _value: T
    ctx: dict

    def __init__(self, ctx: dict, initial_value: T) -> None:
        self.ctx = ctx
        self._value = initial_value
        self._disposed = False
        # Auto-discovered parents (Slots/Signals/Effects reading this cell),
        # tracked by object identity so repeated reads never grow the fan-out.
        # Lazily materialized: an empty CPython ``set()`` is ~216 B, so
        # deferring it keeps quiescent leaf sources cheap.
        self._parents = None

    def __call__(self) -> T:
        return self.value

    @property
    def value(self) -> T:
        if self._disposed:
            raise DisposedError("read of disposed cell")
        if slot_stack:
            # Track the running parent by identity so repeated reads during one
            # computation, and re-reads across reruns, do not grow the fan-out.
            reader = slot_stack[-1]
            if self._parents is None:
                self._parents = set()
            self._parents.add(reader)
            # Symmetric forward edge, so the reader's disposal can find and
            # detach this cell's reverse edge (see ``Slot.__call__``).
            if reader._deps is None:
                reader._deps = set()
            reader._deps.add(self)
        return self._value

    @value.setter
    def value(self, value: T) -> None:
        if self._disposed:
            # Disposal is terminal: a write to a torn-down source is inert
            # rather than an error, so teardown ordering never matters to a
            # writer that outlives the cell.
            return
        _value = self._value
        self._value = value
        if self._value != _value:
            # Coalesce-aware: inside a `batch`, defer the touch to the outermost
            # boundary so multiple writes produce one invalidation wave.
            _notify_change(self)

    def get(self) -> T:
        """Alias for the value property"""
        return self.value

    def set(self, value: T) -> None:
        """Alias for value= property setter"""
        self.value = value

    @property
    def disposed(self) -> bool:
        """Whether :meth:`dispose` has been called (terminal)."""
        return self._disposed

    def dependent_count(self) -> int:
        """How many nodes currently read this cell — reverse edge degree."""
        pare = self._parents
        return 0 if pare is None else len(pare)

    def dependency_count(self) -> int:
        """Always ``0``. A cell is a pure source and reads nothing.

        Present so degree introspection is uniform across node kinds — the
        Python spelling of ``lazily-rs``'s sealed ``GraphNode`` trait.
        """
        return 0

    def dispose(self) -> None:
        """Tear down this source: detach its dependents and dirty them.

        Terminal and idempotent. Unlike :meth:`Slot.dispose` this takes no
        context — a cell already owns the one it was created against.

        A cell has no dependencies, so only the downstream direction needs
        detaching; the dirtying is what stops a surviving reader from serving a
        cached value derived from a source that no longer exists.
        """
        if self._disposed:
            return
        self._disposed = True
        pare = self._parents
        self._parents = None
        if pare:
            _dirty_disposed_dependents(pare, self.ctx)

    def touch(self) -> None:
        if self._disposed:
            return
        # The auto-discovered parents are the only fan-out: they are reactive
        # edges, so rebind-then-clear (they re-establish on recompute) and push
        # them into the coalesced invalidation wave — no tuple alloc.
        pare = self._parents
        if pare:
            self._parents = None
            ctx = self.ctx
            for parent in pare:
                _reset_work.append((parent, ctx))
            _drain_resets()


def none_callable(_: dict) -> None:
    return None


def _none_as_t(_: dict) -> Any:
    return None


@mypyc_attr(allow_interpreted_subclasses=True)
class CellSlot[C_in, C_ctx: dict, T](BaseSlot[C_in, C_ctx, Cell[T]]):
    __slots__ = ()

    def __init__(
        self,
        callable: Callable[[C_ctx], T] = _none_as_t,
        resolve_ctx: Callable[[C_in], C_ctx] | None = None,
    ) -> None:
        super().__init__(
            callable=lambda ctx: Cell(ctx, callable(ctx)), resolve_ctx=resolve_ctx
        )


def cell[C_ctx: dict, T](
    callable: Callable[[C_ctx], T] = _none_as_t,
) -> CellSlot[C_ctx, C_ctx, T]:
    """
    Decorator for creating a slot that returns a Cell.

    Note: this is intentionally a function (not a class) so type checkers
    correctly treat @cell as transforming the function type from T to Cell[T].
    """
    return CellSlot(callable=callable)


def cell_def[C_in, C_ctx: dict, T](
    resolve_ctx: Callable[[C_in], C_ctx],
) -> Callable[[Callable[[C_ctx], T]], CellSlot[C_in, C_ctx, T]]:
    def outer(callable: Callable[[C_ctx], T]) -> CellSlot[C_in, C_ctx, T]:
        return CellSlot[C_in, C_ctx, T](callable=callable, resolve_ctx=resolve_ctx)

    return outer


# -- Cell kernel vocabulary (#lzcellkernel) ---------------------------------- #
# v2: ``Source`` is the concrete handle name for the writable value kind. The
# native class keeps the name ``Cell`` (so mypyc native-struct reads and the
# ``isinstance(node, Cell)`` checks across the family are untouched) and ``Source``
# is a name alias bound to it — the reconciliation of "``Cell`` is the value-node
# concept, the bare kind name is the handle" for a language whose mypyc native
# class must keep its identity. Python has no compile-time read/write split
# (design §4): the split is expressed by which methods a kind *has* — a
# ``Source`` has ``set`` / ``merge``; a :class:`~lazily.signal.Computed` does not
# — and is a convention, not a runtime gate (§4 rejected downgrading the
# guarantee to a panic; Python simply has neither). ``SourceCell`` /
# ``SourceCellSlot`` remain as v1 back-compat aliases.
Source = Cell
SourceSlot = CellSlot
SourceCell = Cell
SourceCellSlot = CellSlot


def source[C_ctx: dict, T](
    callable: Callable[[C_ctx], T] = _none_as_t,
) -> CellSlot[C_ctx, C_ctx, T]:
    """Create a slot that returns a :data:`Source` cell (default ``KeepLatest``).

    The Cell-kernel spelling of :func:`cell`; for a non-``KeepLatest`` fold use
    :func:`~lazily.merge.merge_cell`, which builds a
    :class:`~lazily.merge.MergeCell` (a ``Source`` with policy ``M``).
    """
    return CellSlot(callable=callable)


def source_def[C_in, C_ctx: dict, T](
    resolve_ctx: Callable[[C_in], C_ctx],
) -> Callable[[Callable[[C_ctx], T]], CellSlot[C_in, C_ctx, T]]:
    """Cell-kernel spelling of :func:`cell_def`."""
    return cell_def(resolve_ctx)

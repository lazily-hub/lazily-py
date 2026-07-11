"""The unified keyed reactive family (``ReactiveFamily``) and its materialization
mode (``#lzmatmode``).

The Python counterpart of ``lazily-rs/src/reactive_family.rs``, the Lean
``LazilyFormal.Materialization`` model in ``lazily-formal``, and
``lazily-spec/cell-model.md`` § "The ``ReactiveFamily`` vehicle".

A ``ReactiveFamily`` maps keys ``K`` to per-entry reactive nodes and abstracts
over the entry's **handle kind** (the axis Rust expresses as ``ReactiveFamily<K,
V, H>``):

- **Cell entries** (:attr:`EntryKind.CELL`) are **input** nodes (:class:`Cell`).
  An input has no derivation to defer, so it is **always materialized**
  regardless of mode. The keyed cell collection (:class:`~lazily.CellFamily`) is
  this input-cell specialization.
- **Slot entries** (:attr:`EntryKind.SLOT`) are **derived** nodes (:class:`slot`).
  These are what materialization mode governs.

Materialization mode is **orthogonal** to entry kind: it fixes *when a derived
node is allocated*, never what it computes or how it converges, and it is not
observable through any node's value.

- :attr:`MaterializationMode.EAGER` (**default**) — every derived node is
  allocated when the family is built. A read is a direct node access.
- :attr:`MaterializationMode.LAZY` (opt-in) — a derived node is allocated on its
  **first read** ("materialize on pull"), addressed by key. A never-read derived
  cell is never allocated. Lazy is a keyed overlay on the eager core, not a
  second engine: the first read of key ``k`` builds the *same* node the eager
  build would have, then caches it.

Observational transparency holds: ``observe(build(eager, s), k) ==
observe(build(lazy, s), k) == s.val(k)`` for every key — mode changes allocation
timing and memory, never observed values (proved in ``lazily-formal``'s
``Materialization`` module as ``observe_canonical`` /
``eager_lazy_observationally_equivalent``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING, TypeVar

from .cell import Cell
from .slot import slot


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


__all__ = ["EntryKind", "MaterializationMode", "ReactiveFamily"]

# Free type variable for the internal handle-kind seam's value type.
V = TypeVar("V")

# A family entry's reactive handle: an input ``Cell`` or a derived ``slot``.
type FamilyHandle = Cell | slot


class EntryKind(Enum):
    """Which kind of reactive node a :class:`ReactiveFamily` entry is — the
    handle-kind axis the family abstracts over, kept orthogonal to
    :class:`MaterializationMode`.

    Mirrors ``EntryKind`` in ``lazily-rs`` and ``lazily-formal``'s
    ``Materialization`` module.
    """

    #: An **input** cell (:class:`Cell`) — always materialized, any mode.
    CELL = "cell"
    #: A **derived** slot (:class:`slot`) — materialized eagerly, or lazily on
    #: first read.
    SLOT = "slot"


class MaterializationMode(Enum):
    """When a :class:`ReactiveFamily`'s derived (slot) entries are allocated.

    Orthogonal to :class:`EntryKind`; never observable on the value axis. The
    default is :attr:`EAGER` (``Mode.default = Mode.eager``). Mirrors ``Mode`` in
    ``lazily-formal``'s ``Materialization`` module.
    """

    #: Allocate every derived node up front at build time. The shared
    #: high-performance core and the required default.
    EAGER = "eager"
    #: Allocate a derived node on its first read, keyed rather than
    #: handle-addressed. An opt-in overlay on the eager core.
    LAZY = "lazy"

    @classmethod
    def default(cls) -> MaterializationMode:
        """The default materialization mode (:attr:`EAGER`)."""
        return cls.EAGER


# ---------------------------------------------------------------------------
# Sealed handle-kind seam (CellHandle | SlotHandle only — bindings add no kinds)
# ---------------------------------------------------------------------------


class _HandleKind(ABC):
    """The entry-handle axis a :class:`ReactiveFamily` abstracts over. Sealed:
    only :class:`_CellHandleKind` (input cells) and :class:`_SlotHandleKind`
    (derived slots) — the two node kinds of the cell model — implement it, so a
    binding does not add new kinds (mirrors Rust's sealed ``FamilyHandle`` trait).
    """

    KIND: EntryKind

    @abstractmethod
    def materialize(self, ctx: dict, compute: Callable[[], V]) -> FamilyHandle:
        """Allocate the node for one entry in ``ctx``, with ``compute``
        producing its canonical value. An input cell sets the value directly; a
        derived slot wraps ``compute`` as its recomputation."""

    @abstractmethod
    def observe(self, ctx: dict, handle: FamilyHandle) -> V:
        """Read the entry's value through ``ctx`` (subscribes the running
        Slot/Effect, as any cell/slot read does)."""


class _CellHandleKind(_HandleKind):
    KIND = EntryKind.CELL

    def materialize(self, ctx: dict, compute: Callable[[], V]) -> FamilyHandle:
        # An input has no derivation: materialize by setting its value directly.
        return Cell(ctx, compute())

    def observe(self, ctx: dict, handle: FamilyHandle) -> V:
        return handle.value  # type: ignore[union-attr]


class _SlotHandleKind(_HandleKind):
    KIND = EntryKind.SLOT

    def materialize(self, ctx: dict, compute: Callable[[], V]) -> FamilyHandle:
        # A derived node: the same node an eager build would allocate.
        return slot(lambda _ctx: compute())

    def observe(self, ctx: dict, handle: FamilyHandle) -> V:
        return handle(ctx)  # type: ignore[operator]


_CELL_HANDLE = _CellHandleKind()
_SLOT_HANDLE = _SlotHandleKind()


class ReactiveFamily[K, V]:
    """The unified keyed reactive family (``#lzmatmode``): keys ``K`` map to
    per-entry reactive nodes of an :class:`EntryKind` (:class:`Cell` inputs or
    :class:`slot` derived nodes), allocated per its :class:`MaterializationMode`.

    Operations run against the owning ``ctx`` dict, like the rest of ``lazily``.
    See the module docs for the eager/lazy contract and the
    :class:`~lazily.CellFamily` input-cell specialization.
    """

    __slots__ = ("_ctx", "_factory", "_handle_kind", "_materialized", "_mode", "_order")

    def __init__(
        self,
        ctx: dict,
        mode: MaterializationMode,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        entry_kind: EntryKind = EntryKind.SLOT,
    ) -> None:
        self._ctx = ctx
        self._mode = mode
        self._factory = factory
        self._handle_kind = (
            _CELL_HANDLE if entry_kind is EntryKind.CELL else _SLOT_HANDLE
        )
        self._materialized: dict[K, FamilyHandle] = {}
        self._order: list[K] = []
        eager = mode is MaterializationMode.EAGER
        is_cell = self._handle_kind.KIND is EntryKind.CELL
        for key in keys:
            # A cell entry is always materialized regardless of mode; a slot
            # entry only under eager (``present := isInput or eager``).
            if is_cell or eager:
                self._materialize_key(key)

    # -- constructors --------------------------------------------------- #

    @classmethod
    def eager(
        cls,
        ctx: dict,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        entry_kind: EntryKind = EntryKind.SLOT,
    ) -> ReactiveFamily[K, V]:
        """Build an **eager** family: every declared key's node is allocated now.
        This is the default mode (:attr:`MaterializationMode.EAGER`)."""
        return cls(ctx, MaterializationMode.EAGER, keys, factory, entry_kind=entry_kind)

    @classmethod
    def lazy(
        cls,
        ctx: dict,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        entry_kind: EntryKind = EntryKind.SLOT,
    ) -> ReactiveFamily[K, V]:
        """Build a **lazy** family: derived (slot) entries are deferred to first
        read; input (cell) entries in ``keys`` are still materialized at build
        (cells are always materialized). Pass empty ``keys`` for a purely
        on-demand slot family."""
        return cls(ctx, MaterializationMode.LAZY, keys, factory, entry_kind=entry_kind)

    @classmethod
    def new(
        cls,
        ctx: dict,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        entry_kind: EntryKind = EntryKind.SLOT,
    ) -> ReactiveFamily[K, V]:
        """Build a family in the **default** mode (eager). Alias for
        :meth:`eager`."""
        return cls.eager(ctx, keys, factory, entry_kind=entry_kind)

    @classmethod
    def cell_family(
        cls,
        ctx: dict,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        mode: MaterializationMode = MaterializationMode.EAGER,
    ) -> ReactiveFamily[K, V]:
        """Build an **input-cell** family (:attr:`EntryKind.CELL`). Entries are
        writable inputs, always materialized at build regardless of ``mode``."""
        return cls(ctx, mode, keys, factory, entry_kind=EntryKind.CELL)

    @classmethod
    def slot_family(
        cls,
        ctx: dict,
        keys: Iterable[K],
        factory: Callable[[K], V],
        *,
        mode: MaterializationMode = MaterializationMode.EAGER,
    ) -> ReactiveFamily[K, V]:
        """Build a **derived-slot** family (:attr:`EntryKind.SLOT`) — the entry
        kind materialization mode governs."""
        return cls(ctx, mode, keys, factory, entry_kind=EntryKind.SLOT)

    # -- internals ------------------------------------------------------ #

    def _materialize_key(self, key: K) -> FamilyHandle:
        handle = self._materialized.get(key)
        if handle is not None:
            return handle  # warm: already allocated.
        handle = self._handle_kind.materialize(
            self._ctx, lambda k=key: self._factory(k)
        )
        self._materialized[key] = handle
        self._order.append(key)
        return handle

    # -- reads ---------------------------------------------------------- #

    def get(self, key: K) -> FamilyHandle:
        """Get the entry handle for ``key``, materializing it on first access
        (the lazy pull) and caching it. Under eager mode an entry is already
        present, so this returns the cached handle."""
        return self._materialize_key(key)

    def observe(self, key: K) -> V:
        """Observe ``key``'s value — the headline transparency law: the returned
        value is identical under either mode. Materializes the entry if absent."""
        return self._handle_kind.observe(self._ctx, self.get(key))

    def is_present(self, key: K) -> bool:
        """Whether ``key`` is currently materialized (present in the allocated
        set). Non-reactive."""
        return key in self._materialized

    def present_keys(self) -> list[K]:
        """The currently-materialized keys, in first-materialization order. The
        present set only grows (deferral, not de-allocation)."""
        return list(self._order)

    def present_count(self) -> int:
        """Number of currently-materialized entries."""
        return len(self._order)

    @property
    def mode(self) -> MaterializationMode:
        """This family's materialization mode."""
        return self._mode

    @property
    def entry_kind(self) -> EntryKind:
        """This family's entry kind (:attr:`EntryKind.CELL` for a cell family,
        :attr:`EntryKind.SLOT` for a slot family)."""
        return self._handle_kind.KIND

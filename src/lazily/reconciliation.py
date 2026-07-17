"""Keyed reconciliation by stable key — the move-minimized ``{insert, remove,
move, update}`` op set a level diff emits.

This is the Python counterpart of the Lean ``LazilyFormal.Reconciliation``
formal model in ``lazily-formal`` and the executable reference behind the
``lazily-spec/conformance/collections/keyed_reconciliation_lis.json``
conformance fixture. Reconciling a ``prior`` level to a ``target`` level by
**stable key, not position**, emits the minimal op set: keys already in relative
order (the longest-increasing-subsequence (LIS) over their prior indices) do
NOT move, and a stable entry (in the LIS, unchanged value) is neither moved nor
updated — so its value cell is untouched by the reconcile.

The move-minimization is over a longest-increasing-subsequence kernel: the LIS
is made *definitional* by the include-vs-skip recursion (the longer of the two
branches wins), exactly as in the Lean model — so the result is genuinely
longest, not merely a greedy approximation. ``reconcile_ops`` is a pure total
function of ``(prior, target)``, so the move-minimized / stable-not-invalidated
guarantees hold for *every* input — the universal result no finite fixture suite
can establish.
"""

from __future__ import annotations


__all__ = [
    "EntryValue",
    "Key",
    "Level",
    "ReconcileOp",
    "common_keys",
    "idx_in",
    "lis_by",
    "moved_keys",
    "reconcile_ops",
    "stable_keys",
]

from dataclasses import dataclass
from typing import Protocol, TypeVar


class Key(Protocol):
    """A hashable, comparable collection key (a ``K`` in ``lazily-rs``)."""

    def __hash__(self) -> int: ...


class EntryValue(Protocol):
    """A per-entry value. The model exercises equality, never the type."""

    def __eq__(self, other: object) -> bool: ...


K = TypeVar("K", bound=Key)
V = TypeVar("V", bound=EntryValue)


@dataclass(frozen=True)
class Level[K, V]:
    """A keyed level: an insertion-ordered list of keys plus a value map.

    Mirrors ``LazilyFormal.Reconciliation.Level``. Only keys present in
    :attr:`order` are meaningful; the value map is treated as total via
    :meth:`value_of` (absent ⇒ the key is not a member).
    """

    order: list[K]
    values: dict[K, V]

    def value_of(self, key: K) -> V | None:
        return self.values.get(key)


@dataclass(frozen=True)
class ReconcileOp[K, V]:
    """One diff op emitted by reconciling a prior level to a target level
    (``cell-model.md:236``): the minimal ``{insert, remove, move, update}``
    per key.

    The ``move`` op is a single reposition (not remove + insert); for a
    collection with explicit positions it carries the resolved anchor
    (``after``) so the caller can emit a single ``move_after``.
    """

    kind: str  # "insert" | "remove" | "update" | "move"
    key: K
    value: V | None = None
    after: K | None = None


# --------------------------------------------------------------------------- #
# The longest increasing subsequence (LIS)
# --------------------------------------------------------------------------- #


def lis_by[K: Key](p: dict[K, int], keys: list[K]) -> list[K]:
    """The longest strictly-increasing (by ``p``) subsequence of ``keys``.

    Mirrors ``lisBy``. ``p`` maps each key to its prior index; the LIS is the
    maximal set of common keys already in relative prior-index order, which a
    move-minimized reconcile therefore leaves untouched.

    Computed by patience sorting in **O(n log n)** (it replaced an O(2ⁿ)
    include-vs-skip recursion). Among all longest increasing subsequences the
    lexicographically-smallest by ``keys`` position is returned — exactly the
    subsequence the include-on-tie recursion chose — so the stable set a
    reconcile selects is unchanged.
    """
    n = len(keys)
    if n == 0:
        return []
    seq = [p[k] for k in keys]
    # len_start[i] = length of the longest strictly-increasing subsequence that
    # starts at index i. Built right-to-left: ``heads[L]`` is the greatest first
    # value of any length-(L+1) increasing subsequence in the processed suffix.
    # ``heads`` is strictly decreasing in L, so it binary-searches.
    len_start = [0] * n
    heads: list[int] = []
    for i in range(n - 1, -1, -1):
        v = seq[i]
        # Rightmost L whose head > v (preparable); heads is strictly decreasing.
        lo, hi = 0, len(heads)
        while lo < hi:
            mid = (lo + hi) >> 1
            if heads[mid] > v:
                lo = mid + 1
            else:
                hi = mid
        length = lo + 1
        len_start[i] = length
        idx = length - 1
        if idx == len(heads):
            heads.append(v)
        elif v > heads[idx]:
            heads[idx] = v
    # Greedy lex-smallest-by-index reconstruction: walk left to right, taking
    # the earliest index that still reaches the max length with a rising value.
    target = len(heads)
    out: list[K] = []
    prev: int | None = None
    for i in range(n):
        if target == 0:
            break
        if len_start[i] == target and (prev is None or seq[i] > prev):
            out.append(keys[i])
            prev = seq[i]
            target -= 1
    return out


def idx_in[K: Key](order: list[K], key: K) -> int:
    """Index of ``key`` in ``order`` (0-based), or ``len(order)`` if absent.
    Mirrors ``idxIn`` (defined without ``list.index`` so prior-only keys map to
    a total, well-defined value)."""
    for i, k in enumerate(order):
        if k == key:
            return i
    return len(order)


def common_keys[K: Key](prior: list[K], target: list[K]) -> list[K]:
    """The keys present in both ``prior`` and ``target``, in ``target`` order.
    Mirrors ``commonKeys``."""
    prior_set = set(prior)
    return [k for k in target if k in prior_set]


def stable_keys[K: Key](prior: list[K], target: list[K]) -> list[K]:
    """The keys the reconcile leaves in place (the LIS over the common keys by
    prior index): the maximal already-in-relative-order subset. Mirrors
    ``stableKeys``."""
    commons = common_keys(prior, target)
    # Build the prior-index map once (O(n)) instead of an ``idx_in`` linear scan
    # per common key (O(n·m)) — ``#lzpyreconcileidx``.
    prior_index = {k: i for i, k in enumerate(prior)}
    p = {k: prior_index[k] for k in commons}
    return lis_by(p, commons)


def moved_keys[K: Key](prior: list[K], target: list[K]) -> list[K]:
    """The common keys the reconcile repositions (common keys not in the LIS).
    Mirrors ``movedKeys``."""
    stable = set(stable_keys(prior, target))
    return [k for k in common_keys(prior, target) if k not in stable]


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #


def reconcile_ops[K: Key, V: EntryValue](
    prior: Level[K, V], target: Level[K, V]
) -> list[ReconcileOp[K, V]]:
    """The reconcile op set: the minimal ``{insert, remove, move, update}`` per
    key (``cell-model.md:236``). Order: remove ++ insert ++ update ++ move.

    Moves are emitted only for common keys NOT in the LIS; a move carries the
    resolved ``after`` anchor (the preceding stable key in target order, or
    ``None`` for a move to the front). Stable keys with unchanged value emit
    neither ``move`` nor ``update`` — so their value cells are untouched.
    """
    prior_order = prior.order
    target_order = target.order
    prior_set = set(prior_order)
    target_set = set(target_order)

    ops: list[ReconcileOp[K, V]] = []

    # remove: one per prior-only key.
    for k in prior_order:
        if k not in target_set:
            ops.append(ReconcileOp(kind="remove", key=k))

    # insert: one per target-only key (in target order).
    for k in target_order:
        if k not in prior_set:
            ops.append(ReconcileOp(kind="insert", key=k, value=target.value_of(k)))

    # update: one per common key whose value changed.
    for k in common_keys(prior_order, target_order):
        if prior.value_of(k) != target.value_of(k):
            ops.append(ReconcileOp(kind="update", key=k, value=target.value_of(k)))

    # move: one per common non-LIS key. Resolve the `after` anchor from the
    # target order — the preceding key in target order that is stable (so the
    # move repositions relative to a key that will not itself move), or None
    # when the key moves to the front. Compute the LIS once and derive the
    # moved set from it (``#lzpyreconcileidx`` — avoids recomputing the LIS).
    commons = common_keys(prior_order, target_order)
    stable = set(stable_keys(prior_order, target_order))
    moved = [k for k in commons if k not in stable]
    moved_set = set(moved)
    for k in moved:
        anchor: K | None = None
        for tk in target_order:
            if tk == k:
                break
            if tk in stable or tk in moved_set:
                anchor = tk
        ops.append(ReconcileOp(kind="move", key=k, after=anchor))

    return ops

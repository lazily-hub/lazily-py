"""Named keyed fold — :class:`KeyedFold` (``#lzpykeyedfold``).

N independent writers, one key each. A writer sets, folds and **clears only its
own key**; the summary is a guarded :class:`~lazily.signal.Computed` over the
live set, so a reader of the summary is invalidated exactly when the summary
changes, and a reader of one key is never invalidated by a sibling's write.

This is the general shape :class:`~lazily.service.HealthCell` implements for
booleans and :class:`~lazily.merge.MergeCell` has the algebra for but no keyed
surface. The bug it exists to prevent is the last-writer-wins one: several
components sharing a single cell for "the current errors", where whoever writes
last erases everyone else's entry and whoever clears erases entries that were
never theirs. Here a key is owned, so a writer can only ever publish or retract
its own contribution.

Example::

    ctx: dict = {}
    errors = KeyedFold[str, str, dict[str, str]](ctx)

    intake = errors.claim("intake")
    dispatcher = errors.claim("dispatcher")

    intake.set("connection refused")
    dispatcher.set("timeout")
    intake.clear()  # dispatcher's entry survives

    errors.value  # {'dispatcher': 'timeout'}

Pass ``summarize`` to fold the live set into something other than the dict — a
worst-of aggregate, a count, a sorted list — and ``policy`` to make a writer's
:meth:`KeyedFoldWriter.merge` accumulate rather than replace.
"""

from __future__ import annotations


__all__ = [
    "KeyAlreadyClaimedError",
    "KeyedFold",
    "KeyedFoldWriter",
    "keyed_fold",
]

from typing import TYPE_CHECKING, Any, cast

from .collection import SourceMap
from .merge import KeepLatest
from .signal import computed


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .cell import Source
    from .merge import MergePolicy
    from .signal import Computed


class KeyAlreadyClaimedError(RuntimeError):
    """:meth:`KeyedFold.claim` was called twice for the same key.

    The whole point of the fold is that a key has one owner; a second claim is
    the bug, not a sharing mechanism. Use :meth:`KeyedFold.writer` when a shared
    handle really is intended.
    """


class KeyedFoldWriter[K, V]:
    """One writer's handle on one key. It can reach no other key."""

    __slots__ = ("_fold", "_key")

    def __init__(self, fold: KeyedFold[K, V, Any], key: K) -> None:
        self._fold = fold
        self._key = key

    @property
    def key(self) -> K:
        """The key this writer owns."""
        return self._key

    def set(self, value: V) -> None:
        """Publish ``value`` as this key's entry, inserting it if absent.

        An equal write is inert: the entry is one cell, so the guard applies
        before the summary is ever asked to recompute.
        """
        self._fold._set(self._key, value)

    def merge(self, op: V) -> None:
        """Fold ``op`` into this key's entry under the fold's merge policy.

        A first write seeds the entry with ``op`` — a policy is an associative
        merge, not a monoid, so there is no identity to start from.
        """
        self._fold._merge(self._key, op)

    def clear(self) -> bool:
        """Retract this key's entry. Returns whether one was present.

        The only removal a writer can perform. Sibling entries are untouched.
        """
        return self._fold.clear(self._key)

    def release(self) -> bool:
        """:meth:`clear` this key and release the claim so it can be re-claimed."""
        present = self.clear()
        self._fold._release(self._key)
        return present

    @property
    def present(self) -> bool:
        """Whether this key currently has an entry. Untracked."""
        return self._fold.contains(self._key)

    @property
    def value(self) -> V | None:
        """This key's entry, or ``None`` when absent. Untracked."""
        return self._fold.get(self._key)

    def get(self, ctx: Any = None) -> V | None:
        """Read this key's entry. Pass a compute view to depend on this key alone."""
        return self._fold.get(self._key, ctx)

    def __repr__(self) -> str:
        return f"<KeyedFoldWriter {self._key!r} present={self.present}>"


class KeyedFold[K, V, S]:
    """A keyed set of owned entries plus a guarded summary over the live set.

    ``summarize`` receives a plain mapping of the present entries and returns the
    summary; it defaults to a copy of the mapping itself, which makes the fold a
    live per-writer map. ``policy`` is the merge algebra
    :meth:`KeyedFoldWriter.merge` folds under (default
    :data:`~lazily.merge.KeepLatest`, i.e. replace).

    The summary is **eager** by default, matching
    :class:`~lazily.service.HealthCell`: it re-projects after every write so a
    summary reader is invalidated only when the summary actually changes. Pass
    ``eager=False`` to defer the fold to the first read instead, at the cost of
    that guard — a lazy computed holds no settled value to compare, so any write
    reaches a summary reader.
    """

    __slots__ = ("_claims", "_entries", "_policy", "_summary", "_writers")

    def __init__(
        self,
        ctx: dict,
        summarize: Callable[[Mapping[K, V]], S] | None = None,
        *,
        policy: MergePolicy[V] = KeepLatest,
        eager: bool = True,
    ) -> None:
        self._entries: SourceMap[K, V] = SourceMap(ctx)
        self._policy = policy
        self._claims: set[K] = set()
        self._writers: dict[K, KeyedFoldWriter[K, V]] = {}
        fold = summarize if summarize is not None else _identity_summary

        def summary(compute: Any) -> S:
            # ``keys`` subscribes to the order signal and each ``get`` to that
            # entry alone, so the summary depends on membership *and* on every
            # live value — and on nothing else.
            live = {
                key: self._entries.get(key, compute)
                for key in self._entries.keys(compute)
            }
            return cast("S", fold(cast("Mapping[K, V]", live)))

        self._summary: Computed[S] = computed(ctx, summary)
        if eager:
            self._summary.eager()

    # -- writers ------------------------------------------------------------ #

    def claim(self, key: K) -> KeyedFoldWriter[K, V]:
        """Claim exclusive ownership of ``key`` and return its writer.

        Raises :class:`KeyAlreadyClaimedError` if the key is already claimed —
        two components writing one key is the defect this type exists to catch.
        """
        if key in self._claims:
            raise KeyAlreadyClaimedError(
                f"{key!r} is already claimed; a keyed-fold key has one owner "
                f"(use writer() if a shared handle is really intended)"
            )
        self._claims.add(key)
        return self.writer(key)

    def writer(self, key: K) -> KeyedFoldWriter[K, V]:
        """The writer handle for ``key``, without claiming exclusivity.

        Stable: the same key always yields the same handle.
        """
        existing = self._writers.get(key)
        if existing is not None:
            return existing
        created = KeyedFoldWriter(self, key)
        self._writers[key] = created
        return created

    def is_claimed(self, key: K) -> bool:
        """Whether :meth:`claim` currently holds ``key``."""
        return key in self._claims

    def _release(self, key: K) -> None:
        self._claims.discard(key)

    # -- entries ------------------------------------------------------------ #

    def _set(self, key: K, value: V) -> None:
        self._entries.set(key, value)

    def _merge(self, key: K, op: V) -> None:
        current = self._entries.get(key)
        if current is None and not self._entries.is_present(key):
            self._entries.set(key, op)
            return
        self._entries.set(key, self._policy.merge(cast("V", current), op))

    def clear(self, key: K) -> bool:
        """Retract ``key``'s entry. Returns whether one was present."""
        return self._entries.remove(key)

    def get(self, key: K, ctx: Any = None) -> V | None:
        """``key``'s entry, or ``None``. Pass a compute view to depend on it."""
        return self._entries.get(key, ctx)

    def contains(self, key: K, ctx: Any = None) -> bool:
        """Whether ``key`` has an entry. Pass a compute view to depend on membership."""
        if ctx is None:
            return self._entries.is_present(key)
        return self._entries.contains_key(key, ctx)

    def keys(self, ctx: Any = None) -> list[K]:
        """The present keys, insertion-ordered. Tracked when ``ctx`` is a view."""
        if ctx is None:
            return self._entries.present_keys()
        return self._entries.keys(ctx)

    def entries(self, ctx: Any = None) -> dict[K, V]:
        """A snapshot of the live entries. Tracked when ``ctx`` is a view."""
        return {key: cast("V", self.get(key, ctx)) for key in self.keys(ctx)}

    def entry_cell(self, key: K) -> Source[V] | None:
        """The cell backing ``key``, or ``None`` when absent (advanced wiring)."""
        return cast("Source[V] | None", self._entries.handle(key))

    @property
    def policy(self) -> MergePolicy[V]:
        """The merge algebra :meth:`KeyedFoldWriter.merge` folds under."""
        return self._policy

    # -- summary ------------------------------------------------------------ #

    @property
    def summary(self) -> Computed[S]:
        """The guarded summary computed. Read it through a view to depend on it."""
        return self._summary

    @property
    def value(self) -> S:
        """The current summary, untracked."""
        return self._summary.value

    def observe(self, compute: Any) -> S:
        """The summary as a **tracked** read through ``compute``."""
        return cast("S", compute.read(self._summary))

    def __call__(self) -> S:
        return self._summary.value

    def dispose(self) -> None:
        """Tear down the summary computed and drop every entry."""
        self._summary.dispose()
        for key in self._entries.present_keys():
            self._entries.remove(key)
        self._claims.clear()
        self._writers.clear()

    def __repr__(self) -> str:
        return (
            f"<KeyedFold policy={self._policy.name} "
            f"keys={self._entries.present_keys()}>"
        )


def _identity_summary(entries: Mapping[Any, Any]) -> dict[Any, Any]:
    return dict(entries)


def keyed_fold[K, V, S](
    ctx: dict,
    summarize: Callable[[Mapping[K, V]], S] | None = None,
    *,
    policy: MergePolicy[V] = KeepLatest,
    eager: bool = True,
) -> KeyedFold[K, V, S]:
    """Create a :class:`KeyedFold` over ``ctx``."""
    return KeyedFold(ctx, summarize, policy=policy, eager=eager)

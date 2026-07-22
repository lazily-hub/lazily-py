"""Presence + ephemeral plane (``#lzpresence``).

The Python counterpart of ``lazily-rs/src/presence.rs`` and
``lazily-spec/docs/presence.md``. The CRDT plane is durable; collaborative apps
also need an **ephemeral** plane that does not persist (live cursors, typing
indicators, presence). Each primitive is a pure compute **core** (a keyed map /
single value + TTL over a logical clock) split from a reactive **cell**
projecting the live view onto a :class:`~lazily.cell.Cell` — it invalidates
**only** on a live-view change.

The ephemeral plane is distinct from the durable plane: the :class:`Ephemeral`
marker tags values that MUST NOT be persisted, while :class:`Durable` tags
values that may be written to a durable outbox. In Rust these are compile-time
type markers (a durable sink statically rejects ephemeral values); in Python
they are documentation-only marker classes — the conformance is about the
``EphemeralCell`` / ``PresenceCell`` / ``AwarenessCell`` behavior.

Primitives
----------

- :class:`EphemeralCore` / :class:`EphemeralCell` — a single value with
  auto-expiry ("the last value seen in window N").
- :class:`EphemeralMapCore` — a per-key ephemeral map with TTL eviction, the
  shared core behind presence and awareness.
- :class:`PresenceCell` — per-peer presence kept alive by heartbeats,
  auto-evicted on membership loss (``evict``) or TTL lapse (``tick``).
- :class:`AwarenessCell` — typed ephemeral broadcast (cursors / selections):
  last-writer-per-peer overwrite with a TTL.
"""

from __future__ import annotations


__all__ = [
    "AwarenessCell",
    "Durable",
    "Ephemeral",
    "EphemeralCell",
    "EphemeralCore",
    "EphemeralMapCore",
    "EphemeralValue",
    "PresenceCell",
]

from typing import Any

from .cell import Cell


# ===========================================================================
# Plane markers
# ===========================================================================


class Ephemeral:
    """Marker: a value on the **ephemeral** plane. MUST NOT be persisted.

    Documentation-only in Python (the Rust counterpart is a compile-time trait
    that a durable sink statically rejects).
    """


class Durable:
    """Marker: a value that may be written to the durable outbox."""


class EphemeralValue[T](Ephemeral):
    """A newtype witnessing the :class:`Ephemeral` marker."""

    __slots__ = ("value",)

    def __init__(self, value: T) -> None:
        self.value = value


# ===========================================================================
# Ephemeral single value
# ===========================================================================


class EphemeralCore[T]:
    """Single-value auto-expiry compute core — "the last value seen in window N".

    Pure logic: :meth:`set` stamps ``expiry = now + ttl``; :meth:`tick` clears
    the value once ``now >= expiry``; a :meth:`set` before expiry overwrites.
    """

    __slots__ = ("_expiry", "_value")

    def __init__(self) -> None:
        self._value: T | None = None
        self._expiry: int = 0

    def set(self, value: T, now: int, ttl: int) -> None:
        """Set the value, expiring at ``now + ttl``."""
        self._value = value
        self._expiry = now + ttl

    def tick(self, now: int) -> None:
        """Clear the value once ``now >= expiry``."""
        if self._value is not None and now >= self._expiry:
            self._value = None

    def value(self) -> T | None:
        """The current value (``None`` once expired)."""
        return self._value


class EphemeralCell[T]:
    """Reactive single-value ephemeral cell.

    Wraps an :class:`EphemeralCore` and projects its live value onto an internal
    :class:`~lazily.cell.Cell`; :meth:`value` reads the live view. Reactive reads
    invalidate only when the projected value actually changes.
    """

    __slots__ = ("_core", "_value")

    def __init__(self, ctx: dict) -> None:
        self._core: EphemeralCore[T] = EphemeralCore()
        self._value: Cell[T | None] = Cell(ctx, None)

    def _refresh(self) -> None:
        self._value.value = self._core.value()

    def set(self, value: T, now: int, ttl: int) -> None:
        """Set the value, expiring at ``now + ttl``."""
        self._core.set(value, now, ttl)
        self._refresh()

    def tick(self, now: int) -> None:
        """Advance the logical clock, clearing the value once it has expired."""
        self._core.tick(now)
        self._refresh()

    def value(self, ctx: Any = None) -> T | None:
        """Reactive read of the live value (``None`` once expired).

        Pass the caller's :class:`~lazily.compute.Compute` view (``ctx``) to
        value-thread the edge inside a reactive body; omit it for an untracked
        top-level read (``#lzcellkernel`` bare-read removal)."""
        if ctx is None:
            return self._value.value
        return ctx.read(self._value)

    def value_cell(self) -> Cell[T | None]:
        """The internal value cell, for advanced wiring."""
        return self._value


# ===========================================================================
# Keyed per-peer ephemeral map (shared by presence + awareness)
# ===========================================================================


class EphemeralMapCore[K, V]:
    """Per-key ephemeral map with TTL eviction — the shared core behind presence
    and awareness. Each entry carries an expiry; :meth:`tick` evicts lapsed
    entries."""

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        # key -> (value, expiry)
        self._entries: dict[K, tuple[V, int]] = {}

    def set(self, key: K, value: V, now: int, ttl: int) -> None:
        """Set/refresh ``key``'s value (last-writer wins), expiring at
        ``now + ttl``."""
        self._entries[key] = (value, now + ttl)

    def evict(self, key: K) -> None:
        """Drop ``key`` immediately (membership ``Dead`` / ``Left``)."""
        self._entries.pop(key, None)

    def tick(self, now: int) -> None:
        """Evict entries whose TTL has lapsed (``now >= expiry``)."""
        self._entries = {
            k: (v, expiry) for k, (v, expiry) in self._entries.items() if now < expiry
        }

    def get(self, key: K, now: int) -> V | None:
        """The live value for ``key`` at ``now`` (``None`` if absent/expired)."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expiry = entry
        return value if now < expiry else None

    def present(self, now: int) -> dict[K, V]:
        """The live ``key -> value`` map at ``now`` (sorted by key)."""
        return {
            k: v for k, (v, expiry) in sorted(self._entries.items()) if now < expiry
        }


class PresenceCell[K, V]:
    """Reactive per-peer presence: heartbeat-kept, membership- and TTL-evicted.

    Projects the live ``peer -> value`` map onto an internal
    :class:`~lazily.cell.Cell`; :meth:`present` reads it. The cell invalidates
    only when the live view changes.
    """

    __slots__ = ("_core", "_present", "_ttl")

    def __init__(self, ctx: dict, ttl: int) -> None:
        self._core: EphemeralMapCore[K, V] = EphemeralMapCore()
        self._present: Cell[dict[K, V]] = Cell(ctx, {})
        self._ttl = ttl

    def _refresh(self, now: int) -> None:
        self._present.value = self._core.present(now)

    def heartbeat(self, peer: K, value: V, now: int) -> None:
        """Heartbeat a peer's presence (expiring at ``now + ttl``)."""
        self._core.set(peer, value, now, self._ttl)
        self._refresh(now)

    def evict(self, peer: K, now: int) -> None:
        """Evict a peer on membership loss."""
        self._core.evict(peer)
        self._refresh(now)

    def tick(self, now: int) -> None:
        """Advance the logical clock, evicting peers whose TTL has lapsed."""
        self._core.tick(now)
        self._refresh(now)

    def present(self, ctx: Any = None) -> dict[K, V]:
        """Reactive read of the live ``peer -> value`` presence map. Pass the
        caller's compute view (``ctx``) to value-thread the edge; omit for an
        untracked top-level read (``#lzcellkernel``)."""
        if ctx is None:
            return self._present.value
        return ctx.read(self._present)

    def present_cell(self) -> Cell[dict[K, V]]:
        """The internal presence cell, for advanced wiring."""
        return self._present


class AwarenessCell[K, V]:
    """Reactive typed ephemeral broadcast (cursors / selections):
    last-writer-per-peer with a TTL.

    Values do NOT merge — a later ``set`` for a peer overwrites the earlier one.
    Projects the live map onto an internal :class:`~lazily.cell.Cell`;
    :meth:`present` reads it and invalidates only on a live-view change.
    """

    __slots__ = ("_core", "_present", "_ttl")

    def __init__(self, ctx: dict, ttl: int) -> None:
        self._core: EphemeralMapCore[K, V] = EphemeralMapCore()
        self._present: Cell[dict[K, V]] = Cell(ctx, {})
        self._ttl = ttl

    def _refresh(self, now: int) -> None:
        self._present.value = self._core.present(now)

    def set(self, peer: K, value: V, now: int) -> None:
        """Set a peer's awareness value (last-writer wins, no merge)."""
        self._core.set(peer, value, now, self._ttl)
        self._refresh(now)

    def tick(self, now: int) -> None:
        """Advance the logical clock, evicting expired awareness entries."""
        self._core.tick(now)
        self._refresh(now)

    def get(self, peer: K, now: int) -> V | None:
        """The live awareness value for ``peer`` at ``now`` (non-reactive)."""
        return self._core.get(peer, now)

    def present(self, ctx: Any = None) -> dict[K, V]:
        """Reactive read of the live ``peer -> value`` awareness map. Pass the
        caller's compute view (``ctx``) to value-thread the edge; omit for an
        untracked top-level read (``#lzcellkernel``)."""
        if ctx is None:
            return self._present.value
        return ctx.read(self._present)

    def present_cell(self) -> Cell[dict[K, V]]:
        """The internal awareness cell, for advanced wiring."""
        return self._present

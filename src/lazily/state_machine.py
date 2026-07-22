"""Finite state machine backed by a reactive :class:`~lazily.cell.Cell`.

This mirrors ``StateMachine<S, E>`` in the Rust reference (``lazily-rs``):
the state lives in a :class:`~lazily.cell.Cell` so any ``Computed``, ``Effect``,
or subscriber that reads :attr:`StateMachine.state` is automatically
invalidated when the machine transitions.

The transition function is pure: ``Callable[[S, E], S | None]``.
Returning ``None`` rejects the event (a guard); returning a value
accepts the event and sets the cell to the new state. A self-transition
that returns an equal state is accepted but suppressed by the Cell's
``PartialEq`` guard, so no downstream cascade fires.
"""

from __future__ import annotations


__all__ = ["StateMachine"]

from typing import TYPE_CHECKING, Any, TypeVar

from .cell import Cell
from .effect import effect as _effect


if TYPE_CHECKING:
    from collections.abc import Callable

S = TypeVar("S")
E = TypeVar("E")

# Distinguishes "the effect has not run yet" from a legitimate ``None`` state,
# mirroring the ``Option<S>`` seed in the Rust reference (`lazily-rs`
# `state_machine.rs::on_transition`).
_UNSET: Any = object()


class StateMachine[S, E]:
    """A finite state machine backed by a reactive :class:`Cell`.

    :param ctx: The reactive context dict shared by all primitives.
    :param initial: The initial state value.
    :param transition: A pure function ``(state, event) -> next_state | None``.
        Returning ``None`` rejects the event (guard).

    Example::

        ctx = {}
        m = StateMachine(
            ctx,
            "Red",
            lambda s, e: {"Red": "Green", "Green": "Yellow", "Yellow": "Red"}.get(s)
            if e == "advance"
            else None,
        )

        m.send("advance")  # True  — accepted
        m.state  # "Green"
        m.send("advance")  # True
        m.state  # "Yellow"
    """

    __slots__ = ("_cell", "_transition", "ctx")

    def __init__(
        self,
        ctx: dict,
        initial: S,
        transition: Callable[[S, E], S | None],
    ) -> None:
        self.ctx = ctx
        self._cell: Cell[S] = Cell(ctx, initial)
        self._transition = transition

    def send(self, event: E) -> bool:
        """Send an event to the machine.

        Returns ``True`` if the transition function accepted the event
        (returned a value), ``False`` if it was rejected (returned ``None``).

        A self-transition that returns an equal state is accepted (returns
        ``True``) but will not invalidate dependents — the ``PartialEq`` guard
        on the underlying Cell suppresses the no-op update.
        """
        current = self._cell.get()
        next_state = self._transition(current, event)
        if next_state is not None:
            self._cell.set(next_state)
            return True
        return False

    @property
    def state(self) -> S:
        """The current state — an **untracked** snapshot read.

        A property cannot receive the caller's compute view, so this read forms
        no dependency edge (``#lzcellkernel`` bare-read removal). To subscribe a
        Computed/Effect to the state, read it *through* the compute view with
        :meth:`state_at` (or ``ctx.read(machine.cell)``).
        """
        return self._cell.get()

    def state_at(self, ctx: object) -> S:
        """The current state, **value-threaded** through the caller's compute view.

        Reading this inside a Computed/Effect body registers the dependency edge
        against that reader, so it re-runs on transition — the tracked companion
        to the untracked :attr:`state` property.
        """
        return ctx.read(self._cell)  # type: ignore[attr-defined]

    @property
    def cell(self) -> Cell[S]:
        """The underlying Cell holding the state value."""
        return self._cell

    def on_transition(self, handler: Callable[[S, S], Any]) -> Callable[[], None]:
        """Register a handler that fires with ``(old, new)`` on state change.

        Implemented as an :class:`~lazily.effect.Effect` over the backing cell —
        a declared dependency edge, not a callback registered on the cell. This
        mirrors ``StateMachine::on_transition`` in the Rust reference
        (`lazily-rs`): the effect reads the state, compares it against a ``prev``
        captured in the closure, and invokes ``handler`` only on a real change.

        The handler is **not** called on registration; the effect's initial run
        only seeds ``prev``. It fires on subsequent transitions to a different
        state. This is the state-machine analog of on-enter/on-exit: the handler
        receives both the previous and new state.

        Because this is a graph participant, it observes the **settled** value
        of a :func:`~lazily.batch.batch`. ``A -> B -> C`` inside one batch
        reports a single ``(A, C)`` transition, not two — a batch asserts
        atomicity, so intermediate states are not observable.

        Returns a disposer function; call it to stop observing.
        """
        cell = self._cell
        prev: list[Any] = [_UNSET]

        def _body(_ctx: Any) -> None:
            current = _ctx.read(cell)  # value-threaded: declares the dependency edge
            old = prev[0]
            prev[0] = current
            if old is not _UNSET and old != current:
                handler(old, current)

        eff = _effect(_body)
        eff(self.ctx)  # initial run: seeds `prev`, fires nothing
        return eff.dispose

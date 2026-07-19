"""Finite state machine backed by a reactive :class:`~lazily.cell.Cell`.

This mirrors ``StateMachine<S, E>`` in the Rust reference (``lazily-rs``):
the state lives in a :class:`~lazily.cell.Cell` so any ``Slot``, ``Signal``,
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

from .cell import Cell, CellSubscriber


if TYPE_CHECKING:
    from collections.abc import Callable

S = TypeVar("S")
E = TypeVar("E")


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
        """The current state. Auto-subscribes when read inside a Slot/Signal."""
        return self._cell.get()

    @property
    def cell(self) -> Cell[S]:
        """The underlying Cell holding the state value."""
        return self._cell

    def on_transition(self, handler: Callable[[S, S], Any]) -> Callable[[], None]:
        """Register a handler that fires with ``(old, new)`` on state change.

        The handler is **not** called on registration; it only fires on
        subsequent transitions to a different state. This is the state-machine
        analog of on-enter/on-exit: the handler receives both the previous and
        new state.

        Returns a disposer function; call it to stop observing.
        """
        prev: list[S] = [self._cell.get()]

        def _subscriber(_ctx: dict[Any, Any], new_state: S) -> None:
            old = prev[0]
            if old != new_state:
                handler(old, new_state)
                prev[0] = new_state

        subscriber: CellSubscriber[S] = _subscriber
        return self._cell.subscribe(subscriber)

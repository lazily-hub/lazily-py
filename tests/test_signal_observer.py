"""Observer semantics for :class:`lazily.Signal` (#lzdartobservercow).

``Signal.subscribe`` is a persistent ``set`` of observers plus a disposer.
:class:`lazily.Cell` has no counterpart — the Cell observer API was removed
because a callback registry on every reactive leaf bypasses the graph, ignores
batching, and costs memory whether or not anyone subscribes. This file pins what
remains on ``Signal``.

Every test in :class:`TestSignalObserverEquivalence` is written against a shim
(:func:`_subscribe`) that uses the public disposer when ``subscribe`` returns
one and otherwise falls back to the private ``_subscribers.discard`` that a
caller would have been forced to use before the disposer existed. That lets the
exact same file run green against both the pre-change and post-change
implementations, so "no behaviour changed" is a checked claim rather than an
assertion.

:class:`TestSignalObserverDisposerContract` covers the *new* surface only.
"""

from collections.abc import Callable
from typing import Any

from lazily import Cell, Signal


Sub = Callable[[dict, Any], Any]


def _subscribe(sig: Signal[Any], sub: Sub) -> Callable[[], None]:
    """Subscribe and return a disposer, on old or new implementations.

    The pre-change ``Signal.subscribe`` returned ``None``; the only way to undo
    a registration was to reach into ``_subscribers``. Post-change it returns an
    idempotent disposer. Both are normalised to a disposer here.
    """
    disposer = sig.subscribe(sub)
    if callable(disposer):
        return disposer

    def _legacy_dispose() -> None:
        subs = sig._subscribers
        if subs is not None:
            subs.discard(sub)

    return _legacy_dispose


def _const(value: Any) -> Signal[Any]:
    """A Signal with no dependencies, driven by explicit ``touch()``."""
    return Signal({}, lambda _ctx: value)


class TestSignalObserverEquivalence:
    """Behaviour that must be identical before and after the change."""

    def test_subscriber_receives_ctx_and_value_on_touch(self) -> None:
        ctx: dict = {}
        s = Signal(ctx, lambda _c: 1)
        seen: list[tuple[dict, int]] = []
        _subscribe(s, lambda x, v: seen.append((x, v)))

        s.touch()

        assert len(seen) == 1
        assert seen[0][0] is ctx
        assert seen[0][1] == 1

    def test_duplicate_registration_is_deduplicated(self) -> None:
        """The same callable subscribed twice is invoked ONCE per touch.

        Subscriber storage is a ``set``, so registration is by equality. This
        matches ``Cell`` and is deliberately preserved.
        """
        s = _const(0)
        calls: list[int] = []

        def sub(_ctx: dict, value: int) -> None:
            calls.append(value)

        _subscribe(s, sub)
        _subscribe(s, sub)

        s.touch()

        assert calls == [0], "duplicate registration must not double-invoke"

    def test_duplicate_registration_disposes_with_one_disposal(self) -> None:
        """Dedup means two subscribes create ONE registration, so one disposal
        removes it. The second disposer is then a no-op."""
        s = _const(0)
        calls: list[int] = []

        def sub(_ctx: dict, value: int) -> None:
            calls.append(value)

        d1 = _subscribe(s, sub)
        d2 = _subscribe(s, sub)

        d1()
        s.touch()
        assert calls == []

        d2()
        s.touch()
        assert calls == []

    def test_every_subscriber_invoked_exactly_once_per_touch(self) -> None:
        """Order is UNSPECIFIED (the storage is a ``set``), so this asserts on
        the multiset of invocations, never on sequence."""
        s = _const(7)
        calls: list[str] = []
        for name in ("a", "b", "c", "d"):
            _subscribe(s, lambda _ctx, _v, n=name: calls.append(n))

        s.touch()

        assert sorted(calls) == ["a", "b", "c", "d"]

    def test_subscribe_during_notification_defers_to_next_touch(self) -> None:
        """``touch`` iterates a snapshot, so a subscriber registered *during* a
        notification is NOT invoked by that same notification."""
        s = _const(0)
        late_calls: list[int] = []

        def late(_ctx: dict, value: int) -> None:
            late_calls.append(value)

        added = False

        def adder(_ctx: dict, _value: int) -> None:
            nonlocal added
            if not added:
                added = True
                _subscribe(s, late)

        _subscribe(s, adder)

        s.touch()
        assert late_calls == [], "late subscriber must not fire in the same pass"

        s.touch()
        assert late_calls == [0], "late subscriber must fire on the next pass"

    def test_unsubscribe_during_notification_still_fires_this_pass(self) -> None:
        """The snapshot is taken before dispatch, so a subscriber removed
        mid-notification is still invoked in that pass, and skipped after."""
        s = _const(0)
        victim_calls: list[int] = []

        def victim(_ctx: dict, value: int) -> None:
            victim_calls.append(value)

        victim_disposer = _subscribe(s, victim)

        removed = False

        def remover(_ctx: dict, _value: int) -> None:
            nonlocal removed
            if not removed:
                removed = True
                victim_disposer()

        _subscribe(s, remover)

        s.touch()
        assert victim_calls == [0], "snapshot semantics: victim still fires"

        s.touch()
        assert victim_calls == [0], "victim must not fire after removal"

    def test_self_unsubscribe_during_own_notification(self) -> None:
        s = _const(0)
        calls: list[int] = []
        disposer: Callable[[], None] | None = None

        def once(_ctx: dict, value: int) -> None:
            calls.append(value)
            assert disposer is not None
            disposer()

        disposer = _subscribe(s, once)

        s.touch()
        s.touch()

        assert calls == [0]

    def test_disposing_all_subscribers_makes_touch_a_no_op(self) -> None:
        s = _const(3)
        calls: list[int] = []
        disposers = [
            _subscribe(s, lambda _ctx, v, i=i: calls.append(i)) for i in range(5)
        ]

        for d in disposers:
            d()

        s.touch()
        assert calls == []

    def test_resubscribe_after_dispose_is_live_again(self) -> None:
        s = _const(1)
        calls: list[int] = []

        def sub(_ctx: dict, value: int) -> None:
            calls.append(value)

        d = _subscribe(s, sub)
        d()
        _subscribe(s, sub)

        s.touch()
        assert calls == [1]

    def test_eager_recompute_notifies_then_disposal_silences(self) -> None:
        """The Signal-specific path: notification driven by a real dependency
        change rather than an explicit ``touch``.

        The memo guard means only a *changed* value notifies, so this also pins
        that an equal recompute stays silent.
        """
        ctx: dict = {}
        source = Cell(ctx, 1)
        s = Signal(ctx, lambda _c: source.value * 10)
        calls: list[int] = []
        disposer = _subscribe(s, lambda _ctx, v: calls.append(v))

        source.value = 2
        assert calls == [20], "eager recompute must notify subscribers"

        source.value = 2
        assert calls == [20], "equal recompute is suppressed by the memo guard"

        disposer()
        source.value = 3
        assert calls == [20], "disposed subscriber must not be notified"


class TestSignalObserverDisposerContract:
    """The disposer surface added by #lzdartobservercow."""

    def test_subscribe_returns_a_callable_disposer(self) -> None:
        s = _const(0)
        disposer = s.subscribe(lambda _ctx, _v: None)
        assert callable(disposer)

    def test_disposal_is_idempotent(self) -> None:
        """Calling the disposer twice is a no-op, not an error."""
        s = _const(0)
        calls: list[int] = []
        disposer = s.subscribe(lambda _ctx, v: calls.append(v))

        disposer()
        disposer()
        disposer()

        s.touch()
        assert calls == []

    def test_stale_disposer_does_not_remove_a_later_registration(self) -> None:
        """A fired disposer must never affect a *subsequent* registration of an
        equal callable.

        The naive ``_subscribers.discard(sub)`` shape this API replaces got this
        wrong: the stale disposer would silently unsubscribe the new
        registration.
        """
        s = _const(5)
        calls: list[int] = []

        def sub(_ctx: dict, value: int) -> None:
            calls.append(value)

        stale = s.subscribe(sub)
        stale()

        s.subscribe(sub)
        stale()  # stale disposer fires again; must NOT remove the new one

        s.touch()
        assert calls == [5], "stale disposer clobbered a live registration"

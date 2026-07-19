"""Observer semantics for :class:`lazily.Cell` (#lzdartobservercow).

These tests pin the *observable* contract of ``Cell.subscribe`` / ``Cell.touch``
so that changes to the subscriber storage are provably behaviour-preserving.

Every test in :class:`TestCellObserverEquivalence` is written against a shim
(:func:`_subscribe`) that uses the public disposer when ``subscribe`` returns
one and otherwise falls back to the private ``_subscribers.discard`` that
callers were forced to use before the disposer existed. That lets the exact
same file run green against both the pre-change and post-change
implementations, so "no behaviour changed" is a checked claim rather than an
assertion.

:class:`TestCellObserverDisposerContract` covers the *new* surface only.
"""

from collections.abc import Callable
from typing import Any

from lazily import Cell


Sub = Callable[[dict, Any], Any]


def _subscribe(cell: Cell[Any], sub: Sub) -> Callable[[], None]:
    """Subscribe and return a disposer, on old or new implementations.

    The pre-change ``Cell.subscribe`` returned ``None``; the only way to undo a
    registration was to reach into ``_subscribers``. Post-change it returns an
    idempotent disposer. Both are normalised to a disposer here.
    """
    disposer = cell.subscribe(sub)
    if callable(disposer):
        return disposer

    def _legacy_dispose() -> None:
        subs = cell._subscribers
        if subs is not None:
            subs.discard(sub)

    return _legacy_dispose


class TestCellObserverEquivalence:
    """Behaviour that must be identical before and after the change."""

    def test_subscriber_receives_ctx_and_value_on_touch(self) -> None:
        ctx: dict = {}
        c = Cell(ctx, 1)
        seen: list[tuple[dict, int]] = []
        _subscribe(c, lambda x, v: seen.append((x, v)))

        c.touch()

        assert len(seen) == 1
        assert seen[0][0] is ctx
        assert seen[0][1] == 1

    def test_duplicate_registration_is_deduplicated(self) -> None:
        """The same callable subscribed twice is invoked ONCE per touch.

        Subscriber storage is a ``set``, so registration is by equality. This
        is load-bearing for existing callers and is deliberately preserved.
        """
        c = Cell({}, 0)
        calls: list[int] = []

        def sub(_ctx: dict, value: int) -> None:
            calls.append(value)

        _subscribe(c, sub)
        _subscribe(c, sub)

        c.touch()

        assert calls == [0], "duplicate registration must not double-invoke"

    def test_duplicate_registration_disposes_with_one_disposal(self) -> None:
        """Dedup means two subscribes create ONE registration, so one disposal
        removes it. The second disposer is then a no-op."""
        c = Cell({}, 0)
        calls: list[int] = []

        def sub(_ctx: dict, value: int) -> None:
            calls.append(value)

        d1 = _subscribe(c, sub)
        d2 = _subscribe(c, sub)

        d1()
        c.touch()
        assert calls == []

        d2()
        c.touch()
        assert calls == []

    def test_every_subscriber_invoked_exactly_once_per_touch(self) -> None:
        """Order is UNSPECIFIED (the storage is a ``set``), so this asserts on
        the multiset of invocations, never on sequence."""
        c = Cell({}, 7)
        calls: list[str] = []
        for name in ("a", "b", "c", "d"):
            _subscribe(c, lambda _ctx, _v, n=name: calls.append(n))

        c.touch()

        assert sorted(calls) == ["a", "b", "c", "d"]

    def test_subscribe_during_notification_defers_to_next_touch(self) -> None:
        """``touch`` iterates a snapshot, so a subscriber registered *during* a
        notification is NOT invoked by that same notification."""
        c = Cell({}, 0)
        late_calls: list[int] = []

        def late(_ctx: dict, value: int) -> None:
            late_calls.append(value)

        added = False

        def adder(_ctx: dict, _value: int) -> None:
            nonlocal added
            if not added:
                added = True
                _subscribe(c, late)

        _subscribe(c, adder)

        c.touch()
        assert late_calls == [], "late subscriber must not fire in the same pass"

        c.touch()
        assert late_calls == [0], "late subscriber must fire on the next pass"

    def test_unsubscribe_during_notification_still_fires_this_pass(self) -> None:
        """The snapshot is taken before dispatch, so a subscriber removed
        mid-notification is still invoked in that pass, and skipped after."""
        c = Cell({}, 0)
        victim_calls: list[int] = []

        def victim(_ctx: dict, value: int) -> None:
            victim_calls.append(value)

        victim_disposer = _subscribe(c, victim)

        removed = False

        def remover(_ctx: dict, _value: int) -> None:
            nonlocal removed
            if not removed:
                removed = True
                victim_disposer()

        _subscribe(c, remover)

        c.touch()
        assert victim_calls == [0], "snapshot semantics: victim still fires"

        c.touch()
        assert victim_calls == [0], "victim must not fire after removal"

    def test_self_unsubscribe_during_own_notification(self) -> None:
        c = Cell({}, 0)
        calls: list[int] = []
        disposer: Callable[[], None] | None = None

        def once(_ctx: dict, value: int) -> None:
            calls.append(value)
            assert disposer is not None
            disposer()

        disposer = _subscribe(c, once)

        c.touch()
        c.touch()

        assert calls == [0]

    def test_disposing_all_subscribers_makes_touch_a_no_op(self) -> None:
        c = Cell({}, 3)
        calls: list[int] = []
        disposers = [
            _subscribe(c, lambda _ctx, v, i=i: calls.append(i)) for i in range(5)
        ]

        for d in disposers:
            d()

        c.touch()
        assert calls == []

    def test_resubscribe_after_dispose_is_live_again(self) -> None:
        c = Cell({}, 1)
        calls: list[int] = []

        def sub(_ctx: dict, value: int) -> None:
            calls.append(value)

        d = _subscribe(c, sub)
        d()
        _subscribe(c, sub)

        c.touch()
        assert calls == [1]


class TestCellObserverDisposerContract:
    """The disposer surface added by #lzdartobservercow."""

    def test_subscribe_returns_a_callable_disposer(self) -> None:
        c = Cell({}, 0)
        disposer = c.subscribe(lambda _ctx, _v: None)
        assert callable(disposer)

    def test_disposal_is_idempotent(self) -> None:
        """Calling the disposer twice is a no-op, not an error."""
        c = Cell({}, 0)
        calls: list[int] = []
        disposer = c.subscribe(lambda _ctx, v: calls.append(v))

        disposer()
        disposer()
        disposer()

        c.touch()
        assert calls == []

    def test_stale_disposer_does_not_remove_a_later_registration(self) -> None:
        """A fired disposer must never affect a *subsequent* registration of an
        equal callable.

        The naive ``_subscribers.discard(sub)`` workaround this API replaces got
        this wrong: the stale disposer would silently unsubscribe the new
        registration.
        """
        c = Cell({}, 5)
        calls: list[int] = []

        def sub(_ctx: dict, value: int) -> None:
            calls.append(value)

        stale = c.subscribe(sub)
        stale()

        c.subscribe(sub)
        stale()  # stale disposer fires again; must NOT remove the new one

        c.touch()
        assert calls == [5], "stale disposer clobbered a live registration"

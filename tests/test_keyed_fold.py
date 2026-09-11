"""Named keyed fold (``#lzpykeyedfold``).

The property the type exists for: a key has one owner, so a writer can publish
and retract only its own contribution. The bug it prevents is the
last-writer-wins one — several components sharing a cell for "the current
errors", where whoever writes last erases everyone else.
"""

from __future__ import annotations

import pytest

from lazily import (
    KeyAlreadyClaimedError,
    KeyedFold,
    Max,
    SetUnion,
    Sum,
    computed,
    keyed_fold,
)


def _counting_view(ctx: dict, read):
    runs = {"n": 0}

    def body(compute):
        runs["n"] += 1
        return read(compute)

    return computed(ctx, body).eager(), runs


# -- ownership ----------------------------------------------------------------


def test_a_writer_clears_only_its_own_key() -> None:
    ctx: dict = {}
    errors = KeyedFold[str, str, dict[str, str]](ctx)
    intake = errors.claim("intake")
    dispatcher = errors.claim("dispatcher")

    intake.set("connection refused")
    dispatcher.set("timeout")
    assert errors.value == {"intake": "connection refused", "dispatcher": "timeout"}

    intake.clear()
    assert errors.value == {"dispatcher": "timeout"}
    assert dispatcher.value == "timeout"


def test_a_writer_cannot_reach_another_key() -> None:
    errors = KeyedFold[str, str, dict[str, str]]({})
    intake = errors.claim("intake")
    errors.claim("dispatcher").set("timeout")

    intake.set("boom")
    # The writer's whole surface is its own key; there is no argument that
    # names another one.
    assert intake.key == "intake"
    assert errors.value == {"dispatcher": "timeout", "intake": "boom"}


def test_claim_is_exclusive() -> None:
    errors = KeyedFold[str, str, dict[str, str]]({})
    errors.claim("intake")
    with pytest.raises(KeyAlreadyClaimedError, match="already claimed"):
        errors.claim("intake")
    assert errors.is_claimed("intake")


def test_writer_shares_a_handle_without_claiming() -> None:
    errors = KeyedFold[str, str, dict[str, str]]({})
    first = errors.writer("intake")
    assert errors.writer("intake") is first
    assert not errors.is_claimed("intake")
    errors.claim("intake")  # claiming afterwards is still allowed
    assert errors.is_claimed("intake")


def test_release_clears_and_frees_the_claim() -> None:
    errors = KeyedFold[str, str, dict[str, str]]({})
    intake = errors.claim("intake")
    intake.set("boom")

    assert intake.release() is True
    assert not errors.is_claimed("intake")
    assert errors.value == {}
    errors.claim("intake")  # re-claimable


# -- reactivity ---------------------------------------------------------------


def test_summary_reader_is_not_invalidated_by_an_equal_write() -> None:
    ctx: dict = {}
    errors = KeyedFold[str, str, dict[str, str]](ctx)
    intake = errors.claim("intake")
    intake.set("boom")

    view, runs = _counting_view(ctx, lambda c: errors.observe(c))
    assert view.value == {"intake": "boom"}
    before = runs["n"]

    intake.set("boom")
    assert runs["n"] == before  # guarded at the entry cell


def test_summary_reader_sees_a_clear() -> None:
    ctx: dict = {}
    errors = KeyedFold[str, str, dict[str, str]](ctx)
    intake = errors.claim("intake")
    dispatcher = errors.claim("dispatcher")
    intake.set("boom")
    dispatcher.set("timeout")

    view, _ = _counting_view(ctx, lambda c: errors.observe(c))
    assert view.value == {"intake": "boom", "dispatcher": "timeout"}

    intake.clear()
    assert view.value == {"dispatcher": "timeout"}


def test_a_key_reader_is_not_invalidated_by_a_sibling() -> None:
    ctx: dict = {}
    errors = KeyedFold[str, str, dict[str, str]](ctx)
    intake = errors.claim("intake")
    dispatcher = errors.claim("dispatcher")
    intake.set("boom")

    view, runs = _counting_view(ctx, lambda c: errors.get("intake", c))
    assert view.value == "boom"
    assert runs["n"] == 1

    dispatcher.set("timeout")
    assert runs["n"] == 1
    assert view.value == "boom"


def test_summary_guard_suppresses_an_unchanged_summary() -> None:
    """A summary that ignores a key is not invalidated by that key moving."""
    ctx: dict = {}
    fold = KeyedFold[str, int, int](ctx, lambda entries: len(entries))
    a = fold.claim("a")
    b = fold.claim("b")
    a.set(1)
    b.set(2)

    view, runs = _counting_view(ctx, lambda c: fold.observe(c))
    assert view.value == 2
    before = runs["n"]

    a.set(99)  # the count did not move
    assert view.value == 2
    assert runs["n"] == before


def test_lazy_summary_defers_the_fold() -> None:
    ctx: dict = {}
    calls = {"n": 0}

    def summarize(entries):
        calls["n"] += 1
        return dict(entries)

    fold = KeyedFold[str, int, dict](ctx, summarize, eager=False)
    fold.claim("a").set(1)
    assert calls["n"] == 0  # nothing folded yet

    assert fold.value == {"a": 1}
    assert calls["n"] == 1


# -- fold algebra -------------------------------------------------------------


def test_merge_folds_under_the_policy_per_key() -> None:
    fold = KeyedFold[str, int, int]({}, lambda e: sum(e.values()), policy=Sum)
    a = fold.claim("a")
    b = fold.claim("b")

    a.merge(2)  # first write seeds
    a.merge(3)
    b.merge(10)

    assert fold.entries() == {"a": 5, "b": 10}
    assert fold.value == 15

    a.clear()
    assert fold.value == 10


def test_merge_seeds_a_fresh_key_with_the_operand() -> None:
    fold = KeyedFold[str, frozenset, frozenset](
        {},
        lambda e: frozenset().union(*e.values()) if e else frozenset(),
        policy=SetUnion,
    )
    fold.claim("a").merge(frozenset({1, 2}))
    fold.claim("b").merge(frozenset({2, 3}))
    assert fold.value == frozenset({1, 2, 3})


def test_policy_is_exposed() -> None:
    fold = KeyedFold[str, int, int](
        {}, lambda e: max(e.values(), default=0), policy=Max
    )
    assert fold.policy is Max
    writer = fold.claim("a")
    writer.merge(5)
    writer.merge(2)  # Max keeps 5
    assert writer.value == 5


def test_set_bypasses_the_policy() -> None:
    fold = KeyedFold[str, int, int]({}, lambda e: sum(e.values()), policy=Sum)
    writer = fold.claim("a")
    writer.merge(5)
    writer.set(1)  # a replace, not a fold
    assert writer.value == 1


# -- surface ------------------------------------------------------------------


def test_reads_and_membership() -> None:
    fold = KeyedFold[str, int, dict]({})
    a = fold.claim("a")
    assert a.present is False
    assert a.value is None
    assert fold.get("a") is None
    assert fold.contains("a") is False

    a.set(1)
    assert a.present is True
    assert fold.keys() == ["a"]
    assert fold.contains("a") is True
    assert fold.entry_cell("a") is not None
    assert fold.entry_cell("missing") is None


def test_clear_reports_whether_an_entry_was_present() -> None:
    fold = KeyedFold[str, int, dict]({})
    writer = fold.claim("a")
    assert writer.clear() is False
    writer.set(1)
    assert writer.clear() is True


def test_call_and_repr() -> None:
    fold = keyed_fold({})
    fold.claim("a").set(1)
    assert fold() == {"a": 1}
    assert "KeyedFold" in repr(fold)
    assert "KeyedFoldWriter" in repr(fold.writer("a"))


def test_dispose_drops_entries_and_claims() -> None:
    fold = KeyedFold[str, int, dict]({})
    fold.claim("a").set(1)
    fold.dispose()
    assert fold.keys() == []
    assert not fold.is_claimed("a")

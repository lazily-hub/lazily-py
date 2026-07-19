"""Reactive-graph conformance runner against canonical lazily-spec fixtures.

Executes the JSON fixtures in ``lazily-spec/conformance/reactive-graph``
**directly** — this is a fixture *interpreter*, not a hand-transcribed replay.
Transcriptions and bundled copies drift from the spec (``lazily-kt`` bundles
copies under ``src/test/resources/conformance/`` and they have already diverged),
so the canonical files are read from the sibling checkout and never vendored
here.

Fixture path resolution mirrors ``lazily-rs`` (``#lzspecconf``,
``tests/collections_conformance.rs``): one sibling-relative ``SPEC_DIR``
constant, skip with an explicit message when it is absent, and a CI guard that
asserts the directory exists so the skip can never silently pass — see the
"Assert canonical lazily-spec fixtures are present" step in
``.github/workflows/precommit.yml``.

Currently covered: the five ``observer_*`` fixtures, which pin the normative
observer contract of ``lazily-spec/docs/reactive-graph.md``
(``#lzdartobservercow``) — registration-order firing, independent duplicate
registrations, deferred subscribe-during-notify, immediate
unsubscribe-during-notify, and latching disposers. The remaining fixtures in the
directory exercise a disposal/teardown-scope vocabulary (``computed``, ``read``,
``begin_scope`` / ``end_scope`` / ``disarm``, ``effect``, ``fanout``, ``churn``,
``dispose_stale_handle``, and the ``dependents_of`` / ``dependencies_of`` graph
assertions) that lazily-py does not yet expose; the runner reports them as
skipped with the unsupported op named, rather than pretending to cover them.
The parametrization globs the whole directory, so a newly-added observer fixture
is picked up with no edit here.

**Registration labels.** A fixture labels *registrations* (``obs_x1``,
``obs_x2``) and separately names the *callback* they share
(``"callback": "x"``). Where two registrations share a callable, a Python
callback cannot report which registration invoked it — so the runner records the
callback label and maps the fixture's ``observed_order`` /``observed_counts``
(registration ids) through each registration's callback binding before
comparing. Registrations with no explicit ``callback`` get a callback unique to
them, so those compare id-for-id. Order and invocation counts — everything the
fixtures assert — are preserved, and the shared-callable case is exactly what
makes the no-deduplication clause testable in Python.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from lazily import Cell


# Sibling-relative, mirroring lazily-rs `const SPEC_DIR` (#lzspecconf). Resolved
# against the repository root rather than the process cwd so the suite runs the
# same from any directory.
SPEC_DIR = "../lazily-spec/conformance/reactive-graph"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC_PATH = (_REPO_ROOT / SPEC_DIR).resolve()

_SUPPORTED_OPS = frozenset({"cell", "subscribe", "unsubscribe", "set_cell", "dispose"})
_SUPPORTED_EXPECT = frozenset(
    {"observed_order", "observed_count", "observed_counts", "readable", "error", "note"}
)


class Unsupported(Exception):
    """A fixture uses vocabulary this binding does not model yet."""


def _fixture_paths() -> list[Path]:
    if not _SPEC_PATH.is_dir():
        return []
    return sorted(_SPEC_PATH.glob("*.json"))


def _unsupported_reason(fixture: dict[str, Any]) -> str | None:
    """Pre-flight the whole fixture so an unsupported one skips before any op runs."""
    steps = fixture.get("steps")
    if not steps:
        return "fixture declares no `steps`"
    for step in steps:
        pending = [step["op"]]
        while pending:
            op = pending.pop()
            kind = op.get("type")
            if kind not in _SUPPORTED_OPS:
                return f"unsupported op `{kind}`"
            pending.extend(op.get("on_notify") or [])
        for key, value in (step.get("expect") or {}).items():
            if key not in _SUPPORTED_EXPECT:
                return f"unsupported expectation `{key}`"
            if key == "error" and value is not None:
                return f"unsupported expectation `error: {value!r}`"
    return None


class _Runner:
    """Interprets the reactive-graph fixture op vocabulary against lazily.Cell."""

    def __init__(self) -> None:
        self.cells: dict[str, Cell[Any]] = {}
        # Registration id -> its disposer, and -> the callback label it records.
        self.disposers: dict[str, Any] = {}
        self.callback_of: dict[str, str] = {}
        # Callback label -> the single shared callable, so two registrations
        # naming the same `callback` really are the same object. That is what
        # makes an equality/identity-keyed observer collection collapse them,
        # which is the defect the no-dedup fixture exists to catch.
        self.callables: dict[str, Any] = {}
        self.observed: list[str] = []
        self._prefix_counters: Counter[str] = Counter()

    # -- op execution ----------------------------------------------------

    def run_step(self, step: dict[str, Any]) -> None:
        self.observed.clear()
        self.exec_op(step["op"])
        expect = step.get("expect")
        if expect is not None:
            self.check(expect, step)

    def exec_op(self, op: dict[str, Any]) -> None:
        kind = op["type"]
        if kind == "cell":
            self.cells[op["id"]] = Cell({}, op["value"])
        elif kind == "set_cell":
            self.cells[op["id"]].set(op["value"])
        elif kind == "dispose":
            # `Cell` has no explicit teardown method — a cell is torn down by
            # dropping it. Removing it from the environment is the observable
            # equivalent: it stops being readable, and (as the fixture asserts)
            # nothing is notified on the way out.
            del self.cells[op["id"]]
        elif kind == "subscribe":
            self.exec_subscribe(op)
        elif kind == "unsubscribe":
            for _ in range(op.get("times", 1)):
                self.disposers[op["id"]]()
        else:  # pragma: no cover - pre-flighted by _unsupported_reason
            raise Unsupported(f"unsupported op `{kind}`")

    def exec_subscribe(self, op: dict[str, Any]) -> None:
        registration = op.get("id")
        if registration is None:
            prefix = op["id_prefix"]
            registration = f"{prefix}_{self._prefix_counters[prefix]}"
            self._prefix_counters[prefix] += 1

        on_notify = op.get("on_notify")
        label = op.get("callback", registration)
        if on_notify and "callback" in op:
            raise Unsupported("a shared `callback` with `on_notify` is ambiguous")

        callback = self.callables.get(label)
        if callback is None:
            callback = self._make_callback(label, on_notify, op.get("on_notify_once"))
            self.callables[label] = callback

        self.callback_of[registration] = label
        self.disposers[registration] = self.cells[op["cell"]].subscribe(callback)

    def _make_callback(
        self, label: str, on_notify: list[dict[str, Any]] | None, once: Any
    ) -> Any:
        fired = False

        def _observe(_ctx: dict, _value: Any) -> None:
            nonlocal fired
            self.observed.append(label)
            if on_notify and not (once and fired):
                fired = True
                for nested in on_notify:
                    self.exec_op(nested)

        return _observe

    # -- assertions ------------------------------------------------------

    def check(self, expect: dict[str, Any], step: dict[str, Any]) -> None:
        note = expect.get("note", "")
        detail = f"{step['op']}\n{note}"

        if "observed_order" in expect:
            want = [self.callback_of[obs] for obs in expect["observed_order"]]
            assert self.observed == want, detail

        if "observed_count" in expect:
            assert len(self.observed) == expect["observed_count"], detail

        if "observed_counts" in expect:
            want_counts: Counter[str] = Counter()
            for obs, count in expect["observed_counts"].items():
                want_counts[self.callback_of[obs]] += count
            assert Counter(self.observed) == want_counts, detail

        if "readable" in expect:
            for cell_id, readable in expect["readable"].items():
                assert (cell_id in self.cells) is readable, detail


@pytest.mark.parametrize(
    "fixture_path",
    _fixture_paths(),
    ids=lambda p: p.stem,  # type: ignore[misc]
)
def test_reactive_graph_fixture(fixture_path: Path) -> None:
    fixture = json.loads(fixture_path.read_text())
    reason = _unsupported_reason(fixture)
    if reason is not None:
        pytest.skip(f"{fixture_path.name}: {reason}")

    runner = _Runner()
    for step in fixture["steps"]:
        runner.run_step(step)


def test_spec_fixtures_are_present() -> None:
    """The canonical fixtures must be reachable, or the suite tests nothing.

    Mirrors lazily-rs `spec_fixtures_present` (#lzspecconf): skip loudly rather
    than fail locally, because a checkout without the sibling is a normal
    developer state. CI closes the hole — `.github/workflows/precommit.yml`
    clones lazily-spec and then asserts this directory exists, so the skip
    cannot mask an untested run there.
    """
    if not _SPEC_PATH.is_dir():
        pytest.skip(f"skipping: {SPEC_DIR} absent - run with the lazily-spec sibling")
    assert _fixture_paths(), f"{SPEC_DIR} present but holds no fixtures"

"""Reactive-graph conformance runner against canonical lazily-spec fixtures.

Executes the JSON fixtures in ``lazily-spec/conformance/reactive-graph``
**directly** — this is a fixture *interpreter*, not a hand-transcribed replay.
Transcriptions and bundled copies drift from the spec, so the canonical files
are read from the sibling checkout and never vendored here.

Fixture path resolution mirrors ``lazily-rs``
(``tests/reactive_graph_conformance.rs``, ``#lzspecconf``): one sibling-relative
``SPEC_DIR`` constant, skip with an explicit message when it is absent, and a CI
guard that asserts the directory exists so the skip can never silently pass —
see the "Assert canonical lazily-spec fixtures are present" step in
``.github/workflows/precommit.yml``.

## Replayed against every context lazily-py ships

The corpus runs against :class:`~lazily.cell.Cell`/:class:`~lazily.slot.Slot`
(the synchronous graph), :class:`~lazily.thread_safe.ThreadSafeContext`, and
:class:`~lazily.async_context.AsyncContext`. Replaying only the default context
is exactly how the ``lazily-dart`` and ``lazily-go`` invalidation-cascade
defects hid: both were correct synchronously and broken asynchronously. The
async path — where staleness is tracked by revision counters and in-flight
state rather than a pull chain — is the one the transitive-depth fixture was
written to discriminate (``#lzdartobservercow``).

## Coverage and skips

``lazily-py`` models ``cell``, ``computed``, ``read`` and ``set_cell``. The
remaining fixtures exercise a teardown-scope vocabulary (``begin_scope`` /
``end_scope`` / ``disarm``, ``effect``, ``dispose``, ``fanout``, ``churn``,
``dispose_stale_handle``) and degree introspection (``dependents_of`` /
``dependencies_of``) that this binding does not expose. Those are skipped with
the unsupported op named — never silently — and the skip ledger below is
asserted exactly, so gaining support for an op fails the build until the ledger
is updated rather than letting new coverage arrive unnoticed.

## Positive assertion (``#lzspecconf``)

An absence guard is not enough: a runner that skips everything must fail. For
each model this asserts (a) the fixture set on disk matches :data:`FIXTURES`
exactly, (b) the set actually replayed matches :data:`EXPECTED_REPLAYED`
exactly, (c) the observed skips match :data:`EXPECTED_SKIPS` exactly, and (d) a
non-zero number of fixtures, ops and assertions actually executed. A runner
that can pass while executing nothing is the anti-pattern this file exists to
kill.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from lazily import Cell, slot
from lazily.async_context import AsyncCellHandle, AsyncContext, AsyncSlotHandle
from lazily.thread_safe import ThreadSafeContext


# Sibling-relative, mirroring lazily-rs `const SPEC_DIR` (#lzspecconf). Resolved
# against the repository root rather than the process cwd so the suite runs the
# same from any directory. No bundled copies.
SPEC_DIR = "../lazily-spec/conformance/reactive-graph"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC_PATH = (_REPO_ROOT / SPEC_DIR).resolve()

# The canonical fixture set. Asserted against the directory listing so a fixture
# added or renamed upstream fails loudly instead of going unrun.
FIXTURES = (
    "churn_returns_to_baseline.json",
    "cross_scope_teardown_hazard.json",
    "disarm_disposes_nothing.json",
    "dispose_detaches_edges_both_directions.json",
    "read_after_dispose_is_an_error.json",
    "recycled_id_inherits_nothing.json",
    "scope_teardown_equals_fold_of_disposals.json",
    "scoping_bounds_teardown_not_visibility.json",
    "transitive_invalidation_reaches_depth.json",
)

# Fixtures this binding executes end to end. Asserted exactly: a fixture that
# stops replaying, and a fixture that starts replaying, both fail the build.
EXPECTED_REPLAYED = frozenset({"transitive_invalidation_reaches_depth.json"})

# Fixtures skipped, with the *first* unsupported op or expectation encountered.
# Each entry is a gap in lazily-py's public surface, not a relaxation of the
# fixture. Asserted exactly, so implementing an op forces its entry to be
# removed rather than letting the new coverage arrive silently.
EXPECTED_SKIPS = {
    "churn_returns_to_baseline.json": "unsupported op `fanout`",
    "cross_scope_teardown_hazard.json": "unsupported op `begin_scope`",
    "disarm_disposes_nothing.json": "unsupported op `begin_scope`",
    "dispose_detaches_edges_both_directions.json": "unsupported op `effect`",
    "read_after_dispose_is_an_error.json": "unsupported op `dispose`",
    "recycled_id_inherits_nothing.json": "unsupported op `fanout`",
    # `scenarios`-shaped: `observationally_equal` is a relation between two op
    # streams, which the `steps` replay loop cannot express. Its steps also need
    # `begin_scope`/`disarm`.
    "scope_teardown_equals_fold_of_disposals.json": "unsupported fixture shape `scenarios`",
    "scoping_bounds_teardown_not_visibility.json": "unsupported op `begin_scope`",
}

_SUPPORTED_OPS = frozenset({"cell", "computed", "read", "set_cell"})
_SUPPORTED_EXPECT = frozenset({"value", "read", "note"})


# --------------------------------------------------------------------------- #
# Execution models — one per context lazily-py ships.
#
# Every method is `async` so a single replay loop drives all three; the two
# synchronous models simply never suspend. Each model owns the whole op
# vocabulary, so a divergence between contexts shows up as a fixture failure
# under one model and not the others.
# --------------------------------------------------------------------------- #


class SyncModel:
    """The synchronous graph: :class:`Cell` + :class:`slot`, ambient tracking."""

    NAME = "Context"

    def __init__(self) -> None:
        self.ctx: dict = {}
        self.cells: dict[str, Cell[Any]] = {}
        self.slots: dict[str, Any] = {}

    def _node_value(self, node_id: str) -> Any:
        """Read inside a computation, so the dependency edge is registered."""
        if node_id in self.cells:
            return self.cells[node_id].value
        return self.slots[node_id](self.ctx)

    async def cell(self, node_id: str, value: Any) -> None:
        self.cells[node_id] = Cell(self.ctx, value)

    async def computed(self, node_id: str, reads: list[str], offset: Any) -> None:
        def compute(_ctx: dict) -> Any:
            total = offset
            for dep in reads:
                total += self._node_value(dep)
            return total

        self.slots[node_id] = slot(compute)

    async def read(self, node_id: str) -> Any:
        return self._node_value(node_id)

    async def set_cell(self, node_id: str, value: Any) -> None:
        self.cells[node_id].set(value)


class ThreadSafeModel(SyncModel):
    """The same graph driven through :class:`ThreadSafeContext`'s write path."""

    NAME = "ThreadSafeContext"

    def __init__(self) -> None:
        super().__init__()
        self.ts = ThreadSafeContext()

    async def set_cell(self, node_id: str, value: Any) -> None:
        self.ts.set_cell(self.cells[node_id], value)


class AsyncModel:
    """The async graph: :class:`AsyncContext`, explicit edge registration.

    The context that matters most here — revision-counter staleness plus
    in-flight state is where the pull chain that makes the lazy strategy work
    can break without the synchronous path noticing.
    """

    NAME = "AsyncContext"

    def __init__(self) -> None:
        self.ctx = AsyncContext()
        self.cells: dict[str, AsyncCellHandle[Any]] = {}
        self.slots: dict[str, AsyncSlotHandle[Any]] = {}

    async def cell(self, node_id: str, value: Any) -> None:
        self.cells[node_id] = self.ctx.cell(value)

    async def computed(self, node_id: str, reads: list[str], offset: Any) -> None:
        async def compute(cc: Any) -> Any:
            total = offset
            for dep in reads:
                if dep in self.cells:
                    total += cc.get_cell(self.cells[dep])
                else:
                    total += await cc.get_async(self.slots[dep])
            return total

        self.slots[node_id] = self.ctx.computed_async(compute)

    async def read(self, node_id: str) -> Any:
        if node_id in self.cells:
            return self.cells[node_id].get()
        return await self.ctx.get_async(self.slots[node_id])

    async def set_cell(self, node_id: str, value: Any) -> None:
        self.ctx.set_cell(self.cells[node_id], value)


MODELS = (SyncModel, ThreadSafeModel, AsyncModel)


# --------------------------------------------------------------------------- #
# Fixture loading, pre-flight, replay
# --------------------------------------------------------------------------- #


def _fixture_paths() -> list[Path]:
    if not _SPEC_PATH.is_dir():
        return []
    return sorted(_SPEC_PATH.glob("*.json"))


def _unsupported_reason(fixture: dict[str, Any]) -> str | None:
    """Pre-flight the whole fixture so an unsupported one skips before any op runs.

    Returns the reason — naming the unsupported op or expectation — or ``None``
    when every step is executable.
    """
    shape = fixture.get("shape")
    if shape is None:
        return "fixture declares no `shape`"
    if shape != "steps":
        return f"unsupported fixture shape `{shape}`"
    steps = fixture.get("steps")
    if not steps:
        return "fixture declares no `steps`"
    for step in steps:
        op = step["op"]
        kind = op.get("type")
        if kind not in _SUPPORTED_OPS:
            return f"unsupported op `{kind}`"
        for key in step.get("expect") or {}:
            if key not in _SUPPORTED_EXPECT:
                return f"unsupported expectation `{key}`"
    return None


class Report:
    __slots__ = ("checks", "ops")

    def __init__(self) -> None:
        self.ops = 0
        self.checks = 0


async def _replay(model: Any, name: str, steps: list[dict[str, Any]]) -> Report:
    report = Report()
    for index, step in enumerate(steps):
        op = step["op"]
        kind = op["type"]
        where = f"{model.NAME}/{name}#{index} {op}"

        if kind == "cell":
            await model.cell(op["id"], op["value"])
            value = None
        elif kind == "computed":
            await model.computed(op["id"], op["reads"], op["offset"])
            value = None
        elif kind == "read":
            value = await model.read(op["id"])
        elif kind == "set_cell":
            await model.set_cell(op["id"], op["value"])
            value = None
        else:  # pragma: no cover - pre-flighted by _unsupported_reason
            raise AssertionError(f"unsupported op `{kind}`")
        report.ops += 1

        expect = step.get("expect")
        if not expect:
            continue
        note = expect.get("note", "")

        if "value" in expect:
            assert value == expect["value"], (
                f"{where}\nexpected value {expect['value']}, got {value}\n{note}"
            )
            report.checks += 1

        if "read" in expect:
            for node_id, want in expect["read"].items():
                got = await model.read(node_id)
                assert got == want, (
                    f"{where}\nread({node_id}): expected {want}, got {got}\n{note}"
                )
                report.checks += 1

    return report


def _run_corpus(model_cls: Any) -> None:
    model_name = model_cls.NAME
    if not _SPEC_PATH.is_dir():
        pytest.skip(
            f"reactive_graph_conformance[{model_name}]: {SPEC_DIR} not found — "
            f"clone lazily-spec as a sibling to run the reactive-graph fixtures"
        )

    # The fixture set on disk must be exactly the one this runner knows about,
    # so an upstream addition cannot arrive unexecuted.
    on_disk = {p.name for p in _fixture_paths()}
    assert on_disk == set(FIXTURES), (
        "reactive-graph fixture set drifted; every fixture must be accounted for "
        f"by this runner (on disk: {sorted(on_disk)}, known: {sorted(FIXTURES)})"
    )

    replayed: set[str] = set()
    skipped: dict[str, str] = {}
    total_ops = 0
    total_checks = 0

    for name in FIXTURES:
        fixture = json.loads((_SPEC_PATH / name).read_text())
        reason = _unsupported_reason(fixture)
        if reason is not None:
            skipped[name] = reason
            continue

        model = model_cls()
        report = asyncio.run(_replay(model, name, fixture["steps"]))
        assert report.ops > 0, f"{model_name}/{name}: replayed zero ops"
        assert report.checks > 0, f"{model_name}/{name}: replayed zero assertions"
        total_ops += report.ops
        total_checks += report.checks
        replayed.add(name)

    # Ledgers: both directions fail loudly. A fixture that stops replaying, one
    # that starts, or a skip reason that changes, all break the build.
    assert replayed == set(EXPECTED_REPLAYED), (
        f"{model_name}: replayed set drifted (replayed: {sorted(replayed)}, "
        f"expected: {sorted(EXPECTED_REPLAYED)})"
    )
    assert skipped == EXPECTED_SKIPS, (
        f"{model_name}: skip ledger is stale — update EXPECTED_SKIPS "
        f"(observed: {skipped}, documented: {EXPECTED_SKIPS})"
    )

    # Positive assertion: the runner must have actually executed something.
    assert len(replayed) > 0, f"{model_name}: replayed zero fixtures"
    assert total_ops > 0, f"{model_name}: executed zero ops"
    assert total_checks > 0, f"{model_name}: executed zero assertions"


@pytest.mark.parametrize("model_cls", MODELS, ids=lambda m: m.NAME)  # type: ignore[misc]
def test_reactive_graph_conformance(model_cls: Any) -> None:
    """Replay the reactive-graph corpus against one context."""
    _run_corpus(model_cls)


def test_every_shipped_context_is_covered() -> None:
    """Guards the requirement that gives this runner its value.

    Replaying only the default context is how the dart and go async cascade
    defects hid. If lazily-py gains a context, it must be added to MODELS.
    """
    covered = {m.NAME for m in MODELS}
    assert covered == {"Context", "ThreadSafeContext", "AsyncContext"}, (
        f"MODELS does not cover every shipped context: {sorted(covered)}"
    )


def test_spec_fixtures_are_present() -> None:
    """The canonical fixtures must be reachable, or the suite tests nothing.

    Mirrors lazily-rs (#lzspecconf): skip loudly rather than fail locally,
    because a checkout without the sibling is a normal developer state. CI
    closes the hole — ``.github/workflows/precommit.yml`` clones lazily-spec and
    then asserts this directory exists, so the skip cannot mask an untested run
    there.
    """
    if not _SPEC_PATH.is_dir():
        pytest.skip(f"skipping: {SPEC_DIR} absent - run with the lazily-spec sibling")
    assert _fixture_paths(), f"{SPEC_DIR} present but holds no fixtures"

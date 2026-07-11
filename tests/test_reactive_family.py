"""ReactiveFamily materialization-mode tests (``#lzmatmode``).

Mirrors the ``lazily-rs`` ``reactive_family.rs`` unit tests and the
``materialization_conformance.rs`` harness, driven by the canonical fixtures in
``lazily-spec/conformance/materialization/``. Exercises the laws proved in
``lazily-formal``'s ``Materialization`` module against the Python
``ReactiveFamily`` vehicle: observational transparency (eager vs lazy),
deferral-not-deallocation present-set monotonicity, and entry-kind orthogonal to
mode (input cells always materialized / derived slots deferred under lazy).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lazily.reactive_family import EntryKind, MaterializationMode, ReactiveFamily


_LOCAL_FIXTURES = Path(__file__).resolve().parent / "conformance" / "materialization"
_SPEC_FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "lazily-spec"
    / "conformance"
    / "materialization"
)


def load_fixture(name: str) -> dict:
    spec_path = _SPEC_FIXTURES / name
    path = spec_path if spec_path.exists() else _LOCAL_FIXTURES / name
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Unit tests (mirror reactive_family.rs)
# ---------------------------------------------------------------------------


def test_default_mode_is_eager() -> None:
    assert MaterializationMode.default() is MaterializationMode.EAGER


def test_eager_materializes_all_up_front() -> None:
    fam = ReactiveFamily.eager({}, [0, 1, 2, 5, 9], lambda k: k * 3)
    assert fam.present_count() == 5
    assert all(fam.is_present(k) for k in (0, 1, 2, 5, 9))


def test_lazy_defers_slots_until_read() -> None:
    fam = ReactiveFamily.lazy({}, [0, 1, 2, 5, 9], lambda k: k * 3)
    assert fam.present_count() == 0
    assert not fam.is_present(5)
    # First read materializes just that key ("materialize on pull").
    assert fam.observe(5) == 15
    assert fam.is_present(5)
    assert fam.present_keys() == [5]


def test_eager_and_lazy_observe_identically() -> None:
    ctx: dict = {}
    eager = ReactiveFamily.eager(ctx, [0, 1, 2, 5, 9], lambda k: k * 3)
    lazy = ReactiveFamily.lazy(ctx, [0, 1, 2, 5, 9], lambda k: k * 3)
    for k in (0, 1, 2, 5, 9):
        assert eager.observe(k) == lazy.observe(k)


def test_present_set_is_monotone_across_reads() -> None:
    fam = ReactiveFamily.lazy({}, [1, 2, 3, 4, 5], lambda k: k * 2)
    sizes = []
    for k in (2, 4, 2, 5):
        fam.observe(k)
        sizes.append(fam.present_count())
    # Re-reading 2 does not re-materialize; sizes are non-decreasing.
    assert sizes == [1, 2, 2, 3]
    assert fam.present_keys() == [2, 4, 5]


def test_cell_family_materialized_in_every_mode() -> None:
    for mode in (MaterializationMode.EAGER, MaterializationMode.LAZY):
        fam = ReactiveFamily.cell_family({}, ["a", "b", "c"], lambda _k: 0, mode=mode)
        assert fam.entry_kind is EntryKind.CELL
        # Cells are always present at build, even under lazy.
        assert fam.present_count() == 3


def test_cell_family_entries_are_writable_inputs() -> None:
    fam = ReactiveFamily.cell_family({}, [7], lambda k: k)
    handle = fam.get(7)
    assert handle.get() == 7
    handle.set(100)
    assert fam.observe(7) == 100


def test_new_is_eager_alias() -> None:
    fam = ReactiveFamily.new({}, [1, 2], lambda k: k)
    assert fam.mode is MaterializationMode.EAGER
    assert fam.present_count() == 2


def test_slot_family_defers_under_lazy() -> None:
    fam = ReactiveFamily.slot_family(
        {}, [1, 2, 3], lambda k: k, mode=MaterializationMode.LAZY
    )
    assert fam.entry_kind is EntryKind.SLOT
    assert fam.present_count() == 0


def test_observe_is_reactive_when_factory_reads_a_cell() -> None:
    # A derived slot entry whose factory reads a Cell re-derives when the cell
    # changes — reactivity is orthogonal to materialization.
    from lazily import Cell, slot

    ctx: dict = {}
    src = Cell(ctx, 10)
    fam = ReactiveFamily.eager(ctx, [1], lambda k: src.value + k)
    seen = []

    reader = slot(lambda c: fam.observe(1))
    watcher = slot(lambda c: seen.append(reader(c)))
    watcher(ctx)
    assert seen == [11]
    src.set(100)
    watcher(ctx)
    assert seen == [11, 101]


# ---------------------------------------------------------------------------
# Conformance fixtures (mirror materialization_conformance.rs)
# ---------------------------------------------------------------------------


def _val_lookup(spec_val: dict) -> dict[str, int]:
    return {k: int(v) for k, v in spec_val.items()}


def _check_val_fixture(name: str) -> dict:
    fixture = load_fixture(name)
    assert fixture["kind"] == "ReactiveFamily"
    expected = fixture["expected"]
    assert expected["default_mode"] == "eager"
    assert MaterializationMode.default() is MaterializationMode.EAGER

    vals = _val_lookup(fixture["spec"]["val"])
    keys = list(vals.keys())
    lookup = vals.__getitem__

    ctx: dict = {}
    eager = ReactiveFamily.eager(ctx, keys, lookup)
    lazy = ReactiveFamily.lazy(ctx, keys, lookup)

    # eager_materializes_all
    assert eager.present_count() == len(keys)
    assert set(eager.present_keys()) == set(expected["eager_present"])
    # lazy defers every derived slot: nothing present at build.
    assert lazy.present_count() == 0

    # observe_canonical / eager_lazy_observationally_equivalent
    for k, want in expected["observe"].items():
        assert eager.observe(k) == want
        assert lazy.observe(k) == want

    return fixture


def test_conformance_observational_transparency() -> None:
    fixture = _check_val_fixture("observational_transparency.json")
    expected = fixture["expected"]

    vals = _val_lookup(fixture["spec"]["val"])
    lazy = ReactiveFamily.lazy({}, list(vals.keys()), vals.__getitem__)
    for k in fixture["reads"]:
        lazy.observe(k)
    assert set(lazy.present_keys()) == set(expected["lazy_present_after_reads"])


def test_conformance_deferral_not_deallocation() -> None:
    fixture = _check_val_fixture("deferral_not_deallocation.json")
    expected = fixture["expected"]

    vals = _val_lookup(fixture["spec"]["val"])
    lazy = ReactiveFamily.lazy({}, list(vals.keys()), vals.__getitem__)

    got_sizes = []
    for k in fixture["reads"]:
        lazy.observe(k)
        got_sizes.append(lazy.present_count())
    assert got_sizes == expected["present_after_each_read"]

    lazy_present = set(lazy.present_keys())
    assert lazy_present == set(expected["lazy_present_after_reads"])
    assert lazy_present.issubset(set(expected["eager_present"]))


def test_conformance_entry_kind_orthogonal_to_mode() -> None:
    fixture = load_fixture("entry_kind_orthogonal_to_mode.json")
    expected = fixture["expected"]
    assert expected["default_mode"] == "eager"

    entries = fixture["spec"]["entries"]
    cell_keys = [k for k, e in entries.items() if e["kind"] == "cell"]
    slot_keys = [k for k, e in entries.items() if e["kind"] == "slot"]
    vals = {k: int(e["val"]) for k, e in entries.items()}
    lookup = vals.__getitem__

    ctx: dict = {}

    # A single ReactiveFamily fixes one handle kind, so a mixed-kind fixture is
    # modelled by a cell family over the cell entries and a slot family over the
    # slot entries — sharing one logical key space.
    eager_cells = ReactiveFamily.cell_family(ctx, cell_keys, lookup)
    eager_slots = ReactiveFamily.slot_family(ctx, slot_keys, lookup)
    assert eager_cells.entry_kind is EntryKind.CELL
    assert eager_slots.entry_kind is EntryKind.SLOT
    eager_present = set(eager_cells.present_keys()) | set(eager_slots.present_keys())
    assert eager_present == set(expected["eager_present"])

    lazy_cells = ReactiveFamily.cell_family(
        ctx, cell_keys, lookup, mode=MaterializationMode.LAZY
    )
    lazy_slots = ReactiveFamily.slot_family(
        ctx, slot_keys, lookup, mode=MaterializationMode.LAZY
    )
    # Cells present at build, slots deferred.
    assert set(lazy_cells.present_keys()) == set(expected["lazy_present_at_build"])
    assert lazy_slots.present_keys() == []

    for k in fixture["reads"]:
        if k in slot_keys:
            lazy_slots.observe(k)
        else:
            lazy_cells.observe(k)
    lazy_after = set(lazy_cells.present_keys()) | set(lazy_slots.present_keys())
    assert lazy_after == set(expected["lazy_present_after_reads"])

    # Observational transparency across kinds.
    for k, want in expected["observe"].items():
        if k in cell_keys:
            assert eager_cells.observe(k) == want
            assert lazy_cells.observe(k) == want
        else:
            assert eager_slots.observe(k) == want
            assert lazy_slots.observe(k) == want


@pytest.mark.parametrize(
    "name",
    [
        "observational_transparency.json",
        "deferral_not_deallocation.json",
        "entry_kind_orthogonal_to_mode.json",
    ],
)
def test_fixture_loads_and_is_reactive_family(name: str) -> None:
    fixture = load_fixture(name)
    assert fixture["kind"] == "ReactiveFamily"
    assert fixture["model"] == "ReactiveFamily"
    assert fixture["expected"]["default_mode"] == "eager"

"""Replay lazily-spec/conformance/view. Skip when that area is absent."""

from __future__ import annotations

import json

import pytest
from conformance_assert import corpus_subdir, instrument

from lazily.view import View

_SPEC = corpus_subdir("view")
_FIXTURES = (
    "batch_send.json",
    "counter.json",
    "field_change.json",
    "malformed_rejected.json",
)


def _skip_if_absent() -> None:
    if not _SPEC.is_dir():
        pytest.skip(f"conformance/view is absent at {_SPEC}")


def _load(name: str) -> dict:
    path = _SPEC / name
    return instrument(json.loads(path.read_text()), name=f"view/{name}")


def test_view_fixture_set() -> None:
    _skip_if_absent()
    found = tuple(sorted(path.name for path in _SPEC.glob("*.json")))
    assert found == _FIXTURES


@pytest.mark.parametrize("name", ["counter.json", "field_change.json", "batch_send.json"])
def test_view_conformance(name: str) -> None:
    _skip_if_absent()
    fixture = _load(name)
    assert fixture["kind"] == "View"
    view = View(fixture["view"])
    assert view.signals_json() == fixture["signals"]
    assert view.expressions_json() == fixture["expressions"]
    assert view.layout_json() == fixture["layout"]
    assert fixture["trace"] == []
    assert view.chart_json() == fixture["chart"]
    for index, step in enumerate(fixture["steps"]):
        trace = view.dispatch(step["node"], step["event"], step["value"])
        label = f"{name} step {index}"
        assert view.signals_json() == step["signals"], label
        assert view.expressions_json() == step["expressions"], label
        assert trace == step["trace"], label
        assert view.chart_json() == step["chart"], label


def test_view_malformed_corpus_is_rejected() -> None:
    _skip_if_absent()
    fixture = _load("malformed_rejected.json")
    assert fixture["cases"]
    for case in fixture["cases"]:
        with pytest.raises(ValueError):
            View(case["view"])

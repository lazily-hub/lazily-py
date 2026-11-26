"""Graph IR runtime. Pins the lazily-rs reference scenarios."""

from __future__ import annotations

import pytest

from lazily.graph_ir import (
    ComputedNode,
    Edge,
    EdgeKind,
    GraphEdit,
    GraphIr,
    GraphIrError,
    GraphRuntime,
    Policy,
)


COUNTER = """
{
  "kind": "graph",
  "ir": "1",
  "id": "counter",
  "nodes": [
    {"id": "count", "kind": "source", "init": 1},
    {
      "id": "doubled",
      "kind": "computed",
      "eager": true,
      "expr": {"op": "mul", "args": [{"ref": "count"}, {"lit": 2}]}
    },
    {
      "id": "log",
      "kind": "effect",
      "body": {"do": "eval", "expr": {"ref": "doubled"}}
    }
  ]
}
"""

COUNTER_EDIT = """
{
  "kind": "edit",
  "ir": "1",
  "ops": [
    {
      "op": "replace",
      "node": {
        "id": "doubled",
        "kind": "computed",
        "expr": {"op": "add", "args": [{"ref": "count"}, {"lit": 10}]}
      }
    },
    {"op": "add", "node": {"id": "label", "kind": "source", "init": "n"}},
    {
      "op": "add",
      "node": {
        "id": "shown",
        "kind": "computed",
        "expr": {"op": "concat", "args": [{"ref": "label"}, {"lit": ":"}]}
      }
    },
    {"op": "remove", "id": "log"}
  ]
}
"""

COUNTER_SOURCE = """\
graph counter ir 1
source count = 1
computed doubled eager = mul(ref(count), lit(2))
effect log = eval(ref(doubled))
"""

DYNAMIC = """
{
  "kind": "graph", "ir": "1",
  "nodes": [
    {"id": "flag", "kind": "source", "init": false},
    {"id": "left", "kind": "source", "init": "L"},
    {"id": "right", "kind": "source", "init": "R"},
    {"id": "picked", "kind": "computed", "expr": {
      "op": "if", "args": [{"ref": "flag"}, {"ref": "left"}, {"ref": "right"}]
    }}
  ]
}
"""

POLICY = """
{
  "kind": "graph", "ir": "1",
  "nodes": [
    {"id": "tick", "kind": "source", "init": 0},
    {"id": "total", "kind": "source", "init": 0, "policy": "sum"},
    {"id": "bump", "kind": "effect", "body": {"do": "seq", "steps": [
      {"do": "eval", "expr": {"ref": "tick"}},
      {"do": "merge", "target": "total", "expr": {"lit": 1}}
    ]}}
  ]
}
"""

STRUCTURES = """
{
  "kind": "graph", "ir": "1",
  "nodes": [
    {"id": "row", "kind": "source", "init": {"name": "ada", "n": 2}},
    {"id": "name", "kind": "computed", "expr": {
      "op": "get", "args": [{"ref": "row"}, {"lit": "name"}]
    }},
    {"id": "n", "kind": "computed", "expr": {
      "op": "get", "args": [{"ref": "row"}, {"lit": "n"}]
    }},
    {"id": "tag", "kind": "computed", "expr": {
      "op": "concat", "args": [{"ref": "name"}, {"lit": "#"}]
    }},
    {"id": "width", "kind": "computed", "expr": {"op": "len", "args": [{"ref": "name"}]}},
    {"id": "pair", "kind": "computed", "expr": {
      "op": "array", "args": [{"ref": "name"}, {"ref": "n"}]
    }},
    {"id": "wrapped", "kind": "computed", "expr": {
      "object": [{"key": "name", "value": {"ref": "name"}}]
    }}
  ]
}
"""

CYCLE = """
{
  "kind": "graph", "ir": "1",
  "nodes": [
    {"id": "a", "kind": "computed", "expr": {"ref": "b"}},
    {"id": "b", "kind": "computed", "expr": {"ref": "a"}}
  ]
}
"""


def _runtime(text: str) -> GraphRuntime:
    return GraphRuntime.load_json(text)


def _edit(text: str) -> GraphEdit:
    return GraphEdit.from_json_str(text)


def _read(from_id: str, to: str) -> Edge:
    return Edge(from_id, EdgeKind.READ, to)


def test_counter_lowers_then_an_edit_replaces_the_graph() -> None:
    doc = GraphIr.from_json_str(COUNTER)
    assert doc.id() == "counter"
    doubled = doc.node("doubled")
    assert isinstance(doubled, ComputedNode)
    assert doubled.eager
    assert doubled.expr.op == "mul"
    assert doc.static_edges() == [
        _read("doubled", "count"),
        _read("log", "doubled"),
    ]
    assert doc.emit_source() == COUNTER_SOURCE
    again = GraphIr.from_json(doc.to_json())
    assert again.emit_source() == COUNTER_SOURCE

    rt = GraphRuntime.lower(doc)
    assert rt.get("doubled") == 2
    assert len(rt.effect_trace()) == 1
    assert rt.effect_trace()[0].id == "log"
    assert rt.effect_trace()[0].result == 2
    assert rt.observed_reads("doubled") == {"count"}
    assert rt.observed_reads("log") == {"doubled"}
    assert rt.is_eager("doubled") is True
    assert rt.source_policy("count") is Policy.KEEP_LATEST

    before_write = rt.emit_source()
    rt.write_source("count", 4)
    assert rt.get("doubled") == 8
    assert len(rt.effect_trace()) == 2
    assert rt.effect_trace()[1].result == 8
    assert rt.emit_source() == before_write

    rt.apply(_edit(COUNTER_EDIT))
    assert rt.get("doubled") == 14
    assert rt.get("shown") == "n:"
    assert not rt.contains("log")
    assert rt.is_eager("doubled") is False
    trace_len = len(rt.effect_trace())
    rt.write_source("count", 5)
    assert rt.get("doubled") == 15
    assert len(rt.effect_trace()) == trace_len
    emitted = rt.emit_source()
    assert emitted == (
        "graph counter ir 1\n"
        "source count = 1\n"
        "computed doubled = add(ref(count), lit(10))\n"
        'source label = "n"\n'
        'computed shown = concat(ref(label), lit(":"))\n'
    )
    assert "mul(" not in emitted
    assert "effect log" not in emitted


def test_dynamic_if_observes_only_the_taken_branch() -> None:
    rt = _runtime(DYNAMIC)
    assert rt.static_edges() == [
        _read("picked", "flag"),
        _read("picked", "left"),
        _read("picked", "right"),
    ]
    assert rt.get("picked") == "R"
    assert rt.observed_reads("picked") == {"flag", "right"}
    rt.write_source("flag", True)
    assert rt.get("picked") == "L"
    assert rt.observed_reads("picked") == {"flag", "left"}


def test_sum_policy_folds_on_merge_and_skips_an_equal_write() -> None:
    rt = _runtime(POLICY)
    assert "policy sum" in rt.emit_source()
    assert "merge(total, lit(1))" in rt.emit_source()
    assert rt.get("total") == 1
    assert rt.effect_trace()[0].id == "bump"
    assert rt.effect_trace()[0].result == [0, 1]
    assert rt.source_policy("total") is Policy.SUM
    rt.write_source("tick", 1)
    assert rt.get("total") == 2
    assert len(rt.effect_trace()) == 2
    rt.write_source("tick", 1)
    assert len(rt.effect_trace()) == 2
    assert rt.get("total") == 2


def test_structures_read_fields_length_and_objects() -> None:
    rt = _runtime(STRUCTURES)
    assert rt.get("name") == "ada"
    assert rt.get("n") == 2
    assert rt.get("tag") == "ada#"
    assert rt.get("width") == 3
    assert rt.get("pair") == ["ada", 2]
    assert rt.get("wrapped") == {"name": "ada"}


def test_cycle_is_schema_shaped_and_rejected_on_load() -> None:
    with pytest.raises(GraphIrError, match="cycle in read edges"):
        GraphIr.from_json_str(CYCLE)
    with pytest.raises(GraphIrError, match="cycle in read edges"):
        GraphRuntime.load_json(CYCLE)


def test_snapshot_envelope_is_not_a_graph_document() -> None:
    with pytest.raises(GraphIrError, match="graph"):
        GraphIr.from_json_str(
            '{"Snapshot":{"epoch":0,"nodes":[],"edges":[],"roots":[]}}'
        )


def test_null_init_and_ordered_boolean_policies() -> None:
    rt = _runtime(
        """
        {
          "kind": "graph", "ir": "1",
          "nodes": [
            {"id": "empty", "kind": "source", "init": null},
            {"id": "hi", "kind": "source", "init": 3, "policy": "max"},
            {"id": "lo", "kind": "source", "init": 3, "policy": "min"},
            {"id": "both", "kind": "source", "init": true, "policy": "and"},
            {"id": "either", "kind": "source", "init": false, "policy": "or"},
            {"id": "fold", "kind": "effect", "body": {"do": "seq", "steps": [
              {"do": "merge", "target": "hi", "expr": {"lit": 1}},
              {"do": "merge", "target": "hi", "expr": {"lit": 5}},
              {"do": "merge", "target": "hi", "expr": {"lit": 5}},
              {"do": "merge", "target": "lo", "expr": {"lit": 1}},
              {"do": "merge", "target": "both", "expr": {"lit": false}},
              {"do": "merge", "target": "either", "expr": {"lit": true}}
            ]}}
          ]
        }
        """
    )
    assert rt.get("empty") is None
    assert rt.get("hi") == 5
    assert rt.get("lo") == 1
    assert rt.get("both") is False
    assert rt.get("either") is True
    assert rt.source_policy("hi") is Policy.MAX
    assert rt.source_policy("lo") is Policy.MIN
    assert rt.source_policy("both") is Policy.AND
    assert rt.source_policy("either") is Policy.OR


def test_and_short_circuits_and_div_by_zero_faults_on_get() -> None:
    rt = _runtime(
        """
        {
          "kind": "graph", "ir": "1",
          "nodes": [
            {"id": "flag", "kind": "source", "init": false},
            {"id": "n", "kind": "source", "init": 1},
            {"id": "bad", "kind": "computed", "expr": {
              "op": "div", "args": [{"ref": "n"}, {"lit": 0}]
            }},
            {"id": "gate", "kind": "computed", "expr": {
              "op": "and", "args": [{"ref": "flag"}, {"ref": "bad"}]
            }}
          ]
        }
        """
    )
    assert rt.get("gate") is False
    assert rt.observed_reads("gate") == {"flag"}
    with pytest.raises(GraphIrError, match="division by zero"):
        rt.get("bad")


def test_remove_of_a_live_dependency_keeps_the_previous_graph() -> None:
    rt = _runtime(
        """
        {"kind":"graph","ir":"1","nodes":[
          {"id":"a","kind":"source","init":1},
          {"id":"b","kind":"computed","expr":{"ref":"a"}}
        ]}
        """
    )
    with pytest.raises(GraphIrError, match="edit op 0"):
        rt.apply(_edit('{"kind":"edit","ir":"1","ops":[{"op":"remove","id":"a"}]}'))
    assert rt.contains("a") and rt.contains("b")
    assert rt.get("b") == 1


def test_forward_ref_inside_one_edit_applies_nothing() -> None:
    rt = _runtime('{"kind":"graph","ir":"1","nodes":[]}')
    with pytest.raises(GraphIrError, match="edit op 0"):
        rt.apply(
            _edit(
                """
                {"kind":"edit","ir":"1","ops":[
                  {"op":"add","node":{"id":"a","kind":"computed","expr":{"ref":"b"}}},
                  {"op":"add","node":{"id":"b","kind":"source","init":1}}
                ]}
                """
            )
        )
    assert not rt.contains("a") and not rt.contains("b")
    assert rt.emit_source() == "graph ir 1\n"


def test_replace_source_resets_the_live_value_to_the_new_init() -> None:
    rt = _runtime(
        """
        {"kind":"graph","ir":"1","id":"g","nodes":[
          {"id":"count","kind":"source","init":1}
        ]}
        """
    )
    rt.write_source("count", 9)
    assert rt.get("count") == 9
    assert rt.emit_source() == "graph g ir 1\nsource count = 1\n"
    rt.apply(
        _edit(
            """
            {"kind":"edit","ir":"1","ops":[
              {"op":"replace","node":{"id":"count","kind":"source","init":0}}
            ]}
            """
        )
    )
    assert rt.get("count") == 0
    assert rt.emit_source() == "graph g ir 1\nsource count = 0\n"


def test_when_set_writes_and_a_skipped_when_records_null() -> None:
    rt = _runtime(
        """
        {"kind":"graph","ir":"1","nodes":[
          {"id":"on","kind":"source","init":true},
          {"id":"value","kind":"source","init":0},
          {"id":"apply","kind":"effect","body":{
            "do":"when","cond":{"ref":"on"},
            "then":{"do":"set","target":"value","expr":{"lit":7}}
          }}
        ]}
        """
    )
    assert rt.get("value") == 7
    assert rt.effect_trace()[0].result == 7

    rt = _runtime(
        """
        {"kind":"graph","ir":"1","nodes":[
          {"id":"on","kind":"source","init":false},
          {"id":"value","kind":"source","init":0},
          {"id":"apply","kind":"effect","body":{
            "do":"when","cond":{"ref":"on"},
            "then":{"do":"set","target":"value","expr":{"lit":7}}
          }}
        ]}
        """
    )
    assert rt.get("value") == 0
    assert rt.effect_trace()[0].result is None


def test_empty_graph_accepts_an_add() -> None:
    rt = _runtime('{"kind":"graph","ir":"1","nodes":[]}')
    rt.apply(
        _edit(
            """
            {"kind":"edit","ir":"1","ops":[
              {"op":"add","node":{"id":"x","kind":"source","init":1}}
            ]}
            """
        )
    )
    assert rt.get("x") == 1
    assert rt.emit_source() == "graph ir 1\nsource x = 1\n"


def test_rejections() -> None:
    rejected = [
        '{"kind":"graph","ir":"2","nodes":[]}',
        '{"kind":"graph","ir":"1","nodes":[{"id":"1count","kind":"source","init":0}]}',
        '{"kind":"graph","ir":"1","nodes":[{"id":"s","kind":"source","init":0,"policy":"product"}]}',
        '{"kind":"graph","ir":"1","nodes":[{"id":"c","kind":"computed","eager":"yes","expr":{"lit":1}}]}',
        '{"kind":"graph","ir":"1","nodes":[{"id":"c","kind":"computed","expr":{"op":"add","args":[{"lit":1}]}}]}',
        '{"kind":"graph","ir":"1","nodes":[{"id":"c","kind":"computed","expr":{"op":"pow","args":[{"lit":2},{"lit":3}]}}]}',
        '{"kind":"graph","ir":"1","nodes":[{"id":"e","kind":"effect","body":"log"}]}',
        '{"kind":"graph","ir":"1","nodes":[{"id":"s","kind":"source"}]}',
        '{"kind":"graph","ir":"1","nodes":[{"id":"c","kind":"computed","expr":{"fn":"sum"}}]}',
        '{"kind":"edit","ir":"1","ops":[]}',
        '{"kind":"edit","ir":"1","ops":[{"op":"remove"}]}',
    ]
    for text in rejected:
        with pytest.raises(GraphIrError):
            if '"kind":"edit"' in text:
                GraphEdit.from_json_str(text)
            else:
                GraphIr.from_json_str(text)


def test_feedback_stops_at_the_drain_budget() -> None:
    doc = GraphIr.from_json_str(
        """
        {"kind":"graph","ir":"1","nodes":[
          {"id":"n","kind":"source","init":0},
          {"id":"loop","kind":"effect","body":{
            "do":"set","target":"n","expr":{"op":"add","args":[{"ref":"n"},{"lit":1}]}
          }}
        ]}
        """
    )
    rt = GraphRuntime.lower(doc, drain_budget=4)
    assert rt.last_drain_exhaustion == 4
    assert rt.get("n") == 4

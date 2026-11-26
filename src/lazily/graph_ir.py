"""Graph IR interpreter.

A definition document (lazily-spec ``schemas/graph-ir.json``) parses into an
expression tree, lowers onto sources, computeds, and effects, and can emit
``canonical-source``. Edits add, replace, and remove nodes while the runtime
holds the document. This is compute, not a Snapshot or Delta.

The behavior matches the lazily-rs reference behind the ``graph-ir`` feature:
``GraphIr.from_json_str``, ``GraphRuntime.lower``, ``GraphRuntime.apply``,
``GraphIr.static_edges``, and ``GraphIr.emit_source``.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


IR_VERSION = "1"
I64_MIN = -9223372036854775808
I64_MAX = 9223372036854775807
DRAIN_BUDGET = 100_000
_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_UNARY = frozenset({"neg", "not", "len"})
_BINARY = frozenset(
    {
        "add",
        "sub",
        "mul",
        "div",
        "mod",
        "min",
        "max",
        "eq",
        "ne",
        "lt",
        "le",
        "gt",
        "ge",
        "concat",
        "get",
        "and",
        "or",
    }
)
_OPS = _UNARY | _BINARY | {"if", "array"}


class GraphIrError(Exception):
    """A parse, check, edit, or value fault."""


class Policy(Enum):
    KEEP_LATEST = "keep-latest"
    SUM = "sum"
    MAX = "max"
    MIN = "min"
    AND = "and"
    OR = "or"

    def as_str(self) -> str:
        return self.value


class EdgeKind(Enum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, order=True)
class Edge:
    from_id: str
    kind: EdgeKind
    to: str


@dataclass(frozen=True)
class EffectRun:
    id: str
    result: Any


@dataclass(frozen=True)
class Lit:
    value: Any


@dataclass(frozen=True)
class Ref:
    id: str


@dataclass(frozen=True)
class Call:
    op: str
    args: tuple[Any, ...]


@dataclass(frozen=True)
class ObjectField:
    key: str
    value: Any


@dataclass(frozen=True)
class ObjectExpr:
    fields: tuple[ObjectField, ...]


@dataclass(frozen=True)
class EvalStmt:
    expr: Any


@dataclass(frozen=True)
class SetStmt:
    target: str
    expr: Any


@dataclass(frozen=True)
class MergeStmt:
    target: str
    expr: Any


@dataclass(frozen=True)
class SeqStmt:
    steps: tuple[Any, ...]


@dataclass(frozen=True)
class WhenStmt:
    cond: Any
    then_stmt: Any
    else_stmt: Any | None


@dataclass(frozen=True)
class SourceNode:
    id: str
    init: Any
    policy: Policy


@dataclass(frozen=True)
class ComputedNode:
    id: str
    expr: Any
    eager: bool


@dataclass(frozen=True)
class EffectNode:
    id: str
    body: Any


Node = SourceNode | ComputedNode | EffectNode
Expr = Lit | Ref | Call | ObjectExpr
Stmt = EvalStmt | SetStmt | MergeStmt | SeqStmt | WhenStmt


@dataclass(frozen=True)
class AddOp:
    node: Node


@dataclass(frozen=True)
class ReplaceOp:
    node: Node


@dataclass(frozen=True)
class RemoveOp:
    id: str


EditOp = AddOp | ReplaceOp | RemoveOp


def json_text(value: Any) -> str:
    """Compact JSON. Object keys are sorted, matching serde_json's default map."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return "null"


def _freeze(value: Any) -> Any:
    if value is None or isinstance(value, (str, float, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return [_freeze(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _freeze(item) for key, item in value.items()}
    raise GraphIrError(f"value is not JSON: {value!r}")


def _json_eq(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, int) and isinstance(right, int):
        return left == right
    if isinstance(left, float) and isinstance(right, float):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_eq(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            return False
        return all(_json_eq(left[key], right[key]) for key in left)
    return False


def _as_object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GraphIrError(f"{what} must be an object")
    return value


def _allow(obj: dict[str, Any], what: str, keys: tuple[str, ...]) -> None:
    allowed = set(keys)
    for key in obj:
        if key not in allowed:
            raise GraphIrError(f"{what}: unknown field `{key}`")


def _require(obj: dict[str, Any], field_name: str, what: str) -> Any:
    if field_name not in obj:
        raise GraphIrError(f"{what} requires `{field_name}`")
    return obj[field_name]


def _require_str(obj: dict[str, Any], field_name: str, what: str) -> str:
    value = _require(obj, field_name, what)
    if not isinstance(value, str):
        raise GraphIrError(
            f"{what} `{field_name}` must be a string, got {json_text(value)}"
        )
    return value


def _parse_id(text: str, what: str) -> str:
    if not _ID_RE.match(text) or len(text) > 128:
        raise GraphIrError(f"{what} `{text}` is not a graph IR id")
    return text


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise GraphIrError(f"graph document is not JSON: {error}") from None


def _require_version(obj: dict[str, Any], what: str) -> None:
    version = _require_str(obj, "ir", what)
    if version != IR_VERSION:
        raise GraphIrError(f"unsupported graph IR version `{version}`")


class GraphIr:
    """A parsed graph definition. The tree is the document, not a live cell."""

    def __init__(self, graph_id: str | None, nodes: list[Node]) -> None:
        self._id = graph_id
        self._nodes = nodes
        self._index: dict[str, int] = {}
        self._reindex()

    @staticmethod
    def from_json_str(text: str) -> GraphIr:
        return GraphIr.from_json(_parse_json(text))

    @staticmethod
    def from_json(value: Any) -> GraphIr:
        obj = _as_object(value, "graph document")
        _allow(obj, "graph document", ("kind", "ir", "id", "nodes"))
        kind = _require_str(obj, "kind", "graph document")
        if kind != "graph":
            raise GraphIrError(f'graph document kind must be "graph", got `{kind}`')
        _require_version(obj, "graph document")
        graph_id = _optional_id(obj, "id", "graph id")
        raw_nodes = _require(obj, "nodes", "graph document")
        if not isinstance(raw_nodes, list):
            raise GraphIrError("graph nodes must be an array")
        nodes: list[Node] = []
        seen: set[str] = set()
        for raw in raw_nodes:
            node = _parse_node(raw)
            if node.id in seen:
                raise GraphIrError(f"duplicate node id `{node.id}`")
            seen.add(node.id)
            nodes.append(node)
        document = GraphIr(graph_id, nodes)
        document.check()
        return document

    def to_json(self) -> dict[str, Any]:
        obj: dict[str, Any] = {"kind": "graph", "ir": IR_VERSION}
        if self._id is not None:
            obj["id"] = self._id
        obj["nodes"] = [_node_json(node) for node in self._nodes]
        return obj

    def id(self) -> str | None:
        return self._id

    def nodes(self) -> tuple[Node, ...]:
        return tuple(self._nodes)

    def node(self, node_id: str) -> Node | None:
        slot = self._index.get(node_id)
        if slot is None:
            return None
        return self._nodes[slot]

    def static_edges(self) -> list[Edge]:
        edges: set[Edge] = set()
        for node in self._nodes:
            reads, writes = _edge_sets(node)
            for to in reads:
                edges.add(Edge(node.id, EdgeKind.READ, to))
            for to in writes:
                edges.add(Edge(node.id, EdgeKind.WRITE, to))
        return sorted(edges)

    def emit_source(self) -> str:
        if self._id is None:
            out = f"graph ir {IR_VERSION}\n"
        else:
            out = f"graph {self._id} ir {IR_VERSION}\n"
        for node in self._nodes:
            out += _emit_node(node) + "\n"
        return out

    def check(self) -> None:
        self._check_refs()
        self._check_inits()
        self._check_cycles()

    def clone(self) -> GraphIr:
        copied = GraphIr(self._id, list(self._nodes))
        return copied

    def apply_edit_op(self, op: EditOp) -> None:
        if isinstance(op, AddOp):
            if op.node.id in self._index:
                raise GraphIrError(
                    f"add requires a new id, `{op.node.id}` already exists"
                )
            self._nodes.append(op.node)
        elif isinstance(op, ReplaceOp):
            slot = self._index.get(op.node.id)
            if slot is None:
                raise GraphIrError(f"replace requires an existing node `{op.node.id}`")
            self._nodes[slot] = op.node
        elif isinstance(op, RemoveOp):
            slot = self._index.get(op.id)
            if slot is None:
                raise GraphIrError(f"remove requires an existing node `{op.id}`")
            del self._nodes[slot]
        self._reindex()
        self.check()

    def _reindex(self) -> None:
        self._index = {}
        for slot, node in enumerate(self._nodes):
            if node.id in self._index:
                raise GraphIrError(f"duplicate node id `{node.id}`")
            self._index[node.id] = slot

    def _check_refs(self) -> None:
        for node in self._nodes:
            reads, writes = _edge_sets(node)
            for to in reads:
                target = self.node(to)
                if isinstance(target, EffectNode):
                    raise GraphIrError(
                        f"node `{node.id}` reads effect `{to}`, which has no value"
                    )
                if target is None:
                    raise GraphIrError(f"node `{node.id}` reads unknown node `{to}`")
            for to in writes:
                target = self.node(to)
                if target is None:
                    raise GraphIrError(f"node `{node.id}` writes unknown node `{to}`")
                if not isinstance(target, SourceNode):
                    raise GraphIrError(f"node `{node.id}` writes non-source `{to}`")

    def _check_inits(self) -> None:
        for node in self._nodes:
            if isinstance(node, SourceNode):
                try:
                    _check_policy_init(node.policy, node.init)
                except GraphIrError as error:
                    raise GraphIrError(f"source `{node.id}`: {error}") from None

    def _check_cycles(self) -> None:
        color: dict[str, int] = {}
        stack: list[str] = []

        def visit(node_id: str) -> None:
            state = color.get(node_id, 0)
            if state == 1:
                start = stack.index(node_id) if node_id in stack else 0
                cycle = [*stack[start:], node_id]
                raise GraphIrError("cycle in read edges: " + " -> ".join(cycle))
            if state == 2:
                return
            color[node_id] = 1
            stack.append(node_id)
            target = self.node(node_id)
            if target is not None:
                reads, _writes = _edge_sets(target)
                for dep in sorted(reads):
                    if self.node(dep) is not None:
                        visit(dep)
            stack.pop()
            color[node_id] = 2

        for node in self._nodes:
            visit(node.id)


def _optional_id(obj: dict[str, Any], field_name: str, what: str) -> str | None:
    if field_name not in obj:
        return None
    value = obj[field_name]
    if not isinstance(value, str):
        raise GraphIrError(f"{what} must be a string, got {json_text(value)}")
    return _parse_id(value, what)


class GraphEdit:
    """One or more add, replace, or remove ops, applied in order."""

    def __init__(self, ops: list[EditOp]) -> None:
        self._ops = ops

    @staticmethod
    def from_json_str(text: str) -> GraphEdit:
        return GraphEdit.from_json(_parse_json(text))

    @staticmethod
    def from_json(value: Any) -> GraphEdit:
        obj = _as_object(value, "graph edit")
        _allow(obj, "graph edit", ("kind", "ir", "ops"))
        kind = _require_str(obj, "kind", "graph edit")
        if kind != "edit":
            raise GraphIrError(f'graph edit kind must be "edit", got `{kind}`')
        _require_version(obj, "graph edit")
        raw_ops = _require(obj, "ops", "graph edit")
        if not isinstance(raw_ops, list):
            raise GraphIrError("graph edit ops must be an array")
        if not raw_ops:
            raise GraphIrError("graph edit ops must not be empty")
        return GraphEdit([_parse_edit_op(raw) for raw in raw_ops])

    def ops(self) -> tuple[EditOp, ...]:
        return tuple(self._ops)


def _edge_sets(node: Node) -> tuple[set[str], set[str]]:
    reads: set[str] = set()
    writes: set[str] = set()
    if isinstance(node, ComputedNode):
        _collect_expr(node.expr, reads)
    elif isinstance(node, EffectNode):
        _collect_stmt(node.body, reads, writes)
    return reads, writes


def _collect_expr(expr: Expr, reads: set[str]) -> None:
    if isinstance(expr, Ref):
        reads.add(expr.id)
    elif isinstance(expr, Call):
        for arg in expr.args:
            _collect_expr(arg, reads)
    elif isinstance(expr, ObjectExpr):
        for item in expr.fields:
            _collect_expr(item.value, reads)


def _collect_stmt(stmt: Stmt, reads: set[str], writes: set[str]) -> None:
    if isinstance(stmt, EvalStmt):
        _collect_expr(stmt.expr, reads)
    elif isinstance(stmt, (SetStmt, MergeStmt)):
        writes.add(stmt.target)
        _collect_expr(stmt.expr, reads)
    elif isinstance(stmt, SeqStmt):
        for step in stmt.steps:
            _collect_stmt(step, reads, writes)
    elif isinstance(stmt, WhenStmt):
        _collect_expr(stmt.cond, reads)
        _collect_stmt(stmt.then_stmt, reads, writes)
        if stmt.else_stmt is not None:
            _collect_stmt(stmt.else_stmt, reads, writes)


def _emit_node(node: Node) -> str:
    if isinstance(node, SourceNode):
        line = f"source {node.id} = {json_text(node.init)}"
        if node.policy is not Policy.KEEP_LATEST:
            line += f" policy {node.policy.as_str()}"
        return line
    if isinstance(node, ComputedNode):
        if node.eager:
            return f"computed {node.id} eager = {_emit_expr(node.expr)}"
        return f"computed {node.id} = {_emit_expr(node.expr)}"
    return f"effect {node.id} = {_emit_stmt(node.body)}"


def _emit_expr(expr: Expr) -> str:
    if isinstance(expr, Lit):
        return f"lit({json_text(expr.value)})"
    if isinstance(expr, Ref):
        return f"ref({expr.id})"
    if isinstance(expr, Call):
        rendered = ", ".join(_emit_expr(arg) for arg in expr.args)
        return f"{expr.op}({rendered})"
    rendered = ", ".join(
        f"{json_text(item.key)}: {_emit_expr(item.value)}" for item in expr.fields
    )
    return f"object({rendered})"


def _emit_stmt(stmt: Stmt) -> str:
    if isinstance(stmt, EvalStmt):
        return f"eval({_emit_expr(stmt.expr)})"
    if isinstance(stmt, SetStmt):
        return f"set({stmt.target}, {_emit_expr(stmt.expr)})"
    if isinstance(stmt, MergeStmt):
        return f"merge({stmt.target}, {_emit_expr(stmt.expr)})"
    if isinstance(stmt, SeqStmt):
        rendered = ", ".join(_emit_stmt(step) for step in stmt.steps)
        return f"seq({rendered})"
    if stmt.else_stmt is None:
        return f"when({_emit_expr(stmt.cond)}, {_emit_stmt(stmt.then_stmt)})"
    return (
        f"when({_emit_expr(stmt.cond)}, {_emit_stmt(stmt.then_stmt)}, "
        f"{_emit_stmt(stmt.else_stmt)})"
    )


def _node_json(node: Node) -> dict[str, Any]:
    if isinstance(node, SourceNode):
        obj: dict[str, Any] = {
            "id": node.id,
            "kind": "source",
            "init": _freeze(node.init),
        }
        if node.policy is not Policy.KEEP_LATEST:
            obj["policy"] = node.policy.as_str()
        return obj
    if isinstance(node, ComputedNode):
        obj = {"id": node.id, "kind": "computed"}
        if node.eager:
            obj["eager"] = True
        obj["expr"] = _expr_json(node.expr)
        return obj
    return {"id": node.id, "kind": "effect", "body": _stmt_json(node.body)}


def _expr_json(expr: Expr) -> dict[str, Any]:
    if isinstance(expr, Lit):
        return {"lit": _freeze(expr.value)}
    if isinstance(expr, Ref):
        return {"ref": expr.id}
    if isinstance(expr, Call):
        return {"op": expr.op, "args": [_expr_json(arg) for arg in expr.args]}
    return {
        "object": [
            {"key": item.key, "value": _expr_json(item.value)} for item in expr.fields
        ]
    }


def _stmt_json(stmt: Stmt) -> dict[str, Any]:
    if isinstance(stmt, EvalStmt):
        return {"do": "eval", "expr": _expr_json(stmt.expr)}
    if isinstance(stmt, SetStmt):
        return {"do": "set", "target": stmt.target, "expr": _expr_json(stmt.expr)}
    if isinstance(stmt, MergeStmt):
        return {
            "do": "merge",
            "target": stmt.target,
            "expr": _expr_json(stmt.expr),
        }
    if isinstance(stmt, SeqStmt):
        return {"do": "seq", "steps": [_stmt_json(step) for step in stmt.steps]}
    obj: dict[str, Any] = {
        "do": "when",
        "cond": _expr_json(stmt.cond),
        "then": _stmt_json(stmt.then_stmt),
    }
    if stmt.else_stmt is not None:
        obj["else"] = _stmt_json(stmt.else_stmt)
    return obj


def _parse_node(value: Any) -> Node:
    obj = _as_object(value, "node")
    kind = _require_str(obj, "kind", "node")
    if kind == "source":
        _allow(obj, "source", ("id", "kind", "init", "policy"))
        node_id = _parse_id(_require_str(obj, "id", "source"), "source id")
        if "init" not in obj:
            raise GraphIrError("source requires `init`")
        policy = _parse_policy(obj, node_id)
        return SourceNode(node_id, _freeze(obj["init"]), policy)
    if kind == "computed":
        _allow(obj, "computed", ("id", "kind", "expr", "eager"))
        node_id = _parse_id(_require_str(obj, "id", "computed"), "computed id")
        eager = _parse_eager(obj, node_id)
        expr = _parse_expr(_require(obj, "expr", "computed"))
        return ComputedNode(node_id, expr, eager)
    if kind == "effect":
        _allow(obj, "effect", ("id", "kind", "body"))
        node_id = _parse_id(_require_str(obj, "id", "effect"), "effect id")
        body = _parse_stmt(_require(obj, "body", "effect"))
        return EffectNode(node_id, body)
    raise GraphIrError(f"unknown node kind `{kind}`")


def _parse_policy(obj: dict[str, Any], node_id: str) -> Policy:
    if "policy" not in obj:
        return Policy.KEEP_LATEST
    value = obj["policy"]
    if not isinstance(value, str):
        raise GraphIrError(
            f"source `{node_id}` policy must be a string, got {json_text(value)}"
        )
    for policy in Policy:
        if policy.value == value:
            return policy
    raise GraphIrError(f"unknown source policy `{value}`")


def _parse_eager(obj: dict[str, Any], node_id: str) -> bool:
    if "eager" not in obj:
        return False
    value = obj["eager"]
    if isinstance(value, bool):
        return value
    raise GraphIrError(
        f"computed `{node_id}` eager must be a boolean, got {json_text(value)}"
    )


def _parse_expr(value: Any) -> Expr:
    obj = _as_object(value, "expression")
    _allow(obj, "expression", ("lit", "ref", "op", "args", "object"))
    forms = [key for key in ("lit", "ref", "op", "object") if key in obj]
    if len(forms) != 1:
        raise GraphIrError("expression must be exactly one of lit, ref, op, or object")
    if "args" in obj and "op" not in obj:
        raise GraphIrError("args is only valid on an op expression")
    if "lit" in obj:
        return Lit(_freeze(obj["lit"]))
    if "ref" in obj:
        return Ref(_parse_id(_require_str(obj, "ref", "expression"), "ref"))
    if "object" in obj:
        return _parse_object_expr(obj["object"])
    op = _require_str(obj, "op", "expression")
    if op not in _OPS:
        raise GraphIrError(f"unknown op `{op}`")
    raw_args = _require(obj, "args", "op expression")
    if not isinstance(raw_args, list):
        raise GraphIrError("op args must be an array")
    _check_arity(op, len(raw_args))
    return Call(op, tuple(_parse_expr(arg) for arg in raw_args))


def _check_arity(op: str, length: int) -> None:
    if op in _UNARY:
        ok = length == 1
    elif op == "if":
        ok = length == 3
    elif op == "array":
        ok = True
    else:
        ok = length == 2
    if not ok:
        raise GraphIrError(f"op `{op}` has the wrong number of arguments")


def _parse_object_expr(value: Any) -> ObjectExpr:
    if not isinstance(value, list):
        raise GraphIrError("object expression must be an array")
    fields: list[ObjectField] = []
    seen: set[str] = set()
    for field_value in value:
        obj = _as_object(field_value, "object field")
        _allow(obj, "object field", ("key", "value"))
        key = _require_str(obj, "key", "object field")
        if key in seen:
            raise GraphIrError(f"duplicate object key `{key}`")
        seen.add(key)
        fields.append(
            ObjectField(key, _parse_expr(_require(obj, "value", "object field")))
        )
    return ObjectExpr(tuple(fields))


def _parse_stmt(value: Any) -> Stmt:
    obj = _as_object(value, "statement")
    verb = _require_str(obj, "do", "statement")
    if verb == "eval":
        _allow(obj, "eval", ("do", "expr"))
        return EvalStmt(_parse_expr(_require(obj, "expr", "eval")))
    if verb in ("set", "merge"):
        _allow(obj, verb, ("do", "target", "expr"))
        target = _parse_id(_require_str(obj, "target", verb), "write target")
        expr = _parse_expr(_require(obj, "expr", verb))
        if verb == "set":
            return SetStmt(target, expr)
        return MergeStmt(target, expr)
    if verb == "seq":
        _allow(obj, "seq", ("do", "steps"))
        raw = _require(obj, "steps", "seq")
        if not isinstance(raw, list):
            raise GraphIrError("seq steps must be an array")
        if not raw:
            raise GraphIrError("seq steps must not be empty")
        return SeqStmt(tuple(_parse_stmt(step) for step in raw))
    if verb == "when":
        _allow(obj, "when", ("do", "cond", "then", "else"))
        cond = _parse_expr(_require(obj, "cond", "when"))
        then_stmt = _parse_stmt(_require(obj, "then", "when"))
        else_stmt = None
        if "else" in obj:
            else_stmt = _parse_stmt(obj["else"])
        return WhenStmt(cond, then_stmt, else_stmt)
    raise GraphIrError(f"unknown statement `{verb}`")


def _parse_edit_op(value: Any) -> EditOp:
    obj = _as_object(value, "edit op")
    op = _require_str(obj, "op", "edit op")
    if op in ("add", "replace"):
        _allow(obj, op, ("op", "node"))
        node = _parse_node(_require(obj, "node", op))
        if op == "add":
            return AddOp(node)
        return ReplaceOp(node)
    if op == "remove":
        _allow(obj, "remove", ("op", "id"))
        return RemoveOp(_parse_id(_require_str(obj, "id", "remove"), "remove id"))
    raise GraphIrError(f"unknown edit op `{op}`")


def _check_policy_init(policy: Policy, init: Any) -> None:
    if policy in (Policy.SUM, Policy.MAX, Policy.MIN):
        _as_num(init)
    elif policy in (Policy.AND, Policy.OR):
        _expect_bool(init)


def _expect_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise GraphIrError(f"expected a boolean, got {json_text(value)}")


def _expect_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    raise GraphIrError(f"expected a string, got {json_text(value)}")


def _as_num(value: Any) -> tuple[str, int | float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GraphIrError(f"expected a number, got {json_text(value)}")
    if isinstance(value, int):
        if value < I64_MIN or value > I64_MAX:
            magnitude = abs(value)
            if magnitude > 1e308:
                raise GraphIrError(
                    f"number {value} is outside the graph IR numeric range"
                )
            return ("float", float(value))
        return ("int", value)
    if not math.isfinite(value):
        raise GraphIrError("non-finite number")
    return ("float", value)


def _canonical_f64(number: float) -> int | float:
    if not math.isfinite(number):
        raise GraphIrError("non-finite number")
    if number.is_integer() and I64_MIN <= number <= I64_MAX:
        integer = int(number)
        if float(integer) == number:
            return integer
    return number


def _ordering(left: Any, right: Any) -> int:
    left_num = _as_num(left)
    right_num = _as_num(right)
    if left_num[0] == "int" and right_num[0] == "int":
        if left_num[1] < right_num[1]:
            return -1
        if left_num[1] > right_num[1]:
            return 1
        return 0
    lf = float(left_num[1])
    rf = float(right_num[1])
    if math.isnan(lf) or math.isnan(rf):
        raise GraphIrError("non-finite number")
    if lf < rf:
        return -1
    if lf > rf:
        return 1
    return 0


def _checked(value: int) -> int:
    if value < I64_MIN or value > I64_MAX:
        raise GraphIrError("integer overflow")
    return value


def _trunc_mod(left: int, right: int) -> int:
    if right == 0:
        raise GraphIrError("division by zero")
    quotient = abs(left) // abs(right)
    if (left < 0) != (right < 0):
        quotient = -quotient
    return left - right * quotient


def _num_bin(op: str, left: Any, right: Any) -> int | float:
    left_num = _as_num(left)
    right_num = _as_num(right)
    if op != "div" and left_num[0] == "int" and right_num[0] == "int":
        a = int(left_num[1])
        b = int(right_num[1])
        if op == "add":
            return _checked(a + b)
        if op == "sub":
            return _checked(a - b)
        if op == "mul":
            return _checked(a * b)
        raise GraphIrError(f"op `{op}` is not an integer operator")
    lf = float(left_num[1])
    rf = float(right_num[1])
    if op == "div" and rf == 0.0:
        raise GraphIrError("division by zero")
    if op == "add":
        value = lf + rf
    elif op == "sub":
        value = lf - rf
    elif op == "mul":
        value = lf * rf
    elif op == "div":
        value = lf / rf
    else:
        raise GraphIrError(f"op `{op}` is not a numeric operator")
    return _canonical_f64(value)


def _eval_values(op: str, values: list[Any]) -> Any:
    def one(name: str) -> Any:
        if len(values) != 1:
            raise GraphIrError(f"op `{name}` has the wrong number of arguments")
        return values[0]

    def pair(name: str) -> tuple[Any, Any]:
        if len(values) != 2:
            raise GraphIrError(f"op `{name}` has the wrong number of arguments")
        return values[0], values[1]

    if op == "neg":
        kind, number = _as_num(one("neg"))
        if kind == "int":
            integer = int(number)
            if integer == I64_MIN:
                raise GraphIrError("integer overflow")
            return -integer
        return _canonical_f64(-float(number))
    if op == "not":
        return not _expect_bool(one("not"))
    if op == "len":
        return _len_value(one("len"))
    if op in ("add", "sub", "mul", "div"):
        left, right = pair(op)
        return _num_bin(op, left, right)
    if op == "mod":
        left, right = pair("mod")
        left_num = _as_num(left)
        right_num = _as_num(right)
        if left_num[0] != "int" or right_num[0] != "int":
            raise GraphIrError("mod requires integers")
        return _trunc_mod(int(left_num[1]), int(right_num[1]))
    if op in ("min", "max"):
        left, right = pair(op)
        order = _ordering(left, right)
        if op == "max":
            return left if order >= 0 else right
        return left if order <= 0 else right
    if op == "eq":
        left, right = pair("eq")
        return _json_eq(left, right)
    if op == "ne":
        left, right = pair("ne")
        return not _json_eq(left, right)
    if op in ("lt", "le", "gt", "ge"):
        left, right = pair(op)
        order = _ordering(left, right)
        if op == "lt":
            return order < 0
        if op == "le":
            return order <= 0
        if op == "gt":
            return order > 0
        return order >= 0
    if op == "concat":
        left, right = pair("concat")
        return _expect_str(left) + _expect_str(right)
    if op == "get":
        return _eval_get(pair("get"))
    raise GraphIrError(f"op `{op}` is not an eager operator")


def _len_value(value: Any) -> int:
    if isinstance(value, (str, list, dict)):
        length = len(value)
    else:
        raise GraphIrError(
            f"len requires a string, array, or object, got {json_text(value)}"
        )
    if length > I64_MAX:
        raise GraphIrError("length does not fit an integer")
    return length


def _eval_get(pair: tuple[Any, Any]) -> Any:
    target, key = pair
    if isinstance(target, list):
        if isinstance(key, bool) or not isinstance(key, int):
            kind, _number = _as_num(key)
            if kind != "int":
                raise GraphIrError("array index must be an integer")
            index = int(_number)
        else:
            index = key
        if index < 0:
            raise GraphIrError("array index must be >= 0")
        if index >= len(target):
            raise GraphIrError(f"index {index} out of range")
        return _freeze(target[index])
    if isinstance(target, dict):
        name = _expect_str(key)
        if name not in target:
            raise GraphIrError(f"missing key `{name}`")
        return _freeze(target[name])
    raise GraphIrError(f"get requires an array or object, got {json_text(target)}")


def _fold(policy: Policy, old: Any, op: Any) -> Any:
    if policy is Policy.KEEP_LATEST:
        return _freeze(op)
    if policy is Policy.SUM:
        return _num_bin("add", old, op)
    if policy is Policy.MAX:
        return _freeze(old if _ordering(old, op) >= 0 else op)
    if policy is Policy.MIN:
        return _freeze(old if _ordering(old, op) <= 0 else op)
    if policy is Policy.AND:
        return _expect_bool(old) and _expect_bool(op)
    if policy is Policy.OR:
        return _expect_bool(old) or _expect_bool(op)
    raise GraphIrError(f"unknown source policy `{policy}`")


@dataclass
class _SourceState:
    value: Any
    policy: Policy


@dataclass
class _ComputedState:
    expr: Expr
    eager: bool
    cache: Any = None
    has_value: bool = False
    dirty: bool = True


@dataclass
class _EffectState:
    body: Stmt


@dataclass
class _BindingSnap:
    kind: str
    source: _SourceState | None = None
    computed: _ComputedState | None = None
    effect: _EffectState | None = None
    deps: set[str] = field(default_factory=set)


class GraphRuntime:
    """Document plus the cells it was lowered onto."""

    def __init__(self, doc: GraphIr) -> None:
        self._doc = doc
        self._sources: dict[str, _SourceState] = {}
        self._computeds: dict[str, _ComputedState] = {}
        self._effects: dict[str, _EffectState] = {}
        self._deps: dict[str, set[str]] = {}
        self._subs: dict[str, set[str]] = {}
        self._observed: dict[str, set[str]] = {}
        self._trace: list[EffectRun] = []
        self._frames: list[set[str]] = []
        self._stack: list[str | None] = []
        self._current: str | None = None
        self._computing: set[str] = set()
        self._scheduled: list[str] = []
        self._scheduled_set: set[str] = set()
        self._draining = False
        self._fault: str | None = None
        self.drain_budget = DRAIN_BUDGET
        self.last_drain_exhaustion: int | None = None

    @staticmethod
    def load_json(text: str) -> GraphRuntime:
        return GraphRuntime.lower(GraphIr.from_json_str(text))

    @staticmethod
    def lower(doc: GraphIr, drain_budget: int = DRAIN_BUDGET) -> GraphRuntime:
        doc.check()
        runtime = GraphRuntime(doc.clone())
        runtime.drain_budget = drain_budget
        runtime._install_document()
        return runtime

    def document(self) -> GraphIr:
        return self._doc

    def contains(self, node_id: str) -> bool:
        return self._doc.node(node_id) is not None

    def is_eager(self, node_id: str) -> bool:
        node = self._doc.node(node_id)
        if isinstance(node, ComputedNode):
            return node.eager
        if node is None:
            raise GraphIrError(f"unknown node `{node_id}`")
        raise GraphIrError(f"`{node_id}` is not a computed")

    def source_policy(self, node_id: str) -> Policy:
        node = self._doc.node(node_id)
        if isinstance(node, SourceNode):
            return node.policy
        if node is None:
            raise GraphIrError(f"unknown node `{node_id}`")
        raise GraphIrError(f"`{node_id}` is not a source")

    def static_edges(self) -> list[Edge]:
        return self._doc.static_edges()

    def emit_source(self) -> str:
        return self._doc.emit_source()

    def observed_reads(self, node_id: str) -> set[str]:
        return set(self._observed.get(node_id, set()))

    def effect_trace(self) -> list[EffectRun]:
        return list(self._trace)

    def get(self, node_id: str) -> Any:
        if (
            node_id in self._effects
            and node_id not in self._sources
            and node_id not in self._computeds
        ):
            raise GraphIrError(f"`{node_id}` is an effect and has no value")
        if node_id in self._sources:
            return _freeze(self._sources[node_id].value)
        if node_id in self._computeds:
            state = self._computeds[node_id]
            if state.dirty or not state.has_value:
                self._recompute(node_id)
                state = self._computeds[node_id]
            return _freeze(state.cache)
        raise GraphIrError(f"unknown node `{node_id}`")

    def write_source(self, node_id: str, value: Any) -> None:
        self._set_source(node_id, _freeze(value))
        self._drain()
        self._raise_fault()

    def apply(self, edit: GraphEdit) -> None:
        for index, op in enumerate(edit.ops()):
            nxt = self._doc.clone()
            try:
                nxt.apply_edit_op(op)
            except GraphIrError as error:
                raise GraphIrError(f"edit op {index}: {error}") from None
            try:
                self._install_op(op)
            except GraphIrError as error:
                raise GraphIrError(f"edit op {index}: {error}") from None
            self._doc = nxt

    def _install_document(self) -> None:
        for node in self._doc.nodes():
            if isinstance(node, SourceNode):
                self._insert_source(node)
        for node in self._doc.nodes():
            if isinstance(node, ComputedNode):
                self._insert_computed(node)
        for node in self._doc.nodes():
            if isinstance(node, ComputedNode) and node.eager:
                self._recompute(node.id)
        for node in self._doc.nodes():
            if isinstance(node, EffectNode):
                self._insert_effect(node)

    def _install_op(self, op: EditOp) -> None:
        if isinstance(op, AddOp):
            self._add_node(op.node)
        elif isinstance(op, ReplaceOp):
            self._replace_node(op.node)
        else:
            self._remove_node(op.id)
        self._drain()
        self._raise_fault()

    def _add_node(self, node: Node) -> None:
        if isinstance(node, SourceNode):
            self._insert_source(node)
        elif isinstance(node, ComputedNode):
            self._insert_computed(node)
            if node.eager:
                try:
                    self._recompute(node.id)
                except GraphIrError:
                    self._drop_binding(node.id)
                    raise
        else:
            self._insert_effect(node)

    def _replace_node(self, node: Node) -> None:
        snap = self._snapshot(node.id)
        self._drop_binding(node.id)
        try:
            self._add_node(node)
        except GraphIrError:
            self._drop_binding(node.id)
            self._restore(node.id, snap)
            raise
        self._notify(node.id)

    def _remove_node(self, node_id: str) -> None:
        if (
            node_id not in self._sources
            and node_id not in self._computeds
            and node_id not in self._effects
        ):
            raise GraphIrError(f"unknown node `{node_id}`")
        self._drop_binding(node_id)
        self._subs.pop(node_id, None)

    def _insert_source(self, node: SourceNode) -> None:
        _check_policy_init(node.policy, node.init)
        self._sources[node.id] = _SourceState(_freeze(node.init), node.policy)

    def _insert_computed(self, node: ComputedNode) -> None:
        self._computeds[node.id] = _ComputedState(node.expr, node.eager)
        self._deps[node.id] = set()

    def _insert_effect(self, node: EffectNode) -> None:
        self._effects[node.id] = _EffectState(node.body)
        self._deps[node.id] = set()
        try:
            self._run_effect(node.id)
        except GraphIrError:
            self._drop_binding(node.id)
            raise

    def _snapshot(self, node_id: str) -> _BindingSnap | None:
        if node_id in self._sources:
            state = self._sources[node_id]
            return _BindingSnap(
                "source",
                source=_SourceState(_freeze(state.value), state.policy),
            )
        if node_id in self._computeds:
            state = self._computeds[node_id]
            return _BindingSnap(
                "computed",
                computed=_ComputedState(
                    state.expr,
                    state.eager,
                    _freeze(state.cache) if state.has_value else None,
                    state.has_value,
                    state.dirty,
                ),
                deps=set(self._deps.get(node_id, set())),
            )
        if node_id in self._effects:
            return _BindingSnap(
                "effect",
                effect=_EffectState(self._effects[node_id].body),
                deps=set(self._deps.get(node_id, set())),
            )
        return None

    def _restore(self, node_id: str, snap: _BindingSnap | None) -> None:
        if snap is None:
            return
        if snap.kind == "source" and snap.source is not None:
            self._sources[node_id] = snap.source
        elif snap.kind == "computed" and snap.computed is not None:
            self._computeds[node_id] = snap.computed
            self._deps[node_id] = set()
            for dep in snap.deps:
                self._subs.setdefault(dep, set()).add(node_id)
                self._deps[node_id].add(dep)
        elif snap.kind == "effect" and snap.effect is not None:
            self._effects[node_id] = snap.effect
            self._deps[node_id] = set()
            for dep in snap.deps:
                self._subs.setdefault(dep, set()).add(node_id)
                self._deps[node_id].add(dep)

    def _drop_binding(self, node_id: str) -> None:
        self._sources.pop(node_id, None)
        self._computeds.pop(node_id, None)
        self._effects.pop(node_id, None)
        for dep in self._deps.pop(node_id, set()):
            readers = self._subs.get(dep)
            if readers is not None:
                readers.discard(node_id)
        self._observed.pop(node_id, None)
        self._scheduled_set.discard(node_id)

    def _set_source(self, node_id: str, value: Any) -> Any:
        state = self._sources.get(node_id)
        if state is None:
            if node_id in self._computeds or node_id in self._effects:
                raise GraphIrError(f"`{node_id}` is not a source")
            raise GraphIrError(f"unknown node `{node_id}`")
        changed = not _json_eq(state.value, value)
        state.value = _freeze(value)
        if changed:
            self._notify(node_id)
        return _freeze(state.value)

    def _merge_source(self, node_id: str, value: Any) -> Any:
        state = self._sources.get(node_id)
        if state is None:
            if node_id in self._computeds or node_id in self._effects:
                raise GraphIrError(f"`{node_id}` is not a source")
            raise GraphIrError(f"unknown node `{node_id}`")
        folded = _fold(state.policy, state.value, value)
        return self._set_source(node_id, folded)

    def _notify(self, node_id: str) -> None:
        for dep in list(self._subs.get(node_id, set())):
            if dep in self._computeds:
                state = self._computeds[dep]
                if state.eager or self._subs.get(dep):
                    if dep in self._computing:
                        state.dirty = True
                    else:
                        self._recompute(dep)
                else:
                    state.dirty = True
            elif dep in self._effects:
                self._schedule(dep)

    def _schedule(self, node_id: str) -> None:
        if node_id in self._effects and node_id not in self._scheduled_set:
            self._scheduled_set.add(node_id)
            self._scheduled.append(node_id)

    def _drain(self) -> None:
        if self._draining:
            return
        self._draining = True
        iterations = 0
        try:
            while self._scheduled:
                iterations += 1
                if iterations >= self.drain_budget:
                    self.last_drain_exhaustion = iterations
                    return
                node_id = self._scheduled.pop(0)
                self._scheduled_set.discard(node_id)
                if node_id not in self._effects:
                    continue
                try:
                    self._run_effect(node_id)
                except GraphIrError as error:
                    self._fault = str(error)
                    return
        finally:
            self._draining = False

    def _raise_fault(self) -> None:
        if self._fault is not None:
            message = self._fault
            self._fault = None
            raise GraphIrError(message)

    def _run_effect(self, node_id: str) -> None:
        state = self._effects[node_id]
        self._begin(node_id)
        try:
            result = self._eval_stmt(state.body)
        except GraphIrError:
            self._end(node_id)
            raise
        self._end(node_id)
        self._trace.append(EffectRun(node_id, _freeze(result)))
        self._drain()

    def _recompute(self, node_id: str) -> Any:
        state = self._computeds[node_id]
        if node_id in self._computing:
            raise GraphIrError(f"cycle in read edges: {node_id}")
        self._computing.add(node_id)
        self._begin(node_id)
        try:
            value = self._eval_expr(state.expr)
        except GraphIrError:
            self._end(node_id)
            self._computing.discard(node_id)
            state.dirty = True
            state.has_value = False
            state.cache = None
            raise
        self._end(node_id)
        self._computing.discard(node_id)
        changed = not state.has_value or not _json_eq(state.cache, value)
        state.cache = _freeze(value)
        state.has_value = True
        state.dirty = False
        if changed:
            self._notify(node_id)
        return state.cache

    def _begin(self, node_id: str) -> None:
        for dep in self._deps.get(node_id, set()):
            readers = self._subs.get(dep)
            if readers is not None:
                readers.discard(node_id)
        self._deps[node_id] = set()
        self._frames.append(set())
        self._stack.append(self._current)
        self._current = node_id

    def _end(self, node_id: str) -> None:
        frame = self._frames.pop()
        self._observed[node_id] = set(frame)
        self._current = self._stack.pop()

    def _note(self, node_id: str) -> None:
        if not self._frames or self._current is None:
            raise GraphIrError("read outside a node evaluation")
        self._frames[-1].add(node_id)
        self._subs.setdefault(node_id, set()).add(self._current)
        self._deps[self._current].add(node_id)

    def _read_ref(self, node_id: str) -> Any:
        self._note(node_id)
        if node_id in self._sources:
            return _freeze(self._sources[node_id].value)
        if node_id in self._computeds:
            state = self._computeds[node_id]
            if state.dirty or not state.has_value:
                return _freeze(self._recompute(node_id))
            return _freeze(state.cache)
        if node_id in self._effects:
            raise GraphIrError(f"`{node_id}` is an effect and has no value")
        raise GraphIrError(f"unknown node `{node_id}`")

    def _eval_expr(self, expr: Expr) -> Any:
        if isinstance(expr, Lit):
            return _freeze(expr.value)
        if isinstance(expr, Ref):
            return self._read_ref(expr.id)
        if isinstance(expr, ObjectExpr):
            obj: dict[str, Any] = {}
            for item in expr.fields:
                if item.key in obj:
                    raise GraphIrError(f"duplicate object key `{item.key}`")
                obj[item.key] = self._eval_expr(item.value)
            return obj
        if expr.op == "and":
            return self._eval_and(expr.args)
        if expr.op == "or":
            return self._eval_or(expr.args)
        if expr.op == "if":
            return self._eval_if(expr.args)
        if expr.op == "array":
            return [self._eval_expr(arg) for arg in expr.args]
        return _eval_values(expr.op, [self._eval_expr(arg) for arg in expr.args])

    def _eval_and(self, args: tuple[Any, ...]) -> bool:
        if len(args) != 2:
            raise GraphIrError("op `and` has the wrong number of arguments")
        if not _expect_bool(self._eval_expr(args[0])):
            return False
        return _expect_bool(self._eval_expr(args[1]))

    def _eval_or(self, args: tuple[Any, ...]) -> bool:
        if len(args) != 2:
            raise GraphIrError("op `or` has the wrong number of arguments")
        if _expect_bool(self._eval_expr(args[0])):
            return True
        return _expect_bool(self._eval_expr(args[1]))

    def _eval_if(self, args: tuple[Any, ...]) -> Any:
        if len(args) != 3:
            raise GraphIrError("op `if` has the wrong number of arguments")
        if _expect_bool(self._eval_expr(args[0])):
            return self._eval_expr(args[1])
        return self._eval_expr(args[2])

    def _eval_stmt(self, stmt: Stmt) -> Any:
        if isinstance(stmt, EvalStmt):
            return self._eval_expr(stmt.expr)
        if isinstance(stmt, SetStmt):
            value = self._eval_expr(stmt.expr)
            return self._set_source(stmt.target, value)
        if isinstance(stmt, MergeStmt):
            value = self._eval_expr(stmt.expr)
            return self._merge_source(stmt.target, value)
        if isinstance(stmt, SeqStmt):
            return [self._eval_stmt(step) for step in stmt.steps]
        cond = _expect_bool(self._eval_expr(stmt.cond))
        if cond:
            return self._eval_stmt(stmt.then_stmt)
        if stmt.else_stmt is not None:
            return self._eval_stmt(stmt.else_stmt)
        return None

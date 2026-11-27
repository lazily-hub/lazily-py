"""Host-neutral view compute document.

Install lowers each signal to a :class:`~lazily.cell.Cell`, each text, hidden,
or disabled expression to a guarded :func:`~lazily.signal.computed`, and the
optional chart to one :class:`~lazily.statechart.StateChart`. Dispatch runs
effect trees. The conformance trace records ``set`` and ``send`` only.
"""

from __future__ import annotations

from typing import Any

from .cell import Cell
from .effect import effect
from .signal import computed
from .statechart import ChartDef, StateChart

_ROLES = {"text", "button", "field", "list", "region"}
_FLOWS = {"row", "column", "stack"}
_ALIGNS = {"start", "center", "end", "stretch"}
_EVENTS = {"activate", "change", "submit"}
_EXPR_OPS = {"lit", "ref", "call", "if", "eq", "concat", "trim"}
_EFFECT_OPS = {"set", "batch", "send"}
_CALLS = {"event", "not", "add", "len"}
_NODE_FIELDS = {
    "role",
    "children",
    "layout",
    "text",
    "bind",
    "hidden",
    "disabled",
    "effects",
}


def _allow(obj: dict[str, Any], keys: set[str], what: str) -> None:
    for key in obj:
        if key not in keys:
            raise ValueError(f"{what} has unknown field `{key}`")


def _req(obj: dict[str, Any], key: str, what: str) -> Any:
    if key not in obj:
        raise ValueError(f"{what} missing `{key}`")
    return obj[key]


def _literal(value: Any) -> Any:
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise ValueError(f"non-integer number {value}")
    raise ValueError("value must be null, boolean, integer, or string")


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return value


def _is_expr(value: Any) -> bool:
    return isinstance(value, dict)


def _validate_arg(value: Any, signals: set[str], in_effect: bool) -> None:
    if _is_expr(value):
        _validate_expr(value, signals, in_effect)
    else:
        _literal(value)


def _validate_call(args: list[Any], signals: set[str], in_effect: bool) -> None:
    if not args or not isinstance(args[0], str):
        raise ValueError("call takes a name")
    name = args[0]
    if name not in _CALLS:
        raise ValueError(f"unknown call `{name}`")
    rest = args[1:]
    if name == "event":
        if rest:
            raise ValueError("event takes no arguments")
        if not in_effect:
            raise ValueError("event is illegal outside an effect")
    elif name == "not":
        if len(rest) != 1:
            raise ValueError("not takes one boolean")
        _validate_arg(rest[0], signals, in_effect)
    elif name == "add":
        if len(rest) < 2:
            raise ValueError("add takes two or more integers")
        for arg in rest:
            _validate_arg(arg, signals, in_effect)
    elif name == "len":
        if len(rest) != 1:
            raise ValueError("len takes one string")
        _validate_arg(rest[0], signals, in_effect)


def _validate_expr(expr: dict[str, Any], signals: set[str], in_effect: bool) -> None:
    _allow(expr, {"op", "args"}, "expression")
    op = _req(expr, "op", "expression")
    if op not in _EXPR_OPS:
        raise ValueError(f"unknown expression op `{op}`")
    args = _req(expr, "args", "expression")
    if not isinstance(args, list):
        raise ValueError("expression args must be an array")
    if op == "lit":
        if len(args) != 1:
            raise ValueError("lit takes one literal")
        _literal(args[0])
    elif op == "ref":
        if len(args) != 1 or not isinstance(args[0], str):
            raise ValueError("ref takes one signal id")
        if args[0] not in signals:
            raise ValueError(f"unknown signal `{args[0]}`")
    elif op == "call":
        _validate_call(args, signals, in_effect)
    elif op == "if":
        if len(args) != 3:
            raise ValueError("if takes three arguments")
        for arg in args:
            _validate_arg(arg, signals, in_effect)
    elif op == "eq":
        if len(args) != 2:
            raise ValueError("eq takes two arguments")
        for arg in args:
            _validate_arg(arg, signals, in_effect)
    elif op == "concat":
        for arg in args:
            _validate_arg(arg, signals, in_effect)
    elif op == "trim":
        if len(args) != 1:
            raise ValueError("trim takes one value")
        _validate_arg(args[0], signals, in_effect)


def _validate_effect(effect_doc: dict[str, Any], signals: set[str], has_chart: bool) -> None:
    _allow(effect_doc, {"op", "args"}, "effect")
    op = _req(effect_doc, "op", "effect")
    if op not in _EFFECT_OPS:
        raise ValueError(f"unknown effect op `{op}`")
    args = _req(effect_doc, "args", "effect")
    if not isinstance(args, list):
        raise ValueError("effect args must be an array")
    if op == "set":
        if len(args) != 2 or not isinstance(args[0], str):
            raise ValueError("set takes a signal id and a value")
        if args[0] not in signals:
            raise ValueError(f"unknown signal `{args[0]}`")
        _validate_arg(args[1], signals, True)
    elif op == "send":
        if not has_chart:
            raise ValueError("send requires a chart")
        if len(args) not in (1, 2) or not isinstance(args[0], str):
            raise ValueError("send takes an event name and an optional payload")
        if len(args) == 2:
            _validate_arg(args[1], signals, True)
    elif op == "batch":
        if not args:
            raise ValueError("batch takes one or more effects")
        for nested in args:
            if not isinstance(nested, dict):
                raise ValueError("effect must be an object")
            _validate_effect(nested, signals, has_chart)


def _standing_text(value: Any, signals: set[str]) -> dict[str, Any]:
    if isinstance(value, str):
        return {"op": "lit", "args": [value]}
    if isinstance(value, dict):
        _validate_expr(value, signals, False)
        return value
    raise ValueError("text must be a string or an expression")


def _standing_flag(field: str, value: Any, signals: set[str]) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"op": "lit", "args": [value]}
    if isinstance(value, dict):
        _validate_expr(value, signals, False)
        return value
    raise ValueError(f"{field} must be a boolean or an expression")


def _parse_children(nid: str, value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"node `{nid}` children must be an array")
    seen: set[str] = set()
    for child in value:
        if not isinstance(child, str) or not child:
            raise ValueError(f"node `{nid}` children must be non-empty strings")
        if child in seen:
            raise ValueError(f"node `{nid}` repeats child `{child}`")
        seen.add(child)
    return list(value)


def _parse_layout(nid: str, value: Any) -> str | None:
    if not isinstance(value, dict):
        raise ValueError(f"node `{nid}` layout must be an object")
    _allow(value, {"flow", "align", "grow"}, f"node `{nid}` layout")
    flow = value.get("flow")
    if flow is not None and flow not in _FLOWS:
        raise ValueError(f"unknown flow `{flow}`")
    align = value.get("align")
    if align is not None and align not in _ALIGNS:
        raise ValueError(f"unknown align `{align}`")
    grow = value.get("grow")
    if grow is not None and (isinstance(grow, bool) or not isinstance(grow, int) or grow < 0):
        raise ValueError(f"node `{nid}` layout.grow must be an integer >= 0")
    return flow if isinstance(flow, str) else None


def _check_tree(root: str, nodes: dict[str, dict[str, Any]]) -> None:
    stack: set[str] = set()
    done: set[str] = set()

    def walk(nid: str) -> None:
        if nid in stack:
            raise ValueError(f"children cycle at `{nid}`")
        if nid in done:
            raise ValueError(f"node `{nid}` has more than one parent")
        stack.add(nid)
        for child in nodes[nid]["children"]:
            if child not in nodes:
                raise ValueError(f"children name unknown node `{child}`")
            walk(child)
        stack.remove(nid)
        done.add(nid)

    walk(root)
    if len(done) != len(nodes):
        raise ValueError("a node is not reachable from root")


def _eval_arg(value: Any, read: Any, payload: Any) -> Any:
    if _is_expr(value):
        return _eval_expr(value, read, payload)
    return _literal(value)


def _eval_expr(expr: dict[str, Any], read: Any, payload: Any) -> Any:
    op = expr["op"]
    args = expr["args"]
    if op == "lit":
        return _literal(args[0])
    if op == "ref":
        return read(args[0])
    if op == "call":
        return _eval_call(args, read, payload)
    if op == "if":
        cond = _eval_arg(args[0], read, payload)
        if not isinstance(cond, bool):
            raise ValueError("if condition must be a boolean")
        return _eval_arg(args[1 if cond else 2], read, payload)
    if op == "eq":
        return _eval_arg(args[0], read, payload) == _eval_arg(args[1], read, payload)
    if op == "concat":
        return "".join(_stringify(_eval_arg(arg, read, payload)) for arg in args)
    if op == "trim":
        return _stringify(_eval_arg(args[0], read, payload)).strip()
    raise ValueError(f"unknown expression op `{op}`")


def _eval_call(args: list[Any], read: Any, payload: Any) -> Any:
    name = args[0]
    rest = args[1:]
    if name == "event":
        if payload is _NO_EVENT:
            raise ValueError("event is illegal outside an effect")
        return payload
    if name == "not":
        value = _eval_arg(rest[0], read, payload)
        if not isinstance(value, bool):
            raise ValueError("not expects a boolean")
        return not value
    if name == "add":
        total = 0
        for arg in rest:
            value = _eval_arg(arg, read, payload)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("add expects integers")
            total += value
        return total
    if name == "len":
        value = _eval_arg(rest[0], read, payload)
        if not isinstance(value, str):
            raise ValueError("len expects a string")
        return len(value)
    raise ValueError(f"unknown call `{name}`")


class _NoEvent:
    pass


_NO_EVENT = _NoEvent()


class View:
    """A view document installed on one reactive context."""

    def __init__(self, doc: dict[str, Any]) -> None:
        if not isinstance(doc, dict):
            raise ValueError("view must be a JSON object")
        _allow(doc, {"id", "root", "signals", "nodes", "chart"}, "view")
        view_id = _req(doc, "id", "view")
        if not isinstance(view_id, str) or not view_id:
            raise ValueError("view id must be a non-empty string")
        root = _req(doc, "root", "view")
        if not isinstance(root, str) or not root:
            raise ValueError("view root must be a non-empty string")
        signals_doc = _req(doc, "signals", "view")
        if not isinstance(signals_doc, dict):
            raise ValueError("view `signals` must be an object")
        signal_ids: set[str] = set()
        initials: dict[str, Any] = {}
        for sid, raw in signals_doc.items():
            if not sid:
                raise ValueError("signal id must be a non-empty string")
            if not isinstance(raw, dict):
                raise ValueError(f"signal `{sid}` must be an object")
            _allow(raw, {"initial"}, f"signal `{sid}`")
            signal_ids.add(sid)
            initials[sid] = _literal(_req(raw, "initial", f"signal `{sid}`"))

        has_chart = "chart" in doc
        chart_doc = doc.get("chart")
        if has_chart and not isinstance(chart_doc, dict):
            raise ValueError("chart must be a statechart document")

        nodes_doc = _req(doc, "nodes", "view")
        if not isinstance(nodes_doc, dict) or not nodes_doc:
            raise ValueError("view nodes must contain at least one node")
        nodes: dict[str, dict[str, Any]] = {}
        effects: dict[str, dict[str, Any]] = {}
        expr_docs: list[tuple[str, dict[str, Any]]] = []
        for nid, raw in nodes_doc.items():
            if not nid:
                raise ValueError("node id must be a non-empty string")
            if not isinstance(raw, dict):
                raise ValueError(f"node `{nid}` must be an object")
            _allow(raw, _NODE_FIELDS, f"node `{nid}`")
            role = _req(raw, "role", f"node `{nid}`")
            if role not in _ROLES:
                raise ValueError(f"unknown role `{role}`")
            children = _parse_children(nid, raw["children"]) if "children" in raw else []
            flow = _parse_layout(nid, raw["layout"]) if "layout" in raw else None
            if "bind" in raw:
                bind = raw["bind"]
                if not isinstance(bind, str) or not bind:
                    raise ValueError(f"node `{nid}` bind must be a non-empty string")
                if bind not in signal_ids:
                    raise ValueError(f"bind names unknown signal `{bind}`")
            if "text" in raw:
                expr_docs.append((f"{nid}.text", _standing_text(raw["text"], signal_ids)))
            if "hidden" in raw:
                expr_docs.append((f"{nid}.hidden", _standing_flag("hidden", raw["hidden"], signal_ids)))
            if "disabled" in raw:
                expr_docs.append(
                    (f"{nid}.disabled", _standing_flag("disabled", raw["disabled"], signal_ids))
                )
            if "effects" in raw:
                raw_effects = raw["effects"]
                if not isinstance(raw_effects, dict):
                    raise ValueError(f"node `{nid}` effects must be an object")
                _allow(raw_effects, _EVENTS, f"node `{nid}` effects")
                stored: dict[str, Any] = {}
                for event, effect_doc in raw_effects.items():
                    if not isinstance(effect_doc, dict):
                        raise ValueError("effect must be an object")
                    _validate_effect(effect_doc, signal_ids, has_chart)
                    stored[event] = effect_doc
                if stored:
                    effects[nid] = stored
            nodes[nid] = {"role": role, "children": children, "flow": flow}
        if root not in nodes:
            raise ValueError(f"root `{root}` is not a node")
        _check_tree(root, nodes)

        ctx: dict[str, Any] = {}
        signals = {sid: Cell(ctx, initial) for sid, initial in initials.items()}

        def read_bare(sid: str) -> Any:
            cell = signals.get(sid)
            if cell is None:
                raise ValueError(f"unknown signal `{sid}`")
            return cell.value

        for key, expr in expr_docs:
            try:
                _eval_expr(expr, read_bare, _NO_EVENT)
            except ValueError as exc:
                raise ValueError(f"{key}: {exc}") from exc

        exprs: dict[str, Any] = {}
        for key, expr in expr_docs:
            def compute(c: Any, expr: dict[str, Any] = expr) -> Any:
                def read(sid: str) -> Any:
                    cell = signals.get(sid)
                    if cell is None:
                        raise ValueError(f"unknown signal `{sid}`")
                    return c.read(cell)

                return _eval_expr(expr, read, _NO_EVENT)

            exprs[key] = computed(ctx, compute)

        chart = StateChart(ctx, ChartDef.from_chart(chart_doc)) if has_chart else None
        watched = list(exprs.values()) or list(signals.values())

        def subscribe(c: Any) -> None:
            for node in watched:
                c.read(node)

        effect(subscribe)(ctx)

        self._root = root
        self._nodes = nodes
        self._signals = signals
        self._exprs = exprs
        self._effects = effects
        self._chart = chart
        self._payload: Any = _NO_EVENT

    def signals_json(self) -> dict[str, Any]:
        return {sid: cell.value for sid, cell in self._signals.items()}

    def expressions_json(self) -> dict[str, Any]:
        return {key: node.value for key, node in self._exprs.items()}

    def layout_json(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []

        def walk(nid: str) -> None:
            node = self._nodes[nid]
            out.append(
                {
                    "id": nid,
                    "role": node["role"],
                    "children": node["children"],
                    "flow": node["flow"],
                }
            )
            for child in node["children"]:
                walk(child)

        walk(self._root)
        return out

    def chart_json(self) -> list[str] | None:
        if self._chart is None:
            return None
        return self._chart.active_leaves()

    def dispatch(self, node: str, event: str, value: Any) -> list[dict[str, Any]]:
        if event not in _EVENTS:
            raise ValueError(f"unknown event `{event}`")
        if node not in self._nodes:
            raise ValueError(f"unknown node `{node}`")
        effect_doc = self._effects.get(node, {}).get(event)
        if effect_doc is None:
            return []
        self._payload = _literal(value)
        trace: list[dict[str, Any]] = []
        try:
            self._exec(effect_doc, trace)
        finally:
            self._payload = _NO_EVENT
        return trace

    def _read(self, sid: str) -> Any:
        cell = self._signals.get(sid)
        if cell is None:
            raise ValueError(f"unknown signal `{sid}`")
        return cell.value

    def _eval(self, value: Any) -> Any:
        return _eval_arg(value, self._read, self._payload)

    def _exec(self, effect_doc: dict[str, Any], trace: list[dict[str, Any]]) -> None:
        op = effect_doc["op"]
        args = effect_doc["args"]
        if op == "set":
            sid = args[0]
            cell = self._signals.get(sid)
            if cell is None:
                raise ValueError(f"unknown signal `{sid}`")
            value = self._eval(args[1])
            cell.set(value)
            trace.append({"op": "set", "signal": sid, "value": value})
            return
        if op == "send":
            if self._chart is None:
                raise ValueError("send requires a chart")
            record: dict[str, Any] = {"op": "send", "event": args[0]}
            if len(args) == 2:
                record["payload"] = self._eval(args[1])
            self._chart.send(args[0], {})
            trace.append(record)
            return
        if op == "batch":
            for nested in args:
                self._exec(nested, trace)
            return
        raise ValueError(f"unknown effect op `{op}`")

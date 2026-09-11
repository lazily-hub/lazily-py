"""Struct ↔ reactive bridge tests (``#lzpystructcell``).

The property the bridge exists for: a reader of one field is invalidated only
when *that* field changes, while a whole-struct reader still sees one
re-materialization per settled wave.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, NamedTuple

import pytest

from lazily import (
    DataclassBackend,
    MsgspecBackend,
    NamedTupleBackend,
    PydanticBackend,
    StructSource,
    UnsupportedStructError,
    computed,
    register_struct_backend,
    resolve_struct_backend,
    struct_backends,
    struct_source,
)


@dataclass
class Status:
    depth: int
    last_error: str | None = None
    label: str = field(init=False, default="derived")


class Point(NamedTuple):
    x: int
    y: int


def _counting_view(ctx: dict, read: Any) -> tuple[Any, dict[str, int]]:
    runs = {"n": 0}

    def body(compute: Any) -> Any:
        runs["n"] += 1
        return read(compute)

    return computed(ctx, body).eager(), runs


# -- the core property --------------------------------------------------------


def test_field_reader_is_not_invalidated_by_a_sibling_write() -> None:
    ctx: dict = {}
    status = struct_source(ctx, Status(depth=0))

    depth_view, depth_runs = _counting_view(ctx, lambda c: c.read(status["depth"]) * 2)
    whole_view, whole_runs = _counting_view(ctx, lambda c: c.read(status.struct))

    assert depth_view.value == 0
    assert whole_runs["n"] == 1

    status.update(last_error="boom")
    assert depth_view.value == 0
    assert depth_runs["n"] == 1  # the sibling write formed no edge here
    assert whole_view.value.last_error == "boom"
    assert whole_runs["n"] == 2

    status.update(depth=3)
    assert depth_view.value == 6
    assert depth_runs["n"] == 2


def test_equal_write_is_inert_everywhere() -> None:
    ctx: dict = {}
    status = struct_source(ctx, Status(depth=3, last_error="boom"))
    depth_view, depth_runs = _counting_view(ctx, lambda c: c.read(status["depth"]))
    whole_view, whole_runs = _counting_view(ctx, lambda c: c.read(status.struct))
    assert (depth_view.value, whole_view.value.depth) == (3, 3)

    status.update(depth=3, last_error="boom")

    assert depth_runs["n"] == 1
    assert whole_runs["n"] == 1


def test_multi_field_update_is_one_wave() -> None:
    ctx: dict = {}
    status = struct_source(ctx, Status(depth=0))
    whole_view, whole_runs = _counting_view(ctx, lambda c: c.read(status.struct))
    assert whole_view.value.depth == 0
    assert whole_runs["n"] == 1

    status.update(depth=1, last_error="boom")

    assert whole_view.value == Status(depth=1, last_error="boom")
    assert whole_runs["n"] == 2  # one recompute, not one per field


def test_apply_diffs_a_whole_instance() -> None:
    ctx: dict = {}
    status = struct_source(ctx, Status(depth=1, last_error="boom"))
    depth_view, depth_runs = _counting_view(ctx, lambda c: c.read(status["depth"]))
    assert depth_view.value == 1

    status.apply(Status(depth=1, last_error=None))
    assert depth_runs["n"] == 1  # only last_error moved

    status.apply(Status(depth=7, last_error=None))
    assert depth_view.value == 7
    assert depth_runs["n"] == 2


def test_apply_rejects_a_foreign_type() -> None:
    ctx: dict = {}
    status = struct_source(ctx, Status(depth=0))
    with pytest.raises(TypeError, match="cannot apply Point"):
        status.apply(Point(x=1, y=2))  # type: ignore[arg-type]


# -- construction forms and errors --------------------------------------------


def test_struct_source_from_type_and_fields() -> None:
    ctx: dict = {}
    point = struct_source(ctx, Point, x=1, y=2)
    assert point.value == Point(x=1, y=2)
    point.set_field("x", 5)
    assert point.value == Point(x=5, y=2)


def test_struct_source_rejects_instance_plus_fields() -> None:
    with pytest.raises(TypeError, match="takes no field keyword arguments"):
        struct_source({}, Status(depth=0), depth=1)


def test_non_init_fields_are_not_bridged() -> None:
    ctx: dict = {}
    status = struct_source(ctx, Status(depth=0))
    assert status.fields == ("depth", "last_error")
    assert "label" not in status
    # The derived field is still produced by the constructor.
    assert status.value.label == "derived"


def test_unknown_field_access_and_write_raise() -> None:
    ctx: dict = {}
    status = struct_source(ctx, Status(depth=0))
    with pytest.raises(AttributeError, match="no bridged field 'nope'"):
        status.field("nope")
    with pytest.raises(AttributeError, match="no bridged field"):
        status.update(nope=1)


def test_unsupported_type_names_the_known_backends() -> None:
    class Plain:
        pass

    with pytest.raises(UnsupportedStructError, match="dataclasses"):
        struct_source({}, Plain())


def test_peek_and_call_and_dispose() -> None:
    ctx: dict = {}
    status = struct_source(ctx, Status(depth=2))
    assert status.peek("depth") == 2
    assert status() == status.value
    assert set(status.cells()) == {"depth", "last_error"}
    status.dispose()
    assert all(cell.disposed for cell in status.cells().values())


def test_eager_returns_the_bridge() -> None:
    ctx: dict = {}
    status = struct_source(ctx, Status(depth=0)).eager()
    assert isinstance(status, StructSource)
    assert status.struct.is_eager()


def test_repr_names_the_backend() -> None:
    assert "via dataclasses" in repr(struct_source({}, Status(depth=0)))


# -- backend registry ---------------------------------------------------------


def test_register_replaces_a_backend_of_the_same_name() -> None:
    original = [b for b in struct_backends() if b.name == "pydantic"]
    calls = {"n": 0}

    class LoudPydantic(PydanticBackend):
        def matches(self, struct_type: type) -> bool:
            calls["n"] += 1
            return super().matches(struct_type)

    try:
        register_struct_backend(LoudPydantic())
        names = [b.name for b in struct_backends()]
        assert names.count("pydantic") == 1
        assert names[0] == "pydantic"
        resolve_struct_backend(Status)
        assert calls["n"] >= 1
    finally:
        for backend in original:
            register_struct_backend(backend)


def test_resolution_order_prefers_specific_backends() -> None:
    names = [b.name for b in struct_backends()]
    assert names.index("dataclasses") > names.index("pydantic")
    assert isinstance(resolve_struct_backend(Status), DataclassBackend)
    assert isinstance(resolve_struct_backend(Point), NamedTupleBackend)


def test_importing_lazily_does_not_import_a_struct_library() -> None:
    """The zero-dependency claim, enforced.

    A structural probe must never reach for ``import msgspec`` to decide a type
    is not a msgspec Struct.
    """
    code = (
        "import sys, lazily\n"
        "lazily.resolve_struct_backend\n"
        "leaked = [m for m in ('msgspec', 'pydantic', 'attr', 'attrs')"
        " if m in sys.modules]\n"
        "assert not leaked, leaked\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# -- third-party backends -----------------------------------------------------


def test_msgspec_struct_round_trips() -> None:
    msgspec = pytest.importorskip("msgspec", reason="lazily[msgspec] not installed")

    class MsgStatus(msgspec.Struct):
        depth: int
        last_error: str | None = None

    ctx: dict = {}
    status = struct_source(ctx, MsgStatus(depth=0))
    assert isinstance(status.backend, MsgspecBackend)
    assert status.fields == ("depth", "last_error")

    depth_view, depth_runs = _counting_view(ctx, lambda c: c.read(status["depth"]))
    assert depth_view.value == 0

    status.update(last_error="boom")
    assert depth_runs["n"] == 1
    assert status.value == MsgStatus(depth=0, last_error="boom")

    status.update(depth=4)
    assert depth_view.value == 4


def test_pydantic_model_round_trips() -> None:
    pydantic = pytest.importorskip("pydantic", reason="lazily[pydantic] not installed")

    class PydStatus(pydantic.BaseModel):
        depth: int
        last_error: str | None = None

    ctx: dict = {}
    status = struct_source(ctx, PydStatus(depth=0))
    assert isinstance(status.backend, PydanticBackend)
    assert status.fields == ("depth", "last_error")

    depth_view, depth_runs = _counting_view(ctx, lambda c: c.read(status["depth"]))
    assert depth_view.value == 0

    status.update(last_error="boom")
    assert depth_runs["n"] == 1
    assert status.value == PydStatus(depth=0, last_error="boom")


def test_pydantic_validate_option_is_opt_in() -> None:
    pydantic = pytest.importorskip("pydantic", reason="lazily[pydantic] not installed")

    class Bounded(pydantic.BaseModel):
        depth: int

    lenient = struct_source({}, Bounded(depth=0))
    lenient.update(depth="not an int")
    assert lenient.value.depth == "not an int"  # model_construct, no validation

    strict = StructSource({}, Bounded(depth=0), backend=PydanticBackend(validate=True))
    strict.update(depth="not an int")
    with pytest.raises(pydantic.ValidationError):
        assert strict.value is None


def test_attrs_class_round_trips() -> None:
    attr = pytest.importorskip("attrs", reason="lazily[attrs] not installed")

    @attr.define
    class AttrStatus:
        depth: int
        last_error: str | None = None

    ctx: dict = {}
    status = struct_source(ctx, AttrStatus(depth=0))
    assert status.backend.name == "attrs"
    assert status.fields == ("depth", "last_error")

    depth_view, depth_runs = _counting_view(ctx, lambda c: c.read(status["depth"]))
    assert depth_view.value == 0

    status.update(last_error="boom")
    assert depth_runs["n"] == 1
    assert status.value == AttrStatus(depth=0, last_error="boom")


def test_attrs_private_field_uses_the_init_alias() -> None:
    attr = pytest.importorskip("attrs", reason="lazily[attrs] not installed")

    @attr.define
    class Private:
        _depth: int

    status = struct_source({}, Private(depth=1))
    assert status.fields == ("_depth",)
    status.update({"_depth": 4})
    assert status.value == Private(depth=4)

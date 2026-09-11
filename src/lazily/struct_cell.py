"""Struct ↔ reactive bridge — :func:`struct_source` (``#lzpystructcell``).

A :class:`StructSource` explodes one struct instance into **per-field**
:class:`~lazily.cell.Source` cells plus a single guarded
:class:`~lazily.signal.Computed` that re-materializes the struct. A reader that
depends on one field is invalidated only when *that* field changes; a reader
that wants the whole struct (``status()`` returning a fresh instance) keeps
depending on all of them and pays one construction per settled wave.

The bridge is **library-agnostic**. It never imports ``msgspec``, ``pydantic``
or ``attrs`` at module scope, and never imports one to decide whether a type
belongs to it: the backends probe structurally (an MRO entry from that module,
or the marker attribute the library stamps on the class), so the import can only
happen for a type whose defining module is already loaded. lazily's runtime
dependency set is unchanged — the optional extras in ``pyproject.toml``
(``lazily[msgspec]`` / ``lazily[pydantic]`` / ``lazily[attrs]`` /
``lazily[structs]``) exist for install ergonomics and for testing all backends
in CI, not because the library needs them.

Built-in backends, in resolution order: msgspec ``Struct``, pydantic v2
``BaseModel``, ``attrs``, stdlib ``dataclasses``, stdlib ``NamedTuple``.
Register another with :func:`register_struct_backend`.

Example::

    @dataclass
    class Status:
        queue_depth: int
        last_error: str | None


    ctx: dict = {}
    status = struct_source(ctx, Status(queue_depth=0, last_error=None))

    depth_view = computed(ctx, lambda c: c.read(status["queue_depth"]) * 2)
    status.update(last_error="boom")  # depth_view is NOT invalidated
    status.value  # Status(queue_depth=0, last_error='boom')
"""

from __future__ import annotations


__all__ = [
    "AttrsBackend",
    "DataclassBackend",
    "MsgspecBackend",
    "NamedTupleBackend",
    "PydanticBackend",
    "StructBackend",
    "StructSource",
    "UnsupportedStructError",
    "register_struct_backend",
    "resolve_struct_backend",
    "struct_backends",
    "struct_source",
]

from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from .batch import batch
from .cell import Source
from .signal import Computed, computed


if TYPE_CHECKING:
    from collections.abc import Mapping


class UnsupportedStructError(TypeError):
    """No registered :class:`StructBackend` recognizes this type."""


@runtime_checkable
class StructBackend(Protocol):
    """How one struct library is introspected and reconstructed.

    Implement the three operations and hand the instance to
    :func:`register_struct_backend`. ``matches`` must be cheap and must not
    import the backing library — probe structurally instead, so an application
    that never imports ``msgspec`` never pays for the msgspec backend.
    """

    name: str

    def matches(self, struct_type: type) -> bool:
        """Whether this backend owns ``struct_type``."""
        ...

    def fields(self, struct_type: type) -> tuple[str, ...]:
        """The bridged attribute names, in declaration order.

        Derived / non-``init`` fields are excluded: they are recomputed by the
        constructor and so are not independent state.
        """
        ...

    def values(self, instance: Any) -> dict[str, Any]:
        """The current value of each bridged field. Shallow — never a deep copy."""
        ...

    def build(self, struct_type: type, values: Mapping[str, Any]) -> Any:
        """Construct an instance from bridged field values keyed by field name."""
        ...


def _defined_in(struct_type: type, module_root: str, qualname: str) -> bool:
    """Whether ``struct_type`` inherits from ``module_root``'s ``qualname``.

    A purely structural probe: it walks the MRO and compares ``__module__`` /
    ``__qualname__``, so it cannot import the library. If the library were not
    already imported, no live class could carry that base in the first place.
    """
    try:
        mro = struct_type.__mro__
    except AttributeError:
        return False
    for base in mro:
        if (
            base.__qualname__ == qualname
            and base.__module__.partition(".")[0] == module_root
        ):
            return True
    return False


class MsgspecBackend:
    """``msgspec.Struct`` — the zero-copy, C-accelerated struct family."""

    name = "msgspec"

    def matches(self, struct_type: type) -> bool:
        return isinstance(struct_type, type) and _defined_in(
            struct_type, "msgspec", "Struct"
        )

    def fields(self, struct_type: type) -> tuple[str, ...]:
        import msgspec.structs

        return tuple(
            field.name for field in msgspec.structs.fields(cast("Any", struct_type))
        )

    def values(self, instance: Any) -> dict[str, Any]:
        import msgspec.structs

        # ``asdict`` is shallow and C-implemented; it is the one call the FPE
        # ``status()`` path is allowed to keep paying.
        return msgspec.structs.asdict(instance)

    def build(self, struct_type: type, values: Mapping[str, Any]) -> Any:
        return struct_type(**values)


class PydanticBackend:
    """pydantic v2 ``BaseModel``.

    Re-materialization uses ``model_construct`` by default: the field values
    were already validated on the way in (either by the instance the bridge was
    seeded from or by the caller), and re-validating on every settled wave would
    make a whole-struct read cost a full validation per field write — exactly
    the cost this bridge exists to remove. It is also alias-proof, where the
    validating constructor is not unless the model sets ``populate_by_name``.
    Pass ``PydanticBackend(validate=True)`` and register it yourself to validate
    on every re-materialization instead.
    """

    name = "pydantic"

    def __init__(self, *, validate: bool = False) -> None:
        self.validate = validate

    def matches(self, struct_type: type) -> bool:
        return (
            isinstance(struct_type, type)
            and _defined_in(struct_type, "pydantic", "BaseModel")
            and hasattr(struct_type, "model_fields")
        )

    def fields(self, struct_type: type) -> tuple[str, ...]:
        return tuple(struct_type.model_fields)  # type: ignore[attr-defined]

    def values(self, instance: Any) -> dict[str, Any]:
        return {name: getattr(instance, name) for name in self.fields(type(instance))}

    def build(self, struct_type: type, values: Mapping[str, Any]) -> Any:
        if self.validate:
            return struct_type(**values)
        return struct_type.model_construct(**values)  # type: ignore[attr-defined]


class AttrsBackend:
    """``attrs`` classes — probed by the ``__attrs_attrs__`` marker."""

    name = "attrs"

    def matches(self, struct_type: type) -> bool:
        return isinstance(struct_type, type) and hasattr(struct_type, "__attrs_attrs__")

    @staticmethod
    def _attrs(struct_type: type) -> tuple[Any, ...]:
        return tuple(
            attribute
            for attribute in struct_type.__attrs_attrs__  # type: ignore[attr-defined]
            if attribute.init
        )

    def fields(self, struct_type: type) -> tuple[str, ...]:
        return tuple(attribute.name for attribute in self._attrs(struct_type))

    def values(self, instance: Any) -> dict[str, Any]:
        return {
            attribute.name: getattr(instance, attribute.name)
            for attribute in self._attrs(type(instance))
        }

    def build(self, struct_type: type, values: Mapping[str, Any]) -> Any:
        # attrs strips a leading underscore for the ``__init__`` keyword, and an
        # explicit ``alias=`` overrides that; the attribute name stays the one
        # the bridge keys cells by.
        kwargs = {}
        for attribute in self._attrs(struct_type):
            alias = getattr(attribute, "alias", None) or attribute.name.lstrip("_")
            kwargs[alias] = values[attribute.name]
        return struct_type(**kwargs)


class DataclassBackend:
    """stdlib ``dataclasses`` — no dependency, so no extra.

    Field reads go through ``getattr`` rather than ``dataclasses.asdict``, which
    recurses and deep-copies: a bridged field holds whatever object the struct
    holds, and the bridge must not substitute a copy of it.
    """

    name = "dataclasses"

    def matches(self, struct_type: type) -> bool:
        import dataclasses

        return isinstance(struct_type, type) and dataclasses.is_dataclass(struct_type)

    @staticmethod
    def _fields(struct_type: type) -> tuple[Any, ...]:
        import dataclasses

        return tuple(field for field in dataclasses.fields(struct_type) if field.init)

    def fields(self, struct_type: type) -> tuple[str, ...]:
        return tuple(field.name for field in self._fields(struct_type))

    def values(self, instance: Any) -> dict[str, Any]:
        return {
            field.name: getattr(instance, field.name)
            for field in self._fields(type(instance))
        }

    def build(self, struct_type: type, values: Mapping[str, Any]) -> Any:
        return struct_type(**values)


class NamedTupleBackend:
    """stdlib ``typing.NamedTuple`` / ``collections.namedtuple``."""

    name = "namedtuple"

    def matches(self, struct_type: type) -> bool:
        return (
            isinstance(struct_type, type)
            and issubclass(struct_type, tuple)
            and hasattr(struct_type, "_fields")
        )

    def fields(self, struct_type: type) -> tuple[str, ...]:
        return tuple(struct_type._fields)  # type: ignore[attr-defined]

    def values(self, instance: Any) -> dict[str, Any]:
        return dict(instance._asdict())

    def build(self, struct_type: type, values: Mapping[str, Any]) -> Any:
        return struct_type(**values)


# Resolution order. The library-specific backends come first because their
# probes are exact; ``dataclasses`` is last of the structural ones because a
# ``pydantic.dataclasses.dataclass`` is a real dataclass and is correctly served
# by it. Registered backends are prepended, so an application override wins.
_BACKENDS: list[StructBackend] = [
    MsgspecBackend(),
    PydanticBackend(),
    AttrsBackend(),
    DataclassBackend(),
    NamedTupleBackend(),
]


def register_struct_backend(backend: StructBackend) -> None:
    """Register ``backend`` ahead of the built-ins.

    Re-registering a backend with the same ``name`` replaces the earlier one, so
    installing a customized :class:`PydanticBackend` does not leave the default
    behind it in the chain.
    """
    _BACKENDS[:] = [existing for existing in _BACKENDS if existing.name != backend.name]
    _BACKENDS.insert(0, backend)


def struct_backends() -> tuple[StructBackend, ...]:
    """The registered backends, in resolution order."""
    return tuple(_BACKENDS)


def resolve_struct_backend(struct_type: type) -> StructBackend:
    """The first registered backend that owns ``struct_type``."""
    for backend in _BACKENDS:
        if backend.matches(struct_type):
            return backend
    raise UnsupportedStructError(
        f"no struct backend recognizes {struct_type!r}; register one with "
        f"lazily.register_struct_backend (known: "
        f"{', '.join(b.name for b in _BACKENDS)})"
    )


class StructSource[T]:
    """Per-field :class:`~lazily.cell.Source` cells behind one struct value.

    Built by :func:`struct_source`. Reads of a single field
    (``holder["queue_depth"]``) form an edge on that field alone; :attr:`struct`
    is the guarded whole-struct :class:`~lazily.signal.Computed`, so a write
    that lands an equal value invalidates nothing at all.
    """

    __slots__ = ("_backend", "_cells", "_fields", "_struct", "_struct_type")

    def __init__(
        self,
        ctx: Any,
        initial: T,
        *,
        backend: StructBackend | None = None,
    ) -> None:
        struct_type = type(initial)
        resolved = (
            backend if backend is not None else resolve_struct_backend(struct_type)
        )
        names = resolved.fields(struct_type)
        values = resolved.values(initial)
        cells = {name: Source(ctx, values[name]) for name in names}

        def materialize(compute: Any) -> T:
            return resolved.build(
                struct_type,
                {name: compute.read(cell) for name, cell in cells.items()},
            )

        self._backend = resolved
        self._struct_type = struct_type
        self._fields = names
        self._cells = cells
        self._struct: Computed[T] = computed(ctx, materialize)

    # -- introspection ------------------------------------------------------ #

    @property
    def backend(self) -> StructBackend:
        """The backend that owns this struct type."""
        return self._backend

    @property
    def struct_type(self) -> type:
        """The bridged struct type."""
        return self._struct_type

    @property
    def fields(self) -> tuple[str, ...]:
        """The bridged field names, in declaration order."""
        return self._fields

    def cells(self) -> dict[str, Source[Any]]:
        """A copy of the field-name → :class:`~lazily.cell.Source` mapping."""
        return dict(self._cells)

    # -- per-field access --------------------------------------------------- #

    def field(self, name: str) -> Source[Any]:
        """The :class:`~lazily.cell.Source` cell backing ``name``.

        Read it through a compute view (``compute.read(holder.field("x"))``) to
        depend on that field alone.
        """
        try:
            return self._cells[name]
        except KeyError:
            raise AttributeError(
                f"{self._struct_type.__name__} has no bridged field {name!r}; "
                f"bridged fields: {', '.join(self._fields)}"
            ) from None

    def __getitem__(self, name: str) -> Source[Any]:
        return self.field(name)

    def __contains__(self, name: object) -> bool:
        return name in self._cells

    def peek(self, name: str) -> Any:
        """The current value of ``name``, forming **no** dependency edge."""
        return self.field(name).value

    # -- writes ------------------------------------------------------------- #

    def set_field(self, name: str, value: Any) -> None:
        """Write one field. An equal write invalidates nothing."""
        self.field(name).set(value)

    def update(self, values: Mapping[str, Any] | None = None, /, **kwargs: Any) -> None:
        """Write several fields as one invalidation wave.

        ``values`` is for field names that are not valid keyword arguments; the
        two forms may be combined, with ``kwargs`` winning on a collision.
        """
        pending: dict[str, Any] = {}
        if values is not None:
            pending.update(values)
        pending.update(kwargs)
        unknown = [name for name in pending if name not in self._cells]
        if unknown:
            raise AttributeError(
                f"{self._struct_type.__name__} has no bridged field(s) "
                f"{', '.join(sorted(unknown))}; bridged fields: "
                f"{', '.join(self._fields)}"
            )

        def run() -> None:
            for name, value in pending.items():
                self._cells[name].set(value)

        batch(run)

    def apply(self, instance: T) -> None:
        """Diff a whole instance into the field cells as one wave.

        The ingress for code that already produces a fresh struct: only the
        fields that actually changed invalidate, so replacing an instance whose
        values are identical is inert.
        """
        incoming = type(instance)
        if not (
            incoming is self._struct_type or issubclass(incoming, self._struct_type)
        ):
            raise TypeError(
                f"cannot apply {incoming.__name__} to a "
                f"{self._struct_type.__name__} bridge"
            )
        self.update(self._backend.values(instance))

    # -- whole-struct read -------------------------------------------------- #

    @property
    def struct(self) -> Computed[T]:
        """The guarded :class:`~lazily.signal.Computed` re-materializing the struct."""
        return self._struct

    @property
    def value(self) -> T:
        """The materialized struct. Untracked — read :attr:`struct` to depend on it."""
        return self._struct.value

    def __call__(self) -> T:
        return self._struct.value

    def eager(self) -> StructSource[T]:
        """Make the whole-struct computed eager, and return this bridge."""
        self._struct.eager()
        return self

    # -- teardown ----------------------------------------------------------- #

    def dispose(self) -> None:
        """Tear down the whole-struct computed and every field cell."""
        self._struct.dispose()
        for cell in self._cells.values():
            cell.dispose()

    def __repr__(self) -> str:
        return (
            f"<StructSource {self._struct_type.__name__} "
            f"via {self._backend.name}: {', '.join(self._fields)}>"
        )


def struct_source[T](
    ctx: Any,
    initial: T | type[T],
    *,
    backend: StructBackend | None = None,
    **field_values: Any,
) -> StructSource[T]:
    """Bridge a struct instance onto per-field cells (``#lzpystructcell``).

    ``initial`` is either an instance or the struct **type** plus its field
    values as keyword arguments::

        struct_source(ctx, Status(queue_depth=0))
        struct_source(ctx, Status, queue_depth=0)
    """
    if isinstance(initial, type):
        if backend is None:
            backend = resolve_struct_backend(initial)
        instance = backend.build(initial, field_values)
        return StructSource(ctx, instance, backend=backend)
    if field_values:
        raise TypeError(
            "struct_source(ctx, instance) takes no field keyword arguments; "
            "pass the struct type instead to construct from fields"
        )
    return StructSource(ctx, initial, backend=backend)

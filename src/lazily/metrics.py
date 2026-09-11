"""Counter / gauge cell family — `lazily.metrics` (``#lzpypromegress``).

A metric here is a **reactive node**, not a mirror of one. A
:class:`MetricFamily` is a keyed collection of per-label-set children: a
writable :class:`CounterCell` / :class:`GaugeCell` backed by a
:class:`~lazily.cell.Source`, or a **derived** child bound with
:meth:`MetricFamily.derive`, whose value is a guarded
:class:`~lazily.signal.Computed` read straight off the graph. A derived child is
the point of the module: the scrape reads the graph, so there is no push, no
hand-written mirror, and no window in which the exported number disagrees with
the state it describes.

Exposition is dependency-free — :meth:`MetricsRegistry.render_text` emits the
Prometheus text format directly. The `prometheus_client` adapter lives in
:mod:`lazily.prometheus_egress`, imports its library lazily, and is a pull
collector for the same reason.

Example — replacing a hand-written mirror::

    metrics = MetricsRegistry(ctx)
    up = metrics.gauge(
        "service_up", "1 when the component is connected.", ("component",)
    )

    # No mirror code: the gauge IS the graph read.
    up.derive(("intake",), lambda c: 1.0 if c.read(intake_connected) else 0.0)

    accepted = metrics.counter("intake_accepted_total", "Accepted intake messages.")
    accepted.child().inc()

    metrics.render_text()
"""

from __future__ import annotations


__all__ = [
    "CounterCell",
    "GaugeCell",
    "MetricFamily",
    "MetricFamilySnapshot",
    "MetricKind",
    "MetricNameError",
    "MetricSample",
    "MetricsRegistry",
    "format_metric_value",
]

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from .collection import ComputedMap, SourceMap
from .signal import computed


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from .cell import Source


#: Prometheus metric-name grammar (the ``:`` is reserved for recording rules but
#: is legal in an exposed name).
_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
#: Prometheus label-name grammar. ``__``-prefixed names are reserved.
_LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class MetricNameError(ValueError):
    """A metric or label name that Prometheus would reject."""


class MetricKind(Enum):
    """The exported metric type."""

    COUNTER = "counter"
    GAUGE = "gauge"


@dataclass(frozen=True, slots=True)
class MetricSample:
    """One label set and its current value."""

    labels: tuple[str, ...]
    value: float


@dataclass(frozen=True, slots=True)
class MetricFamilySnapshot:
    """One family resolved at a point in time — what an exporter renders."""

    name: str
    documentation: str
    kind: MetricKind
    label_names: tuple[str, ...]
    samples: tuple[MetricSample, ...]


def _check_metric_name(name: str) -> str:
    if not _METRIC_NAME.match(name):
        raise MetricNameError(
            f"{name!r} is not a valid Prometheus metric name "
            f"(must match {_METRIC_NAME.pattern})"
        )
    return name


def _check_label_names(label_names: Iterable[str]) -> tuple[str, ...]:
    checked = tuple(label_names)
    for label in checked:
        if not _LABEL_NAME.match(label):
            raise MetricNameError(
                f"{label!r} is not a valid Prometheus label name "
                f"(must match {_LABEL_NAME.pattern})"
            )
        if label.startswith("__"):
            raise MetricNameError(f"label name {label!r} is reserved (``__`` prefix)")
    if len(set(checked)) != len(checked):
        raise MetricNameError(f"duplicate label name in {checked!r}")
    return checked


def format_metric_value(value: float) -> str:
    """Render ``value`` the way the Prometheus text format requires.

    Go's ``strconv`` spellings for the three non-finite values (``+Inf``,
    ``-Inf``, ``NaN``); everything else is a plain repr, so an integral float
    exports as ``1.0`` rather than ``1``.
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return repr(float(value))


def _escape_documentation(text: str) -> str:
    return text.replace("\\", r"\\").replace("\n", r"\n")


def _escape_label_value(text: str) -> str:
    return text.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


class CounterCell:
    """A **monotonic** counter backed by one :class:`~lazily.cell.Source`.

    Both writes are monotonic-guarded: :meth:`inc` refuses a negative delta and
    :meth:`set` refuses to move backwards, so mirroring an external absolute
    total can never publish a decrease the scrape would read as a counter reset.
    """

    __slots__ = ("_source",)

    def __init__(self, source: Source[float]) -> None:
        self._source = source

    @property
    def source(self) -> Source[float]:
        """The backing cell. Read it through a compute view to depend on it."""
        return self._source

    @property
    def value(self) -> float:
        """The current total, untracked."""
        return self._source.value

    def inc(self, amount: float = 1.0) -> None:
        """Add ``amount`` (default 1). Negative deltas are rejected."""
        if amount < 0:
            raise ValueError(f"counter increment must be >= 0, got {amount!r}")
        self._source.set(self._source.value + float(amount))

    def set(self, total: float) -> None:
        """Publish an externally-owned absolute total. Must not move backwards."""
        current = self._source.value
        if total < current:
            raise ValueError(
                f"counter total must be monotonic: {total!r} < current {current!r}"
            )
        self._source.set(float(total))

    def __repr__(self) -> str:
        return f"<CounterCell {self.value}>"


class GaugeCell:
    """A settable gauge backed by one :class:`~lazily.cell.Source`."""

    __slots__ = ("_source",)

    def __init__(self, source: Source[float]) -> None:
        self._source = source

    @property
    def source(self) -> Source[float]:
        """The backing cell. Read it through a compute view to depend on it."""
        return self._source

    @property
    def value(self) -> float:
        """The current value, untracked."""
        return self._source.value

    def set(self, value: float) -> None:
        """Publish ``value``. An equal write is inert (the cell guard)."""
        self._source.set(float(value))

    def inc(self, amount: float = 1.0) -> None:
        self._source.set(self._source.value + float(amount))

    def dec(self, amount: float = 1.0) -> None:
        self._source.set(self._source.value - float(amount))

    def __repr__(self) -> str:
        return f"<GaugeCell {self.value}>"


class MetricFamily:
    """One metric name and its per-label-set children.

    A child is either **writable** (`child` / `labels`, a `CounterCell` or
    `GaugeCell` over a `Source`) or **derived** (:meth:`derive`, a guarded
    `Computed` read off the graph). A label set holds at most one of the two;
    asking for the other kind at the same labels is an error, because the two
    disagree about who owns the value.
    """

    __slots__ = (
        "_ctx",
        "_derived",
        "_documentation",
        "_kind",
        "_label_names",
        "_name",
        "_sources",
    )

    def __init__(
        self,
        ctx: dict,
        name: str,
        documentation: str,
        kind: MetricKind,
        label_names: Iterable[str] = (),
    ) -> None:
        self._ctx = ctx
        self._name = _check_metric_name(name)
        self._documentation = documentation
        self._kind = kind
        self._label_names = _check_label_names(label_names)
        self._sources: SourceMap[tuple[str, ...], float] = SourceMap(ctx)
        self._derived: ComputedMap[tuple[str, ...], float] = ComputedMap(ctx)

    # -- identity ----------------------------------------------------------- #

    @property
    def name(self) -> str:
        return self._name

    @property
    def documentation(self) -> str:
        return self._documentation

    @property
    def kind(self) -> MetricKind:
        return self._kind

    @property
    def label_names(self) -> tuple[str, ...]:
        return self._label_names

    # -- children ----------------------------------------------------------- #

    def _key(self, values: tuple[Any, ...], named: dict[str, Any]) -> tuple[str, ...]:
        if values and named:
            raise TypeError("pass label values positionally or by name, not both")
        if named:
            missing = [n for n in self._label_names if n not in named]
            extra = [n for n in named if n not in self._label_names]
            if missing or extra:
                raise TypeError(
                    f"{self._name} labels are {self._label_names}; "
                    f"missing={tuple(missing)} unexpected={tuple(extra)}"
                )
            return tuple(str(named[n]) for n in self._label_names)
        if len(values) != len(self._label_names):
            raise TypeError(
                f"{self._name} expects {len(self._label_names)} label value(s) "
                f"{self._label_names}, got {len(values)}"
            )
        return tuple(str(v) for v in values)

    def labels(self, *values: Any, **named: Any) -> CounterCell | GaugeCell:
        """The writable child at this label set, minted on first access."""
        key = self._key(values, named)
        if self._derived.is_present(key):
            raise TypeError(
                f"{self._name}{list(key)} is a derived child; its value is owned "
                f"by the graph and cannot be written"
            )
        source = self._sources.entry(key, 0.0)
        return (
            CounterCell(source)
            if self._kind is MetricKind.COUNTER
            else GaugeCell(source)
        )

    def child(self) -> CounterCell | GaugeCell:
        """The child of an unlabeled family — ``labels()`` spelled for the eye."""
        return self.labels()

    def derive(
        self,
        labels: Iterable[Any] | None,
        compute: Callable[[Any], float],
        *,
        eager: bool = False,
    ) -> None:
        """Bind ``compute`` as the derived child at ``labels``.

        ``compute`` receives a compute view; read the graph through it
        (``c.read(cell)`` / ``cell(c).value``) and the exported number tracks
        that state with no mirror step. Pass ``None`` (or ``()``) for an
        unlabeled family.

        **Lazy by default**, which is the right default for a scrape: the
        expression runs when the metric is collected, not on every upstream
        change, so a graph that churns between scrapes costs nothing. The
        trade-off is that a lazy child has no settled value to compare, so a
        *reactive* consumer of :meth:`observe` is invalidated by any upstream
        change even when the exported number does not move.

        Pass ``eager=True`` when that matters. The child then keeps a settled
        value and the ``Computed`` guard applies: an upstream change that leaves
        the number alone stops at the child and never reaches its readers. The
        cost is that ``compute`` runs on every upstream change, scrape or no
        scrape.

        Either way ``compute`` runs **once here**, to materialize the child.
        """
        key = self._key(tuple(labels or ()), {})
        if self._sources.is_present(key):
            raise TypeError(
                f"{self._name}{list(key)} already has a writable child; remove it "
                f"before binding a derived one"
            )
        if self._derived.is_present(key):
            raise TypeError(f"{self._name}{list(key)} already has a derived child")
        # A ``ComputedMap`` entry is a Slot, which invalidates its dependents on
        # every reset regardless of value. Routing through a real ``Computed``
        # is what makes ``eager=True`` guard: only a settled value can be
        # compared, so only an eager child can suppress an equal recompute
        # before it reaches the entry's readers.
        node = computed(self._ctx, lambda view: float(compute(view)))
        if eager:
            node.eager()
        self._derived.get_or_insert_with(key, lambda view, _key: float(view.read(node)))

    def remove(self, *values: Any, **named: Any) -> bool:
        """Drop the child at this label set. Returns whether one was present."""
        key = self._key(values, named)
        return self._sources.remove(key) or self._derived.remove(key)

    def label_sets(self) -> list[tuple[str, ...]]:
        """Every present label set, writable children first, insertion-ordered."""
        return [*self._sources.present_keys(), *self._derived.present_keys()]

    # -- reads -------------------------------------------------------------- #

    def _samples(self, ctx: Any) -> Iterator[MetricSample]:
        for key in self._sources.present_keys():
            value = self._sources.get(key, ctx)
            yield MetricSample(key, 0.0 if value is None else float(value))
        for key in self._derived.present_keys():
            value = self._derived.get(key, ctx)
            yield MetricSample(key, 0.0 if value is None else float(value))

    def collect(self, ctx: Any = None) -> MetricFamilySnapshot:
        """Resolve every child into a snapshot.

        Untracked by default — a scrape is not a graph node. Pass a compute view
        to depend on every child instead (:meth:`observe`).
        """
        return MetricFamilySnapshot(
            name=self._name,
            documentation=self._documentation,
            kind=self._kind,
            label_names=self._label_names,
            samples=tuple(self._samples(ctx)),
        )

    def observe(self, compute: Any) -> MetricFamilySnapshot:
        """:meth:`collect` as a **tracked** read through ``compute``."""
        return self.collect(compute)

    def __repr__(self) -> str:
        return (
            f"<MetricFamily {self._name} {self._kind.value} "
            f"labels={self._label_names} children={len(self.label_sets())}>"
        )


class MetricsRegistry:
    """A named set of :class:`MetricFamily` objects plus text exposition."""

    __slots__ = ("_ctx", "_families")

    def __init__(self, ctx: dict) -> None:
        self._ctx = ctx
        self._families: dict[str, MetricFamily] = {}

    def _family(
        self,
        name: str,
        documentation: str,
        kind: MetricKind,
        label_names: Iterable[str],
    ) -> MetricFamily:
        existing = self._families.get(name)
        wanted = _check_label_names(label_names)
        if existing is not None:
            if existing.kind is not kind or existing.label_names != wanted:
                raise ValueError(
                    f"{name} is already registered as {existing.kind.value} with "
                    f"labels {existing.label_names}; cannot re-register as "
                    f"{kind.value} with labels {wanted}"
                )
            return existing
        family = MetricFamily(self._ctx, name, documentation, kind, wanted)
        self._families[name] = family
        return family

    def counter(
        self, name: str, documentation: str = "", label_names: Iterable[str] = ()
    ) -> MetricFamily:
        """Get or create the counter family ``name``."""
        return self._family(name, documentation, MetricKind.COUNTER, label_names)

    def gauge(
        self, name: str, documentation: str = "", label_names: Iterable[str] = ()
    ) -> MetricFamily:
        """Get or create the gauge family ``name``."""
        return self._family(name, documentation, MetricKind.GAUGE, label_names)

    def unregister(self, name: str) -> bool:
        """Drop the family ``name``. Returns whether one was registered."""
        return self._families.pop(name, None) is not None

    def families(self) -> list[MetricFamily]:
        """Every registered family, in registration order."""
        return list(self._families.values())

    def collect(self, ctx: Any = None) -> list[MetricFamilySnapshot]:
        """Snapshot every family. Untracked unless a compute view is passed."""
        return [family.collect(ctx) for family in self._families.values()]

    def render_text(self, ctx: Any = None) -> str:
        """The Prometheus text exposition format, with no third-party dependency.

        A family with no children emits its ``HELP``/``TYPE`` header and no
        samples, which is what an exporter is expected to do.
        """
        lines: list[str] = []
        for snapshot in self.collect(ctx):
            lines.append(
                f"# HELP {snapshot.name} "
                f"{_escape_documentation(snapshot.documentation)}"
            )
            lines.append(f"# TYPE {snapshot.name} {snapshot.kind.value}")
            for sample in snapshot.samples:
                if snapshot.label_names:
                    rendered = ",".join(
                        f'{label}="{_escape_label_value(value)}"'
                        for label, value in zip(
                            snapshot.label_names, sample.labels, strict=True
                        )
                    )
                    lines.append(
                        f"{snapshot.name}{{{rendered}}} "
                        f"{format_metric_value(sample.value)}"
                    )
                else:
                    lines.append(f"{snapshot.name} {format_metric_value(sample.value)}")
        return "".join(f"{line}\n" for line in lines)

    def __repr__(self) -> str:
        return f"<MetricsRegistry families={list(self._families)}>"

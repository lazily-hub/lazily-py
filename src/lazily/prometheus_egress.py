"""`prometheus_client` egress adapter (``#lzpypromegress``).

Registers a :class:`~lazily.metrics.MetricsRegistry` **off the reactive graph**
as a `prometheus_client` custom collector: at scrape time the collector resolves
the families, so a derived child is read from the graph on the scrape that
exports it. Nothing is pushed, nothing is mirrored, and there is no interval
during which the exported number and the state it describes disagree.

`prometheus_client` is an optional dependency (``pip install
"lazily[prometheus]"``). This module imports it lazily, inside the calls that
need it, so ``import lazily`` still loads no third-party package — the same rule
:mod:`lazily.struct_cell` follows.

Example::

    from prometheus_client import CollectorRegistry, generate_latest

    metrics = MetricsRegistry(ctx)
    metrics.gauge("service_up", "1 when connected.", ("component",)).derive(
        ("intake",), lambda c: 1.0 if c.read(intake_connected) else 0.0
    )

    prom = CollectorRegistry()
    collector = register_metrics(metrics, prom)
    generate_latest(prom)  # reads the graph, now
    unregister_metrics(collector, prom)
"""

from __future__ import annotations


__all__ = [
    "PrometheusUnavailableError",
    "ReactiveCollector",
    "register_metrics",
    "unregister_metrics",
]

from typing import TYPE_CHECKING, Any

from .metrics import MetricKind


if TYPE_CHECKING:
    from collections.abc import Iterator

    from .metrics import MetricsRegistry


class PrometheusUnavailableError(ImportError):
    """`prometheus_client` is not installed."""


def _prometheus_core() -> Any:
    try:
        from prometheus_client import core
    except ImportError as error:  # pragma: no cover - exercised via monkeypatch
        raise PrometheusUnavailableError(
            "prometheus_client is required for lazily.prometheus_egress; "
            'install it with `pip install "lazily[prometheus]"`'
        ) from error
    return core


class ReactiveCollector:
    """A `prometheus_client` collector that resolves a lazily registry on scrape.

    Implements the collector protocol (a ``collect()`` yielding metric
    families), so it works with any `prometheus_client` ``CollectorRegistry`` —
    including the process-global one — and with `multiprocess`-free exporters.

    Reads are **untracked**: a scrape is not a graph node, and subscribing the
    exporter to every metric would make a scrape invalidate the graph it reads.
    """

    __slots__ = ("_registry",)

    def __init__(self, registry: MetricsRegistry) -> None:
        self._registry = registry

    @property
    def registry(self) -> MetricsRegistry:
        """The lazily registry this collector exports."""
        return self._registry

    def collect(self) -> Iterator[Any]:
        """Yield one `prometheus_client` metric family per lazily family."""
        core = _prometheus_core()
        for snapshot in self._registry.collect():
            if snapshot.kind is MetricKind.COUNTER:
                # CounterMetricFamily strips a trailing ``_total`` and re-adds it
                # to the sample name, so a name given either way round-trips.
                family = core.CounterMetricFamily(
                    snapshot.name,
                    snapshot.documentation,
                    labels=list(snapshot.label_names),
                )
            else:
                family = core.GaugeMetricFamily(
                    snapshot.name,
                    snapshot.documentation,
                    labels=list(snapshot.label_names),
                )
            for sample in snapshot.samples:
                family.add_metric(list(sample.labels), sample.value)
            yield family

    def __repr__(self) -> str:
        return f"<ReactiveCollector {self._registry!r}>"


def register_metrics(
    registry: MetricsRegistry, prometheus_registry: Any = None
) -> ReactiveCollector:
    """Register ``registry`` with a `prometheus_client` registry and return the
    collector (keep it — :func:`unregister_metrics` needs it).

    ``prometheus_registry`` defaults to `prometheus_client`'s process-global
    ``REGISTRY``.
    """
    collector = ReactiveCollector(registry)
    target = _resolve_registry(prometheus_registry)
    target.register(collector)
    return collector


def unregister_metrics(
    collector: ReactiveCollector, prometheus_registry: Any = None
) -> None:
    """Remove a collector previously passed to :func:`register_metrics`."""
    _resolve_registry(prometheus_registry).unregister(collector)


def _resolve_registry(prometheus_registry: Any) -> Any:
    if prometheus_registry is not None:
        return prometheus_registry
    try:
        from prometheus_client import REGISTRY
    except ImportError as error:  # pragma: no cover - exercised via monkeypatch
        raise PrometheusUnavailableError(
            "prometheus_client is required for lazily.prometheus_egress; "
            'install it with `pip install "lazily[prometheus]"`'
        ) from error
    return REGISTRY

"""Counter / gauge cell family and the prometheus egress adapter
(``#lzpypromegress``).

The property the family exists for: a derived child is a graph read, so the
exported number and the state it describes cannot disagree, and nothing has to
be mirrored on a timer.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from lazily import (
    CounterCell,
    GaugeCell,
    MetricKind,
    MetricNameError,
    MetricsRegistry,
    computed,
    format_metric_value,
    source,
)


def _counting_view(ctx: dict, read) -> tuple[object, dict[str, int]]:
    runs = {"n": 0}

    def body(compute):
        runs["n"] += 1
        return read(compute)

    return computed(ctx, body).eager(), runs


# -- writable children --------------------------------------------------------


def test_counter_increments_and_mirrors_a_monotonic_total() -> None:
    metrics = MetricsRegistry({})
    accepted = metrics.counter("accepted_total", "Accepted.", ("source",))
    child = accepted.labels("http")
    assert isinstance(child, CounterCell)

    child.inc()
    child.inc(4)
    assert child.value == 5.0

    child.set(11)  # an externally-owned absolute total
    assert child.value == 11.0


def test_counter_refuses_to_move_backwards() -> None:
    metrics = MetricsRegistry({})
    child = metrics.counter("accepted_total").child()
    child.inc(3)
    with pytest.raises(ValueError, match="must be >= 0"):
        child.inc(-1)
    with pytest.raises(ValueError, match="must be monotonic"):
        child.set(2)
    assert child.value == 3.0


def test_gauge_set_inc_dec() -> None:
    metrics = MetricsRegistry({})
    depth = metrics.gauge("depth").child()
    assert isinstance(depth, GaugeCell)
    depth.set(5)
    depth.inc(2)
    depth.dec()
    assert depth.value == 6.0


def test_equal_gauge_write_is_inert() -> None:
    ctx: dict = {}
    metrics = MetricsRegistry(ctx)
    depth = metrics.gauge("depth").child()
    view, runs = _counting_view(ctx, lambda c: c.read(depth.source))
    assert view.value == 0.0
    assert runs["n"] == 1

    depth.set(0)
    assert runs["n"] == 1  # guarded at the cell

    depth.set(3)
    assert view.value == 3.0
    assert runs["n"] == 2


def test_child_is_stable_across_lookups() -> None:
    metrics = MetricsRegistry({})
    family = metrics.gauge("depth", "", ("component",))
    family.labels("intake").set(4)
    assert family.labels("intake").value == 4.0
    assert family.labels(component="intake").value == 4.0


def test_label_arity_and_names_are_checked() -> None:
    metrics = MetricsRegistry({})
    family = metrics.gauge("depth", "", ("component", "queue"))
    with pytest.raises(TypeError, match="expects 2 label value"):
        family.labels("intake")
    with pytest.raises(TypeError, match="unexpected="):
        family.labels(component="intake", nope="x")
    with pytest.raises(TypeError, match="positionally or by name"):
        family.labels("intake", queue="main")


def test_label_values_are_stringified() -> None:
    metrics = MetricsRegistry({})
    family = metrics.gauge("depth", "", ("shard",))
    family.labels(3).set(1)
    assert family.label_sets() == [("3",)]


# -- derived children ---------------------------------------------------------


def test_derived_child_is_a_graph_read_not_a_mirror() -> None:
    ctx: dict = {}
    metrics = MetricsRegistry(ctx)
    connected = source(lambda c: False)

    up = metrics.gauge("service_up", "1 when connected.", ("component",))
    up.derive(("intake",), lambda c: 1.0 if connected(c).value else 0.0)

    assert up.collect().samples[0].value == 0.0
    connected(ctx).value = True
    # No mirror step ran between these two lines.
    assert up.collect().samples[0].value == 1.0


def test_eager_derived_child_is_guarded() -> None:
    ctx: dict = {}
    metrics = MetricsRegistry(ctx)
    raw = source(lambda c: 10)
    family = metrics.gauge("tens")
    family.derive(None, lambda c: float(raw(c).value // 10), eager=True)

    view, runs = _counting_view(ctx, lambda c: family.observe(c).samples[0].value)
    assert view.value == 1.0
    assert runs["n"] == 1

    raw(ctx).value = 15  # still 1 after the floor divide
    assert view.value == 1.0
    assert runs["n"] == 1  # the guard stopped it at the settled Computed

    raw(ctx).value = 20
    assert view.value == 2.0
    assert runs["n"] == 2


def test_lazy_derived_child_computes_only_when_collected() -> None:
    ctx: dict = {}
    metrics = MetricsRegistry(ctx)
    raw = source(lambda c: 10)
    runs = {"n": 0}

    def body(compute):
        runs["n"] += 1
        return float(raw(compute).value // 10)

    family = metrics.gauge("tens")
    family.derive(None, body)
    assert runs["n"] == 1  # minting the child materializes it once

    assert family.collect().samples[0].value == 1.0
    assert runs["n"] == 1  # cached

    raw(ctx).value = 15
    assert runs["n"] == 1  # a lazy child does not recompute on an upstream change
    assert family.collect().samples[0].value == 1.0
    assert runs["n"] == 2  # it recomputes on the collect that needs it


def test_lazy_derived_child_has_no_settled_value_to_guard_with() -> None:
    """The documented trade-off, asserted rather than described.

    A lazy child holds no settled value, so an upstream change that leaves the
    exported number alone still reaches a reactive consumer of ``observe``.
    ``eager=True`` is the opt-in that buys the guard.
    """
    ctx: dict = {}
    metrics = MetricsRegistry(ctx)
    raw = source(lambda c: 10)
    family = metrics.gauge("tens")
    family.derive(None, lambda c: float(raw(c).value // 10))

    view, runs = _counting_view(ctx, lambda c: family.observe(c).samples[0].value)
    assert view.value == 1.0
    assert runs["n"] == 1

    raw(ctx).value = 15
    assert view.value == 1.0
    assert runs["n"] == 2  # recomputed, unlike the eager child above


def test_derived_and_writable_children_cannot_share_a_label_set() -> None:
    ctx: dict = {}
    metrics = MetricsRegistry(ctx)
    family = metrics.gauge("depth", "", ("component",))
    family.derive(("intake",), lambda c: 1.0)

    with pytest.raises(TypeError, match="derived child"):
        family.labels("intake")
    with pytest.raises(TypeError, match="already has a derived child"):
        family.derive(("intake",), lambda c: 2.0)

    family.labels("dispatcher").set(3)
    with pytest.raises(TypeError, match="already has a writable child"):
        family.derive(("dispatcher",), lambda c: 4.0)


def test_remove_drops_either_kind() -> None:
    ctx: dict = {}
    metrics = MetricsRegistry(ctx)
    family = metrics.gauge("depth", "", ("component",))
    family.labels("intake").set(1)
    family.derive(("dispatcher",), lambda c: 2.0)
    assert sorted(family.label_sets()) == [("dispatcher",), ("intake",)]

    assert family.remove("intake") is True
    assert family.remove("dispatcher") is True
    assert family.remove("intake") is False
    assert family.label_sets() == []


# -- registry -----------------------------------------------------------------


def test_registry_get_or_create_is_stable() -> None:
    metrics = MetricsRegistry({})
    first = metrics.counter("accepted_total", "Accepted.", ("source",))
    assert metrics.counter("accepted_total", "Accepted.", ("source",)) is first
    assert [f.name for f in metrics.families()] == ["accepted_total"]


def test_registry_rejects_a_conflicting_redeclaration() -> None:
    metrics = MetricsRegistry({})
    metrics.gauge("depth", "", ("component",))
    with pytest.raises(ValueError, match="already registered as gauge"):
        metrics.counter("depth")
    with pytest.raises(ValueError, match="already registered as gauge"):
        metrics.gauge("depth", "", ("other",))


def test_unregister() -> None:
    metrics = MetricsRegistry({})
    metrics.gauge("depth")
    assert metrics.unregister("depth") is True
    assert metrics.unregister("depth") is False
    assert metrics.families() == []


@pytest.mark.parametrize("name", ["0bad", "with-dash", "with space", ""])
def test_invalid_metric_names_are_rejected(name: str) -> None:
    with pytest.raises(MetricNameError, match="metric name"):
        MetricsRegistry({}).gauge(name)


def test_invalid_label_names_are_rejected() -> None:
    metrics = MetricsRegistry({})
    with pytest.raises(MetricNameError, match="label name"):
        metrics.gauge("depth", "", ("with-dash",))
    with pytest.raises(MetricNameError, match="reserved"):
        metrics.gauge("depth2", "", ("__reserved",))
    with pytest.raises(MetricNameError, match="duplicate"):
        metrics.gauge("depth3", "", ("a", "a"))


# -- text exposition ----------------------------------------------------------


def test_render_text_emits_help_type_and_samples() -> None:
    ctx: dict = {}
    metrics = MetricsRegistry(ctx)
    metrics.counter("accepted_total", "Accepted messages.", ("source",)).labels(
        "http"
    ).inc(3)
    metrics.gauge("depth", "Pending items.").child().set(7)

    assert metrics.render_text() == (
        "# HELP accepted_total Accepted messages.\n"
        "# TYPE accepted_total counter\n"
        'accepted_total{source="http"} 3.0\n'
        "# HELP depth Pending items.\n"
        "# TYPE depth gauge\n"
        "depth 7.0\n"
    )


def test_render_text_emits_a_header_for_an_empty_family() -> None:
    metrics = MetricsRegistry({})
    metrics.gauge("depth", "Pending items.")
    assert metrics.render_text() == (
        "# HELP depth Pending items.\n# TYPE depth gauge\n"
    )


def test_render_text_escapes_documentation_and_label_values() -> None:
    metrics = MetricsRegistry({})
    family = metrics.gauge("depth", "line\nbreak and \\ slash", ("path",))
    family.labels('a"b\\c\nd').set(1)
    rendered = metrics.render_text()
    assert "# HELP depth line\\nbreak and \\\\ slash\n" in rendered
    assert 'depth{path="a\\"b\\\\c\\nd"} 1.0\n' in rendered


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, "1.0"),
        (1.5, "1.5"),
        (0, "0.0"),
        (float("inf"), "+Inf"),
        (float("-inf"), "-Inf"),
        (float("nan"), "NaN"),
    ],
)
def test_format_metric_value(value: float, expected: str) -> None:
    assert format_metric_value(value) == expected


def test_non_finite_values_render(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics = MetricsRegistry({})
    metrics.gauge("ratio").child().set(float("inf"))
    assert "ratio +Inf\n" in metrics.render_text()


def test_snapshot_carries_kind_and_label_names() -> None:
    metrics = MetricsRegistry({})
    metrics.counter("accepted_total", "Accepted.", ("source",)).labels("http").inc()
    snapshot = metrics.collect()[0]
    assert snapshot.kind is MetricKind.COUNTER
    assert snapshot.label_names == ("source",)
    assert snapshot.samples[0].labels == ("http",)


# -- prometheus_client egress -------------------------------------------------


def test_importing_lazily_does_not_import_prometheus_client() -> None:
    code = (
        "import sys, lazily\n"
        "lazily.register_metrics\n"
        "assert 'prometheus_client' not in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_egress_raises_a_named_error_when_prometheus_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lazily.prometheus_egress import PrometheusUnavailableError, ReactiveCollector

    monkeypatch.setitem(sys.modules, "prometheus_client", None)
    collector = ReactiveCollector(MetricsRegistry({}))
    with pytest.raises(PrometheusUnavailableError, match="lazily\\[prometheus\\]"):
        list(collector.collect())


def test_scrape_reads_the_graph_through_the_collector() -> None:
    pytest.importorskip("prometheus_client", reason="lazily[prometheus] not installed")
    from prometheus_client import CollectorRegistry, generate_latest

    from lazily import register_metrics, unregister_metrics

    ctx: dict = {}
    metrics = MetricsRegistry(ctx)
    connected = source(lambda c: False)
    metrics.gauge("service_up", "1 when connected.", ("component",)).derive(
        ("intake",), lambda c: 1.0 if connected(c).value else 0.0
    )

    prom = CollectorRegistry()
    collector = register_metrics(metrics, prom)
    assert b'service_up{component="intake"} 0.0' in generate_latest(prom)

    connected(ctx).value = True
    # Same collector, no push, no mirror: the scrape resolves the graph.
    assert b'service_up{component="intake"} 1.0' in generate_latest(prom)

    unregister_metrics(collector, prom)
    assert generate_latest(prom) == b""


def test_collector_output_matches_the_dependency_free_renderer() -> None:
    pytest.importorskip("prometheus_client", reason="lazily[prometheus] not installed")
    from prometheus_client import CollectorRegistry, generate_latest

    from lazily import register_metrics

    metrics = MetricsRegistry({})
    metrics.counter("accepted_total", "Accepted messages.", ("source",)).labels(
        "http"
    ).inc(3)
    metrics.gauge("depth", "Pending items.").child().set(7)

    prom = CollectorRegistry()
    register_metrics(metrics, prom)
    assert generate_latest(prom).decode() == metrics.render_text()


@pytest.mark.parametrize("name", ["accepted_total", "accepted"])
def test_counter_name_round_trips_with_or_without_the_total_suffix(name: str) -> None:
    pytest.importorskip("prometheus_client", reason="lazily[prometheus] not installed")
    from prometheus_client import CollectorRegistry, generate_latest

    from lazily import register_metrics

    metrics = MetricsRegistry({})
    metrics.counter(name, "Accepted.").child().inc(2)
    prom = CollectorRegistry()
    register_metrics(metrics, prom)
    assert b"accepted_total 2.0" in generate_latest(prom)


def test_register_metrics_defaults_to_the_global_registry() -> None:
    pytest.importorskip("prometheus_client", reason="lazily[prometheus] not installed")
    from prometheus_client import REGISTRY, generate_latest

    from lazily import register_metrics, unregister_metrics

    metrics = MetricsRegistry({})
    metrics.gauge("lazily_global_probe", "Probe.").child().set(1)
    collector = register_metrics(metrics)
    try:
        assert b"lazily_global_probe 1.0" in generate_latest(REGISTRY)
    finally:
        unregister_metrics(collector)
    assert b"lazily_global_probe" not in generate_latest(REGISTRY)

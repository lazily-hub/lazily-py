"""Large-graph scale benchmark for lazily-py.

Replicates the lazily-rs ``scale`` group (``benches/scale.rs``) and the
lazily-go ``scale`` group (``scale_bench_test.go``) on a spreadsheet-shaped
graph: ``N`` input cells plus ``N`` formula slots, where
``formula[i] = input[i] + input[i - 1]`` (local fan-in, like a column of
``=A_i + A_{i-1}``). With the default ``N = 1_000_000`` that is
**~2,000,000 reactive nodes**.

Four scenarios cover the spreadsheet lifecycle:

* ``build`` — construct all ``2N`` nodes (formulas lazy, not yet computed).
* ``cold_full_recalc`` — first read of every formula (forces every compute +
  edge-tracking).
* ``viewport_recalc`` — edit one input, read only a bounded viewport (the
  lazy-pull win: off-viewport formulas stay dirty and never recompute).
* ``full_recalc_invalidate_all`` — touch every input, then read every formula
  (worst-case full-sheet edit).

Run as a script or module::

    uv run python -m lazily.scale_bench
    LAZILY_SCALE_N=1000000 uv run python -m lazily.scale_bench
    LAZILY_SCALE_N=5000000 uv run python -m lazily.scale_bench   # Google Sheets 10M-cell workbook
    LAZILY_SCALE_VIEWPORT=1000 uv run python -m lazily.scale_bench

Note: CPython is far heavier per node than Rust/Go — every ``Cell``/``Slot`` is
a Python object with its own dependency sets, so both time and memory grow
much faster with ``N``. Pick the largest ``N`` that fits your machine.
"""

from __future__ import annotations


__all__ = [
    "ScaleGraph",
    "ScaleResult",
    "build_scale_graph",
    "run_scale_benchmarks",
    "scale_n",
    "scale_viewport",
]


import gc
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lazily import Cell, Slot


if TYPE_CHECKING:
    from collections.abc import Callable


def scale_n() -> int:
    """Graph size ``N`` (input cells and formula slots), from ``LAZILY_SCALE_N``."""
    raw = os.environ.get("LAZILY_SCALE_N", "")
    try:
        value = int(raw)
    except ValueError:
        return 1_000_000
    return value if value > 0 else 1_000_000


def scale_viewport(n: int) -> int:
    """Viewport width, from ``LAZILY_SCALE_VIEWPORT`` (default 1000, capped at ``n``)."""
    raw = os.environ.get("LAZILY_SCALE_VIEWPORT", "")
    try:
        value = int(raw)
    except ValueError:
        value = 1_000
    if value <= 0:
        value = 1_000
    return min(value, n)


@dataclass
class ScaleGraph:
    """The spreadsheet-shaped graph (formulas lazy until first read)."""

    ctx: dict
    inputs: list[Cell[int]]
    formulas: list[Slot[dict, dict, int]]


def _make_formula(a: Cell[int], b: Cell[int]) -> Callable[[dict], int]:
    # formula[i] = input[i] + input[i - 1]; reads go through Cell.value so the
    # running Slot is auto-tracked as a parent (edge discovery).
    def compute(_: dict) -> int:
        return a.value + b.value

    return compute


def build_scale_graph(n: int) -> ScaleGraph:
    """Construct ``N`` input cells + ``N`` formula slots (formulas not yet computed)."""
    ctx: dict = {}
    inputs: list[Cell[int]] = [Cell(ctx, i) for i in range(n)]
    formulas: list[Slot[dict, dict, int]] = []
    append = formulas.append
    for i in range(n):
        a = inputs[i]
        b = inputs[i - 1] if i > 0 else inputs[0]
        append(Slot(callable=_make_formula(a, b)))
    return ScaleGraph(ctx=ctx, inputs=inputs, formulas=formulas)


def read_all_formulas(g: ScaleGraph) -> int:
    ctx = g.ctx
    acc = 0
    for f in g.formulas:
        acc += f(ctx)
    return acc


@dataclass(frozen=True)
class ScaleResult:
    """One scale-scenario measurement."""

    name: str
    n: int
    total_seconds: float
    cells: int
    per_cell_ns: float | None
    detail: str = ""

    def __str__(self) -> str:
        cell_txt = (
            "—" if self.per_cell_ns is None else f"{self.per_cell_ns:>8.1f} ns/cell"
        )
        return (
            f"{self.name:<28} N={self.n:>10_d}  "
            f"{self.total_seconds * 1000.0:>10.3f} ms  {cell_txt}  {self.detail}"
        )


def _bench_build(n: int) -> ScaleResult:
    gc.collect()
    start = time.perf_counter()
    g = build_scale_graph(n)
    elapsed = time.perf_counter() - start
    cells = 2 * n
    # keep alive
    assert len(g.formulas) == n
    return ScaleResult(
        name="build",
        n=n,
        total_seconds=elapsed,
        cells=cells,
        per_cell_ns=elapsed / cells * 1e9,
        detail=f"{cells:_d} nodes",
    )


def _bench_cold_full_recalc(n: int) -> ScaleResult:
    g = build_scale_graph(n)
    gc.collect()
    start = time.perf_counter()
    sink = read_all_formulas(g)
    elapsed = time.perf_counter() - start
    assert sink >= 0
    return ScaleResult(
        name="cold_full_recalc",
        n=n,
        total_seconds=elapsed,
        cells=n,
        per_cell_ns=elapsed / n * 1e9,
        detail=f"{n:_d} formulas",
    )


def _bench_viewport_recalc(n: int, samples: int = 1_000) -> ScaleResult:
    vp = scale_viewport(n)
    g = build_scale_graph(n)
    read_all_formulas(g)  # warm the whole sheet once
    ctx = g.ctx
    mid = n // 2
    lo = max(0, mid - vp // 2)
    hi = min(n, lo + vp)
    window = g.formulas[lo:hi]
    mid_input = g.inputs[mid]
    gc.collect()
    acc = 0
    start = time.perf_counter()
    for i in range(samples):
        mid_input.value = (
            i + 1
        )  # edit one input (monotonic value passes the PartialEq guard)
        for f in window:
            acc += f(ctx)
    elapsed = time.perf_counter() - start
    assert acc >= 0  # defeat dead-code elimination of the viewport reads
    per_op_us = elapsed / samples * 1e6
    return ScaleResult(
        name="viewport_recalc",
        n=n,
        total_seconds=elapsed / samples,
        cells=vp,
        per_cell_ns=None,
        detail=f"{per_op_us:.2f} us/edit, viewport={vp}, {samples} edits",
    )


def _bench_full_recalc_invalidate_all(n: int) -> ScaleResult:
    g = build_scale_graph(n)
    read_all_formulas(g)  # warm once
    inputs = g.inputs
    gc.collect()
    start = time.perf_counter()
    for j, c in enumerate(inputs):
        c.value = j + 1  # touch every input (immediate invalidation)
    sink = read_all_formulas(g)
    elapsed = time.perf_counter() - start
    assert sink >= 0
    return ScaleResult(
        name="full_recalc_invalidate_all",
        n=n,
        total_seconds=elapsed,
        cells=n,
        per_cell_ns=elapsed / n * 1e9,
        detail=f"{n:_d} inputs re-set + {n:_d} formulas recomputed",
    )


def run_scale_benchmarks(n: int | None = None) -> list[ScaleResult]:
    """Run all four scale scenarios at size ``n`` (default from ``LAZILY_SCALE_N``)."""
    if n is None:
        n = scale_n()
    return [
        _bench_build(n),
        _bench_cold_full_recalc(n),
        _bench_viewport_recalc(n),
        _bench_full_recalc_invalidate_all(n),
    ]


def main() -> None:
    n = scale_n()
    print(f"lazily-py scale benchmarks (N={n:_d}, {2 * n:_d} reactive nodes)")
    for result in run_scale_benchmarks(n):
        print(result)


if __name__ == "__main__":
    main()

"""Instrumentation / benchmarks — :mod:`lazily.benchmarks`.

Microbenchmarks for the reactive core, keyed reconciliation, the keyed cell
collections, and the CRDT plane — the in-process surfaces where proportional-to-
the-diff cost is the load-bearing invariant. Run as a script
(``python -m lazily.benchmarks``) or via :func:`run_benchmarks`; each entry
reports a sample size, total elapsed, and per-op time.
"""

from __future__ import annotations


__all__ = ["Benchmark", "BenchmarkResult", "run_benchmarks", "time_op"]


import time
from dataclasses import dataclass
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class BenchmarkResult:
    """One benchmark measurement."""

    name: str
    samples: int
    total_seconds: float
    per_op_seconds: float

    def __str__(self) -> str:
        per_us = self.per_op_seconds * 1_000_000.0
        return (
            f"{self.name:<36} {self.samples:>10_d} ops  "
            f"{self.total_seconds * 1000.0:>8.3f} ms  {per_us:>9.3f} us/op"
        )


def time_op(name: str, op: Callable[[], None], samples: int) -> BenchmarkResult:
    """Run ``op`` ``samples`` times and report the wall-clock result."""
    start = time.perf_counter()
    for _ in range(samples):
        op()
    elapsed = time.perf_counter() - start
    return BenchmarkResult(
        name=name,
        samples=samples,
        total_seconds=elapsed,
        per_op_seconds=elapsed / samples if samples else 0.0,
    )


@dataclass
class Benchmark:
    """One named benchmark entry."""

    name: str
    run: Callable[[], BenchmarkResult]


def _bench_slot_read(n: int) -> BenchmarkResult:
    from lazily import CellSlot, slot

    ctx: dict = {}
    name = CellSlot[dict, dict, str]()

    @slot
    def derived(c: dict) -> str:
        return f"v-{name(c).value}"

    name(ctx).value = "seed"
    derived(ctx)  # warm the cache
    return time_op("slot.cached_read", lambda: derived(ctx), n)


def _bench_slot_invalidate(n: int) -> BenchmarkResult:
    from lazily import CellSlot, slot

    ctx: dict = {}
    counter = CellSlot[dict, dict, int]()

    @slot
    def doubled(c: dict) -> int:
        return counter(c).value * 2

    counter(ctx).value = 0
    i = 0

    def step() -> None:
        nonlocal i
        i += 1
        counter(ctx).value = i
        doubled(ctx)

    return time_op("slot.invalidate_recompute", step, n)


def _bench_reconcile(n: int) -> BenchmarkResult:
    from lazily import Level, reconcile_ops

    # The LIS kernel is definitional (longest, not greedy), so it is O(2^n) in
    # the worst case — keep the level small enough to stay fast in a microbench.
    prior: Level[str, int] = Level(
        order=[f"k{i}" for i in range(10)],
        values={f"k{i}": i for i in range(10)},
    )
    target: Level[str, int] = Level(
        order=[f"k{i}" for i in range(1, 9)] + ["k0", "k9"],
        values={f"k{i}": i for i in range(10)},
    )
    return time_op(
        "reconcile.lis_move_minimized",
        lambda: reconcile_ops(prior, target),  # type: ignore[arg-type]
        n,
    )


def _bench_cellmap_insert(n: int) -> BenchmarkResult:
    from lazily import CellMap

    def step() -> None:
        cmap = CellMap({})
        for i in range(50):
            cmap.insert(i, i)

    return time_op("cellmap.insert_50", step, n)


def _bench_textcrdt_merge(n: int) -> BenchmarkResult:
    from lazily import TextCrdt

    a = TextCrdt.seed(1, "the quick brown fox")
    b = TextCrdt.seed(2, "jumps over the lazy dog")
    return time_op("textcrdt.merge_disjoint", lambda: a.merge(b), n)


def _bench_crdt_plane_apply(n: int) -> BenchmarkResult:
    from lazily import CrdtOp, CrdtPlaneRuntime, WireStamp
    from lazily.ipc import IpcValue_Inline

    plane = CrdtPlaneRuntime(self_peer=0)
    op = CrdtOp.new(1, WireStamp(10, 0, 1), IpcValue_Inline(b"x"))
    return time_op("crdt_plane.idempotent_apply", lambda: plane.apply(op), n)


def run_benchmarks(samples: int = 10_000) -> list[BenchmarkResult]:
    """Run the full benchmark suite and return each result.

    The suite exercises the reactive core (cached read + invalidate/recompute),
    keyed reconciliation (LIS move-minimized diff), :class:`CellMap` insertion,
    :class:`TextCrdt` merge, and the :class:`CrdtPlaneRuntime` idempotent apply.
    """
    entries = [
        _bench_slot_read,
        _bench_slot_invalidate,
        _bench_reconcile,
        _bench_cellmap_insert,
        _bench_textcrdt_merge,
        _bench_crdt_plane_apply,
    ]
    return [entry(samples) for entry in entries]


def main() -> None:
    print("lazily-py benchmarks")
    for result in run_benchmarks():
        print(result)


if __name__ == "__main__":
    main()

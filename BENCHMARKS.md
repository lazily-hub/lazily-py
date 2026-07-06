# lazily-py Benchmarks

Wall-clock benchmarks for the lazily-py hot paths. Two suites:

- **Micro-benchmarks** — the in-library suite in
  [`src/lazily/benchmarks.py`](src/lazily/benchmarks.py) (`run_benchmarks()`),
  covering the reactive core, keyed reconciliation, `CellMap`, `TextCrdt`, and
  the CRDT plane.
- **Scale** — a large spreadsheet-shaped graph
  ([`src/lazily/scale_bench.py`](src/lazily/scale_bench.py)) mirroring the
  lazily-rs [`scale`](https://github.com/lazily-hub/lazily-rs/blob/main/benches/scale.rs)
  and lazily-go `scale` groups.

All timings use `time.perf_counter()`. Lower is better.

## Reproduce

```bash
make bench          # micro-suite
make bench-scale    # scale suite at the default N = 1,000,000

# or directly:
uv run python -m lazily.benchmarks
uv run python -m lazily.scale_bench

# scale at a specific size / viewport:
LAZILY_SCALE_N=1000000 uv run python -m lazily.scale_bench
LAZILY_SCALE_N=5000000 uv run python -m lazily.scale_bench   # Google Sheets 10M-cell workbook
LAZILY_SCALE_VIEWPORT=1000 uv run python -m lazily.scale_bench
```

Treat the absolute numbers as indicative — the shapes (relative costs,
size-scaling behavior) are what transfer across runs. **CPython is far heavier
per node than Rust or Go**: every `Cell` and `Slot` is a Python object carrying
its own dependency sets, so both time and memory grow much faster with `N` than
in the compiled bindings. These numbers are reported honestly, not to claim
parity.

### Hardware / environment

| | |
|---|---|
| CPU | AMD Ryzen 9 9950X3D (16 cores / 32 threads) |
| RAM | 186 GiB |
| OS | Linux 7.1.1 (CachyOS), x86-64 |
| Python | 3.12.12 (CPython) |

## Micro-benchmark results

10,000 samples per entry (`run_benchmarks()` default).

| Benchmark | µs/op | What it measures |
|-----------|------:|------------------|
| `slot.cached_read` | 0.076 | Cached (memoized) slot read — the steady-state pull with no recompute. |
| `slot.invalidate_recompute` | 0.806 | Set a cell, then re-pull a dependent slot (edge re-tracking + recompute). |
| `reconcile.lis_move_minimized` | 159.7 | LIS move-minimized keyed reconcile over a 10-key level (definitional longest-subsequence kernel, not greedy). |
| `cellmap.insert_50` | 43.6 | Build a `CellMap` and insert 50 keyed entries (whole-collection construction, not per-insert). |
| `textcrdt.merge_disjoint` | 1.345 | Merge two disjoint `TextCrdt` documents (Fugue/RGA order recomputed). |
| `crdt_plane.idempotent_apply` | 0.087 | Re-apply an already-seen op to a `CrdtPlaneRuntime` (idempotent dedupe path). |

### Notes

- The reactive steady state is cheap: a cached slot read is ~76 ns, and an
  invalidate + recompute round trip is ~0.8 µs even under CPython's
  interpreter overhead.
- `reconcile.lis_move_minimized` is the heaviest micro-path because the LIS
  kernel is *definitional* (longest subsequence, not greedy) — it is
  exponential in the worst case, so the bench keeps the level at 10 keys.
- `cellmap.insert_50` measures whole-collection construction (50 inserts +
  allocation), not a single insert — divide by 50 for per-insert.

## Scale (≥1M cells) — spreadsheet-shaped graph

Replicates the lazily-rs `scale` group on a spreadsheet-shaped graph: `N` input
cells + `N` formula slots where `formula[i] = input[i] + input[i-1]` (local
fan-in, like a column of `=A_i + A_{i-1}`). With the default `N = 1,000,000`
that is **~2,000,000 reactive nodes**. Four scenarios cover the spreadsheet
lifecycle:

- `build` — construct all `2N` nodes (formulas lazy, not yet computed).
- `cold_full_recalc` — first read of every formula (forces every compute + edge-tracking).
- `viewport_recalc` — edit one input, read only a 1,000-cell viewport.
- `full_recalc_invalidate_all` — re-set every input, then read every formula.

> **A "cell count" here counts two cells per row** — the graph models a column of
> formulas `=A_i + A_{i-1}`, so each row is **one input cell `A_i` plus one
> formula cell**. `N` rows ⇒ `N` inputs + `N` formulas = `2N` cells.

### 1,000,000 rows (~2M cells / nodes)

Peak RSS ~1.65 GiB.

| Benchmark | Time | Per cell | What it measures |
|-----------|-----:|---------:|------------------|
| `build` | 10.58 s | ~5.29 µs | Construct all 2N nodes (each `Cell`+`Slot` allocates its own dependency sets — allocation- and GC-bound under CPython). |
| `cold_full_recalc` | 382 ms | ~382 ns | First read of every formula — forces every compute + edge-tracking. |
| `viewport_recalc` | **72.9 µs** | — | Edit one input, read only a 1,000-cell viewport. ~5,240× cheaper than a full cold recalc. |
| `full_recalc_invalidate_all` | 1.40 s | ~1.40 µs | Re-set every input, then recompute the whole sheet (worst-case full-sheet edit). |

### 5,000,000 rows (10M cells — a full Google Sheets workbook)

Google Sheets caps a workbook at **10,000,000 cells**. Modeled as 5,000,000
input cells + 5,000,000 formula cells (`LAZILY_SCALE_N=5000000`). This is the
**largest size actually measured** — no extrapolation. Peak RSS ~8.0 GiB.

| Benchmark | Time | Per cell | What it measures |
|-----------|-----:|---------:|------------------|
| `build` | 56.4 s | ~5.64 µs | Build the full 10M-node workbook (allocation/GC-bound). |
| `cold_full_recalc` | 2.88 s | ~576 ns | Compute all 5M formulas cold. |
| `viewport_recalc` | **74.8 µs** | — | Edit one input, read a 1,000-cell viewport. ~38,500× cheaper than a full cold recalc. |
| `full_recalc_invalidate_all` | 8.27 s | ~1.65 µs | Re-set every input, recompute the whole workbook. |

So lazily-py backs a **full-capacity Google Sheets workbook** on CPython:
building it is the expensive part (~56 s, allocation/GC-bound — one Python
object per node), but once built, a full cold recompute is ~2.9 s, and a
one-cell edit + bounded-viewport read stays in the **~75 µs range**. The lazy
pull-based model leaves off-viewport formulas dirty and never recomputes them —
only ~2 formulas actually recompute per edit (the two that read the edited
input), regardless of sheet size, which is exactly the property a
viewport-rendered spreadsheet needs.

### Spreadsheet cell-count context

| Spreadsheet | Documented limit | Cells |
|-------------|------------------|------:|
| Google Sheets | 10,000,000 cells per workbook (18,278 columns max) | 10,000,000 |
| Microsoft Excel | 1,048,576 rows × 16,384 columns per worksheet | 17,179,869,184 |

The `LAZILY_SCALE_N=5000000` run above covers a full Google Sheets workbook. A
grid-complete Excel worksheet (17 billion cells) is unrepresentative — real
sheets populate a tiny fraction of the grid, and lazily stores only the cells
you create, so the `scale` group measures the populated-cell path that matters.

## Notes

### CPython per-node overhead

Build dominates the lifecycle here (~5.3–5.6 µs/node) and is allocation- and
GC-bound: every `Cell` allocates two `set`s (external subscribers +
auto-discovered parents) and every `Slot` allocates a subscriber `set` plus a
closure, so a 10M-node workbook constructs ~30M+ Python objects. This is the
inherent cost of one interpreter object per reactive node — the compiled
bindings (Rust slotmap arrays, Go structs) build the same graph far cheaper. The
*recompute* paths (`cold_full_recalc` ~0.4–0.6 µs/formula) are much closer to
the compiled bindings because they run the interpreter loop, not the allocator.

### Viewport scaling — flat, unlike lazily-go

lazily-py's viewport recalc is **effectively size-independent** (72.9 µs at 2M
nodes, 74.8 µs at 10M nodes). The value cache is a plain Python `dict` keyed by
node identity, so both the ~1,000 viewport cache-hit lookups and the ~2 actual
recomputes are O(1) hash operations that don't scale with total sheet size. This
matches lazily-rs's flat curve and avoids the mild per-lookup growth lazily-go
reports from Go-map cache/TLB pressure on a multi-GB map. At 10M cells a
one-cell edit + 1,000-cell viewport read is ~38,500× cheaper than a full cold
recalc and never touches off-viewport formulas.

### Threading

These are single-threaded benchmarks. The concurrency surfaces
(`ThreadSafeContext`, signaling, the CRDT plane) are correctness-tested rather
than benchmarked here.

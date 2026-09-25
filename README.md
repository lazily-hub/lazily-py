# lazily

Lazy reactive primitives for Python — the **Cell kernel** (`Source` / `Computed`
cells, plus `Effect`) with automatic dependency tracking and cache invalidation,
plus the language-agnostic `lazily-spec` wire protocol for mirroring graph state
across processes and languages.

[![PyPI](https://img.shields.io/pypi/v/lazily.svg)](https://pypi.org/project/lazily/)

## Overview

`lazily` is the Python port of the **Cell kernel** (`#lzcellkernel`): two value
kinds — `Source` and `Computed` — plus the value-less `Effect` sink. `Cell` is
the value-node concept; `Source` is its native writable handle.

- **`Source`** — a value written from *outside* (`set` / `merge`); the writable
kind. Construct with `source` / `cell` (native class `Source`, slot
`SourceSlot`; `Cell` / `CellSlot` remain identity-preserving migration aliases).
A [`MergeCell`](#merge-algebra) is a `Source`
whose write folds under a non-`KeepLatest` policy (`Cell ≡ Source(KeepLatest)`).
- **`Computed`** — a value computed from *upstream*, via a compute function.
  Construct with `computed(ctx, f)`. **Guarded by default** and **lazy by
  default**.
- **`Effect`** — a value-less sink outside the hierarchy; nothing can depend on
  it.

A `Computed` is **lazy by default**: dependents are marked dirty on invalidation
but only recompute when accessed. When you need eager push-style semantics —
recompute immediately, observe `v1 → v2` with no unset window — make it
**eager**: `computed(ctx, f).eager()`. Going eager attaches a scheduled puller
`Effect` over the backing memo (eagerness is graph state — an `_eager` bit plus a
side table — not a distinct type), so N writes inside one `batch` re-materialize
the computed **once**, at the flush. **Every cell is guarded** — an equal
recompute is suppressed by the `PartialEq` guard (matching TC39
`Signal.Computed`), so unchanged values never cascade downstream work. There is
**no unguarded derived mode**: `computed` *is* the guarded derived constructor,
and the former separate `memo` construction is retired.

> **Migration note (v2 Cell kernel, `#lzcellkernel`).** The v1 value vocabulary
> is **removed**: `Signal` / `signal` / `signal_def`, `formula` / `formula_def` /
> `FormulaCell`, `SourceCell` / `SourceCellSlot`, and the `.drive()` / `.undrive()`
> / `is_driven` / `is_active` methods are gone. Use the v2 spelling instead — an
> eager `Computed` is `computed(ctx, f).eager()`; the lifecycle is
> `.eager()` / `.lazy()` / `.is_eager()`. The construction sugar `cell` /
> `cell_def` is **deprecated** in favour of `source` / `source_def`, and the
> derived-value `slot` decorator is **deprecated** in favour of the guarded
> `computed` (`slot_def` remains as the storage-sense factory). Python has no
> compile-time read/write split (see the design's §4): the split is a
> **convention** — a `Source` has `set` / `merge`, a `Computed` does not — not a
> runtime gate.

There is **no dedicated `Context` class** — a plain `dict` is the context, so the
Rust reference's `ctx.computed(f)` is spelled `computed(ctx, f)` here. `Slot` is
retained as the **storage position** that holds a node: a `Slot` uses itself as
the dictionary key that caches its value, so any dict works as the reactive
"world" (`lazily-spec` §5.0 "`Slot`-as-storage"). It is the Python analog of
`lazily-rs`'s surviving storage-sense `Slot`; construct it directly with
`Slot(callable=…)` when a raw storage node is genuinely needed.

## Feature coverage

Coverage by feature family across every binding, generated from
`coverage.json` in lazily-spec. Legend: ✅ shipped ·
`~` partial · `—` absent · `⊘` not applicable. The canonical matrix with per-cell
notes and platform carve-outs lives in
[`lazily-spec` § Cross-Language Coverage](https://github.com/lazily-hub/lazily-spec/blob/main/docs/coverage.md).

<!-- coverage-table:start -->
#### Summary — family × language

| Family | Rust | Python | Kotlin | JS | Dart | Zig | Go | C++ | C# | GDScript |
| --------- | :----: | :------: | :------: | :--: | :----: | :---: | :--: | :---: | :--: | :--------: |
| Reactive graph | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ~ |
| Materialization | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Family sync | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Statecharts | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Keyed collections | ✅ | ✅ | ✅ | ✅ | ✅ | ~ | ✅ | ✅ | ✅ | — |
| Reactive queue | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Broadcast topic | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Work queue | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| CRDT data types | ✅ | ~ | ~ | ~ | ~ | ~ | ~ | ~ | ✅ | — |
| Lossless tree | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Egress | ✅ | ~ | ~ | ~ | ~ | ~ | ~ | ~ | ~ | ~ |
| Ingress | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Wire codec | ✅ | ✅ | ✅ | ✅ | ~ | ✅ | ✅ | ✅ | ✅ | — |
| Transport & FFI | ✅ | ✅ | ✅ | ~ | ~ | ✅ | ✅ | ~ | ✅ | — |
| Message passing | ✅ | ✅ | ✅ | ✅ | ✅ | ~ | ✅ | ✅ | ✅ | — |
| Reliable sync | ~ | ~ | ~ | ~ | ~ | ~ | ~ | ~ | ~ | — |
| Durable owner | ✅ | — | — | — | — | — | — | — | — | — |
| Distributed plane | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Causal receipts | ~ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Security boundary | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Membership | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Coordination | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Presence | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Temporal | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Rate shaping | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Windowing | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Resilience | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Portable stdlib | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Service plane | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Instrumentation | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |

**Roll-up rule:** a family cell is `✅` only when *every required* row in that family is `✅`; `~` when the family is mixed (some shipped or partial); `—` when no required row is shipped or partial; `⊘` only when every required row in the family is not applicable. Rows the spec marks **MAY** (`optional`) are excluded from the roll-up — declining an optional feature is not a gap.

A family cell summarises 77 feature rows. For row-level marks, per-cell notes, and platform carve-outs see [the canonical coverage matrix in `lazily-spec`](https://github.com/lazily-hub/lazily-spec/blob/main/docs/coverage.md).
<!-- coverage-table:end -->

## Installation

```
pip install lazily
```

The library itself has no third-party runtime dependencies. The optional extras
only pull in the struct libraries the
[struct bridge](#struct-bridge--struct_source) can adapt — the stdlib backends
(`dataclasses`, `NamedTuple`) need no extra at all:

```
pip install "lazily[msgspec]"      # or [pydantic], [attrs], [structs] for all three
pip install "lazily[prometheus]"  # the prometheus_client egress adapter
```

## Example usage

```python
from lazily import SourceSlot, source, slot

# Sources hold a value that can be updated.
name = SourceSlot[dict, dict, str]()


# Slots are functions that depend on sources and other slots.
@slot
def greeting(ctx: dict) -> str:
    print("Calculating greeting...")
    return f"Hello, {name(ctx).value}!"


# A SourceSlot can also have a default value.
@source
def response(ctx: dict) -> str:
    return "How are you?"


@slot
def greeting_and_response(ctx: dict) -> str:
    print("Calculating greeting_and_response...")
    return f"{greeting(ctx)} {response(ctx).value}"


ctx = {}

name(ctx).value = "World"

# First access: runs the function
print(greeting(ctx))
# Calculating greeting...
# 'Hello, World!'

# Second access: uses cache (no print)
print(greeting(ctx))
# 'Hello, World!'

# Dependencies also access cached values
print(greeting_and_response(ctx))
# Calculating greeting_and_response...
# 'Hello, World! How are you?'

# Dependencies also cached
print(greeting_and_response(ctx))
# 'Hello, World! How are you?'

# Update cell: invalidates cache
name(ctx).value = "Lazily"

# Access again: re-runs the function
print(greeting_and_response(ctx))
# Calculating greeting_and_response...
# Calculating greeting...
# 'Hello, Lazily! How are you?'

# Another access: uses cache
print(greeting_and_response(ctx))
# 'Hello, Lazily! How are you?'
```

## Core Concepts

### Context

A plain `dict` is the context. It owns all cached Slot values; Slots store their
cache under themselves as keys. The current implementation is single-threaded —
create one dict per reactive graph.

### Slot

A Slot wraps a compute function `(ctx) -> T`; the result is cached after first
access. Dependencies are discovered automatically via a global `slot_stack` —
any Slot or Cell read during computation becomes a dependency and re-subscribes
on every recompute, so conditional branches update the dependency graph with no
manual cleanup. When a dependency invalidates, the Slot only marks its cache
dirty; it does **not** recompute until called again.

| Type | Purpose |
|------|---------|
| `BaseSlot[C_in, C_ctx, T]` | Base slot without subscriber support |
| `Slot[C_in, C_ctx, T]` | Storage-sense slot with dependency tracking and invalidation |
| `slot` | **Deprecated** — use `computed` (guarded) or `Slot(callable=…)` (storage) |
| `slot_def(resolve_ctx)` | Storage-sense decorator factory for a custom context resolver |

### Source cell

A `Source` cell holds a mutable value. Reading `cell.value`
inside a `Computed` or `Effect` auto-subscribes that reader; assigning
`cell.value = x` (or `cell.set(x)`) compares old and new via `!=` and, only if
changed, cascades invalidation to dependents. Construct with `source` (the v1
`cell` name is deprecated).

| Type | Purpose |
|------|---------|
| `Source[T]` (`Cell[T]` migration alias) | Mutable source value with subscription support |
| `SourceSlot[C_in, C_ctx, T]` (`CellSlot` migration alias) | Slot that returns a `Source` cell |
| `source` | Decorator: `SourceSlot` with an identity resolver (canonical) |
| `source_def(resolve_ctx)` | Decorator factory for a custom context resolver |
| `cell` / `cell_def` | **Deprecated** v1 aliases of `source` / `source_def` |

### Eager Computed

An **eager** `Computed` — `computed(ctx, f).eager()` — is the counterpart to a
lazy `Computed`. Where a lazy cell marks itself dirty on invalidation and
recomputes on the next read, an eager one recomputes *the instant a dependency is
invalidated*, before the mutating call returns. The value is always materialized,
so observers never see an intermediate unset value.

```python
from lazily import SourceSlot, computed

n = SourceSlot[dict, dict, int]()

ctx: dict = {}
n(ctx).value = 1

doubled = computed(ctx, lambda c: n(c).value * 2).eager()  # eager: materialized now
print(doubled.value)   # 2

n(ctx).value = 5       # doubled recomputes immediately
print(doubled.value)   # 10 — already current, no lazy read needed
```

An eager `Computed` is **composed from existing primitives**, not a parallel
engine: a memoized backing `Slot` supplies glitch-free, guarded recomputation,
and a small puller `Effect` re-materializes it after every invalidation to supply
the eagerness. It inherits the guard (an equal recompute suppresses the
downstream cascade). `.lazy()` (or `.dispose()`) removes the eager puller — the
value stays readable but reverts to lazy (recompute-on-read) behavior.

| Method | Purpose |
|------|---------|
| `computed(ctx, f)` | Lazy, guarded derived value bound to a context |
| `computed(ctx, f).eager()` | The eager form (idempotent; returns the same handle) |
| `.lazy()` / `.is_eager()` | Revert to lazy / query eagerness |
| `computed_def(resolve_ctx)` | Decorator factory for a context-cached lazy `Computed` |

### StateMachine

`StateMachine[S, E]` is a finite state machine backed by a reactive `Cell`, so
its `state` participates in dependency tracking like any other reactive value.
Construct it with `StateMachine(ctx, initial, transition)` where `transition` is
a pure `(state, event) -> next_state | None` (returning `None` rejects the
event). `send(event)` returns whether the transition was accepted; a
self-transition to an equal state is accepted but suppressed by the Cell's
`PartialEq` guard.

### Reactive collections, async, and thread-safe contexts

lazily-py also implements the `lazily-spec` compute-layer `MUST`s, each ported
from its Lean formal model in [`lazily-formal`](https://github.com/lazily-hub/lazily-formal):

- **`ReactiveMap` / `SourceMap` / `ComputedMap` / `SourceTree`** — keyed reactive
  collections (`#reactivemap`) with independent value/membership/order signals and
  atomic move. One generic `ReactiveMap` over a handle kind; `SourceMap` (input
  cells, adds `set` + eager `entry`) and `ComputedMap` (derived slots, lazy
  `get_or_insert_with` + eager `materialize_all`) are its specializations.
- **`QueueCell`** — a reactive FIFO queue (SPSC primitive with an MPSC-via-`batch`
  usage rule) with a pluggable `QueueStorage` backend. Reader-kind invalidation
  (head/len/is_empty/is_full/closed), bounded reactive backpressure via `is_full`,
  and the closure lifecycle (drain / Closed-distinct-from-Empty / idempotent).
- **`LatestDurableProjectionCore` / `LatestDurableProjection`** — keyed durable
  egress that converges each sink key to its latest desired epoch, permits one
  in-flight attempt per key, and generation-fences stale acknowledgements.
- **`reconcile_ops`** — move-minimized keyed reconciliation (LIS kernel).
- **`AsyncSlot` / `AsyncEffect`** — the async slot lifecycle with stale-completion
  discard, and cleanup-before-body effect scheduling.
- **`ThreadSafeContext`** — a lock-serialized `batch` boundary that coalesces
  writes into one invalidation pass.
- **`lazily.ffi`** — the C-ABI FFI boundary (`LazilyFfiStatus`,
  `LazilyFfiMessageKind` incl. `CrdtSync = 3`, `LazilyFfiBytes`).

The test suite gates on `lazily-formal`'s `lake build` (every theorem checks)
and mirrors the named Lean theorems as property tests. See `SPEC.md` for the
full compliance surface.

## Struct bridge — `struct_source`

`struct_source` explodes one struct instance into **per-field** `Source` cells
plus a single guarded `Computed` that re-materializes the struct. A reader that
depends on one field is invalidated only when *that* field changes; a reader of
the whole struct keeps depending on all of them and pays one construction per
settled wave. It is the swap-in path for code that already holds a `*State`
struct and hands out a fresh copy from a `status()` method.

```python
from dataclasses import dataclass

from lazily import computed, struct_source


@dataclass
class Status:
    queue_depth: int
    last_error: str | None = None


ctx: dict = {}
status = struct_source(ctx, Status(queue_depth=0))

# Depends on ONE field.
depth_view = computed(ctx, lambda c: c.read(status["queue_depth"]) * 2).eager()
# Depends on the whole struct.
whole = computed(ctx, lambda c: c.read(status.struct)).eager()

status.update(last_error="boom")   # depth_view is NOT invalidated
status.update(queue_depth=3)       # both recompute
status.update(queue_depth=3)       # guarded: equal write, nothing recomputes

# Ingress for code that already produces a fresh instance: only the fields that
# actually moved invalidate, and the whole write is one batch.
status.apply(Status(queue_depth=4, last_error="boom"))

status.value        # Status(queue_depth=4, last_error='boom')
```

The bridge is library-agnostic. Built-in backends, in resolution order: msgspec
`Struct`, pydantic v2 `BaseModel`, `attrs`, stdlib `dataclasses`, stdlib
`NamedTuple`. None of them is imported at module scope, and none is imported to
*decide* whether a type belongs to it — the probes are structural (an MRO entry
from that module, or the marker attribute the library stamps on the class), so
an application that never imports `msgspec` never loads it because of lazily.
Register another library with `register_struct_backend(backend)`; it takes
precedence over the built-ins, and re-registering a built-in's `name` replaces
it rather than shadowing it.

Non-`init` fields are not bridged: they are recomputed by the constructor, so
they are not independent state. pydantic re-materializes through
`model_construct` (values are already validated, and re-validating per wave is
the cost the bridge exists to remove); pass
`PydanticBackend(validate=True)` to `struct_source(..., backend=...)` to
validate on every re-materialization instead.

## Durable execution — `lazily.workflow`

> **`lazily.temporal` is not temporal.io.** It is lazily's *time-operator*
> family (`TimerCell`, `IntervalCell`, `CronCell`, `DeadlineCell`) — "temporal"
> in the tense / logical-clock sense, and older than the engine of the same
> name. The temporal.io integration is **`lazily.workflow`**. The name is kept
> because `lazily.temporal` is public API in nine bindings and renaming it
> across all of them would be a breaking change bought only for this note.

A durable engine replays workflow code from its event log and expects the same
decisions in the same order. A reactive graph fits that well — it is already a
pure function of its sources — until something in it reads the wall clock, a
random number, or a UUID. Then replay diverges silently and surfaces much later
as a workflow that will not complete.

`lazily.workflow` is the boundary that makes that loud. **It does not depend on
`temporalio`**: the engine is injected as a clock accessor plus a two-method
scheduler, so the same code runs against temporal.io, a test double, or any
other durable engine.

```python
from lazily import TimerCore, deterministic_scope, workflow_context

# inside a temporalio workflow
wf = workflow_context(ctx, workflow.now, scheduler=TemporalScheduler())
timer = wf.register(TimerCore(fire_at=wf.clock.tick() + 5_000))

with deterministic_scope():
    while not timer.fired():
        await wf.schedule_next()   # a durable engine timer, not a sleep
        wf.advance()               # one clock reading drives every source
```

- **One reading per advance.** `advance()` reads the engine clock once and ticks
  every registered source, so two sources can never disagree about what time it
  is — the shape that makes a replay diverge from the original run.
- **The clock refuses to go backwards.** `ManualClock` clamps a backwards move,
  which is right for a game loop and wrong here: a replayed clock cannot regress,
  so a backwards reading means something is feeding it wall time. It raises.
- **`activity()` is the only sanctioned side effect.** A reactive effect inside a
  workflow must not perform I/O itself — the effect reruns on replay and the I/O
  would rerun with it.
- **`deterministic_scope()`** rebinds `time.time` / `monotonic` / `perf_counter`
  (and the `_ns` forms), the module-level `random` functions, `os.urandom`, and
  `uuid.uuid1` / `uuid4` to raise `NonDeterminismError`, restoring all of them on
  exit including on an exception. It is a guard, **not a sandbox**: it cannot
  intercept `datetime.datetime.now()` (a C-type method), a name bound before the
  scope opened, or anything a C extension does internally. Module re-import
  isolation is the layer for those, which is what temporal.io's own workflow
  sandbox does.

## Replay-equivalence proof — `lazily.replay`

`lazily.workflow` makes the *reachable* non-determinism raise. This is the other
half of the same gate: it makes replay equivalence **provable**. Nobody should
put a reactive graph inside a workflow without that proof, and this is the proof.

The discipline is borrowed from `tsift`, whose cached excerpts are trustworthy
because every one records a body hash and *revalidates it against the source
bytes* before the excerpt is returned — a stale body deterministically
suppresses the cached answer rather than returning a plausible-looking one. Here
the event log is the source bytes.

```python
from lazily import LatestDurableProjectionCore, ReplayHarness, ReplayLog


class Projection:
    def __init__(self) -> None:
        self.core = LatestDurableProjectionCore(generation=1)

    def apply(self, event) -> None:              # exactly one event
        getattr(self.core, event.name)(*event.payload)

    def observe(self) -> dict[str, object]:      # the cells under proof
        return {"snapshot": self.core.snapshot()}


log = ReplayLog.from_records([
    ("upsert_desired", ("a", 1, 10)),
    ("claim", ("a", 1)),
    ("ack_applied", ("a", 1, 1)),
])
harness = ReplayHarness(Projection, deterministic=True)

fingerprint = harness.record(log)     # pin it, or commit `fingerprint.to_wire()`
harness.verify(log, fingerprint)      # raises unless the replay is identical
harness.prove(log)                    # record + re-replay, no fingerprint needed
```

- **The fingerprint is bound to the log that produced it.** `verify` checks
  `log_digest` *before* comparing any value. A fingerprint recorded against a
  different log raises `ReplayLogMismatchError` and is never compared — so it can
  neither pass by coincidence (two logs, same final state) nor be misreported as
  a graph defect. `check()`, which is non-raising for value divergence, still
  raises here: a stale fingerprint is an unanswerable question, not a report.
- **Divergence is located, not just detected.** Every event is a checkpoint
  (`stride=N` to sample sparsely), so the error names the **first** event where
  the values parted and the exact cell label. A fingerprint covering only the
  final state tells you the graph is wrong but not where.
- **`prove(log)` needs no recorded fingerprint.** It records, replays again, and
  compares: a graph that is not a pure function of its log already disagrees with
  itself. `build` is called once per replay, so a harness cannot accidentally
  prove a graph against its own leftover state.
- **The encoding is canonical or it raises.** `canonical_bytes` is type-tagged
  and length-framed: dict insertion order and set iteration order are not part of
  a value, but `1`, `"1"`, `1.0` and `True` are all distinct, and so are
  `["a", "bc"]` and `["ab", "c"]`. A value with no defined encoding raises
  `ReplayEncodingError` rather than degrading to `repr`, which embeds object
  addresses and would report a false divergence on every run.
- **`deterministic=True`** wraps each replay in `deterministic_scope()`, so a
  wall-clock read raises at the line that reached it instead of appearing as a
  divergence one checkpoint later.
- **The machinery was already there.**
  `reliable_sync`'s `DurableOutbox.replay_from()` *is* a replay source;
  `replay_log_from_outbox(outbox, cursor=0)` makes it a fingerprinted one, using
  outbox epochs as event seqs so an ack-truncated prefix shows up in the log
  digest instead of silently shifting every event.
  `latest_durable_projection` is a deterministic state machine over such a log.
  What was missing was the stated contract: **given the same `ReplayLog`, a
  rebuilt graph observes the same values at every checkpoint — any deviation is a
  defect, not a tolerance.**

Hashing is BLAKE2b-256 from `hashlib`, not BLAKE3: lazily-py has no runtime
dependencies. Digests are not wire-compatible with tsift's and are not meant to
be.

## Projected state chart — `lazily.projected_chart`

`lazily.statechart` owns its transitions: you send it an event and it decides
where to go. That is the wrong shape when the state lives somewhere else. A
workflow run's `status` column is owned by Postgres and advanced by Temporal —
the database is the authority and the graph is strictly downstream of it.
Sending such a chart an event would fork the model from the row.

So this chart never decides anything. You feed it observed states and it answers
the two questions the authority cannot.

```python
from lazily import ProjectedChart, ProjectedChartDef

defn = ProjectedChartDef.of(
    initial="queued",
    transitions={
        "queued": ["started", "failed"],
        "started": ["completed", "failed"],
        "completed": [],
        "failed": ["started"],            # a retry is legal
    },
    deadlines={"queued": 30_000, "started": 300_000},
    terminal=["completed"],
)
chart = ProjectedChart(ctx, defn)

chart.observe("started", at=row.updated_at_ms)   # adopt what the DB says
chart.tick(now_ms)                               # elapse the deadline

if chart.wedged():                               # a derived cell
    alert(chart.state(), chart.overdue_by())
```

- **Was that transition legal?** The authority still wins — an observation is
  *always* adopted — but a step the declared lifecycle does not allow is recorded
  as an `IllegalTransition` and counted on a reactive cell. Refusing the row
  would make this side silently disagree with the database, which is worse than
  not modelling it at all; adopting it quietly would tell you nothing. It adopts
  and says so. An undeclared state is the same story, and cannot wedge — no
  deadline is knowable for a state the chart has never heard of, so the violation
  is the signal rather than a fabricated deadline.
- **Is it wedged?** In a state past that state's deadline with no advance.
  `wedged` is a derived cell, so an effect, alarm, or health cell reading it is
  invalidated exactly on the edge — the declarative form of the hand-written "is
  it still `started`, and has it been too long" branch that otherwise accretes in
  a service. Terminal states and states with no deadline never wedge: a finished
  run is not late.
- **A repeated observation does not reset the deadline.** Re-observing the state
  the chart is already in advances the authority timestamp but *not*
  `entered_at`. A poller that keeps reading the same `started` row is evidence
  the run is wedged, not evidence it just advanced — resetting there would make
  the wedge unreachable, which is the bug this module exists to express.
- **Two clocks, one time base.** `at` is the *authority's* timestamp for the row
  and staleness is last-writer-wins on it, so an out-of-order delivery cannot
  walk the state backwards. `tick(now)` is the *observer's* clock and is what
  makes a deadline elapse; it is strictly monotone and raises on a backwards
  reading, because a deadline computed from a regressing clock is not a deadline.
  Both must be the same time base (epoch milliseconds).
- **It is a `TimelineSource`.** `tick` / `next_fire` match the protocol, so the
  wedge composes with the rest of the time-operator family: register it with a
  `WorkflowContext` and the wedge check becomes a durable engine timer instead of
  a polling loop.
- **It is replayable.** Both clock inputs are arguments, so `ProjectedChartCore`
  is a pure function of its observation log — the suite proves one under
  `ReplayHarness` (see `lazily.replay`), wedge and violations included.

`ProjectedChartCore` is the graph-agnostic projection (no cells, no clock of its
own); `ProjectedChart` is the reactive shell. Sync-only — unlike the spec
families there are no thread-safe or async flavours, because the subject here is
one external row per chart.

## Named keyed fold — `lazily.keyed_fold`

N independent writers, one key each. A writer sets, folds and **clears only its
own key**; the summary is a guarded `Computed` over the live set. This is the
general shape `service.HealthCell` implements for booleans and `merge.MergeCell`
has the algebra for but no keyed surface.

The bug it exists to prevent is the last-writer-wins one: several components
sharing a single cell for "the current errors", where whoever writes last erases
everyone else's entry and whoever clears erases entries that were never theirs.

```python
from lazily import KeyedFold

ctx: dict = {}
errors = KeyedFold[str, str, dict[str, str]](ctx)

intake = errors.claim("intake")          # exclusive: a second claim raises
dispatcher = errors.claim("dispatcher")

intake.set("connection refused")
dispatcher.set("timeout")
intake.clear()                           # dispatcher's entry survives

errors.value                             # {'dispatcher': 'timeout'}
```

A reader of one key is never invalidated by a sibling's write, and a reader of
the summary is invalidated only when the summary actually changes — so a
`summarize` that counts entries is untouched when a *value* moves:

```python
fold = KeyedFold[str, int, int](ctx, lambda entries: len(entries))
```

`policy=` selects the merge algebra `writer.merge(op)` folds under (default
`KeepLatest`, i.e. replace); a first write seeds the entry with the operand,
since a policy is an associative merge and has no identity to start from.
`writer.set` bypasses the policy. `eager=False` defers the fold to the first
read, at the cost of the summary guard.

## Metrics — `lazily.metrics` and `lazily.prometheus_egress`

A metric here is a reactive node, not a mirror of one. A family's child is
either **writable** (`CounterCell` / `GaugeCell` over a `Source`) or **derived**
— bound with `derive`, its value read straight off the graph. A derived child is
the point: the scrape reads the graph, so there is no push step, no
hand-maintained mirror, and no window in which the exported number disagrees
with the state it describes.

```python
from lazily import MetricsRegistry, source

ctx: dict = {}
metrics = MetricsRegistry(ctx)
connected = source(lambda c: False)

up = metrics.gauge("service_up", "1 when the component is connected.", ("component",))
up.derive(("intake",), lambda c: 1.0 if connected(c).value else 0.0)

accepted = metrics.counter("accepted_total", "Accepted messages.", ("source",))
accepted.labels("http").inc(3)

connected(ctx).value = True
# No mirror step ran. The gauge IS the graph read.
print(metrics.render_text())
# HELP service_up 1 when the component is connected.
# TYPE service_up gauge
# service_up{component="intake"} 1.0
# ...
```

`render_text()` emits the Prometheus text exposition format with **no
third-party dependency**. For a real exporter, `lazily.prometheus_egress`
registers the whole registry as a `prometheus_client` custom collector — a pull,
for the same reason:

```python
from prometheus_client import CollectorRegistry, generate_latest

from lazily import register_metrics, unregister_metrics

prom = CollectorRegistry()
collector = register_metrics(metrics, prom)
generate_latest(prom)        # resolves the graph on this scrape
unregister_metrics(collector, prom)
```

`prometheus_client` is optional (`pip install "lazily[prometheus]"`) and
imported lazily inside the calls that need it, so `import lazily` still loads no
third-party package.

A counter is monotonic in both directions of use: `inc` rejects a negative
delta and `set` refuses to move backwards, so mirroring an externally-owned
absolute total can never publish a decrease a scraper would read as a counter
reset. `derive` is **lazy** by default — the expression runs on collection, not
on every upstream change — which is the right default for a scrape; pass
`eager=True` when a reactive consumer of `observe()` should be shielded by the
`Computed` guard from upstream changes that do not move the number.

## Latest-durable projection — `lazily.latest_durable_projection`

Use `LatestDurableProjection` when the durable side effect is a projection of
current state (for example, saving the latest document image), rather than a
command log where every intermediate value must execute. `upsert_desired` keeps
only the newest pending epoch per key; `claim` allows at most one sink attempt
per key; `ack_applied` and `fail_retryable` must match that attempt's generation
and epoch. `reconnect` advances the sink generation and safely requeues claimed
work, so a stale actor can never clear newer intent.

```python
from lazily import LatestDurableProjection

projection = LatestDurableProjection[str, str]({}, generation=1)
projection.upsert_desired("document", epoch=41, value="latest text")
attempt = projection.claim("document", generation=1).envelope
assert attempt is not None

# Await the external write in the caller's driver, then acknowledge its exact
# token. A concurrent epoch 42 remains pending even when epoch 41 succeeds.
projection.ack_applied(attempt.key, attempt.generation, attempt.epoch)
```

The graph-agnostic `LatestDurableProjectionCore` and the reactive
`LatestDurableProjection`, `ThreadSafeLatestDurableProjection`, and
`AsyncLatestDurableProjection` shells implement the same contract. The async
shell's transitions are intentionally synchronous; only the external sink
driver awaits I/O. All three shells replay
`conformance/egress/latest_durable_projection.json` from `lazily-spec` v0.38.0
and correspond to the corrected `lazily-formal` v0.38.1 model.

## Reactive queue — `lazily.queue`

`QueueCell` is a FIFO collection composed of reactive cells — **not a new cell
kind** — that adds queue semantics (push to tail, pop from head) to the reactive
graph. `ThreadSafeQueueCell` and `AsyncQueueCell` carry the same Core surface;
the topic and work-queue families likewise ship `ThreadSafe*` and `Async*`
shells. Nothing in the async family is async-coloured: storage reads, cursor
advances, and lease decisions return plain values.

The reactive shell wraps a pluggable `QueueStorage` backend (default
`VecDequeStorage`) and owns demand-driven reader-kind handles. It invalidates
exactly the handles whose values changed — a push to a non-empty queue does NOT
invalidate the `head` reader, while a pop does. Thread-safe shells serialize
their whole public operation through a `ThreadSafeContext`; the same canonical
fixture corpus and invalidation matrices replay against all three flavors.

```python
from lazily import QueueCell, QueuePopError, batch

ctx = {}
q: QueueCell[str] = QueueCell(ctx)

q.try_push("a")
q.try_push("b")
assert q.head() == "a"
assert q.len() == 2
assert q.try_pop() == "a"

# Bounded queue → reactive backpressure via is_full.
bq = QueueCell[int].with_capacity(ctx, 2)
bq.try_push(1)
bq.try_push(2)
assert bq.is_full()
assert bq.try_push(3).label == "Full"   # reject at capacity
assert bq.try_pop() == 1
assert not bq.is_full()                  # pop freed a slot → is_full reader invalidated

# MPSC: multiple producers push inside one batch → one invalidation pass.
batch(lambda: (q.try_push("p1"), q.try_push("p2")))

# Closure: pop on closed+empty returns Closed (distinct from Empty).
q.close()
assert q.is_closed()
assert q.try_push("x").label == "Closed"
```

### Competing-consumer work queue

`WorkQueueCell` provides exclusive FIFO claims with visibility deadlines,
worker-scoped acknowledgements, tail retries, and bounded dead-letter handling.
Item ids survive retries while each claim gets a fresh delivery id.

```python
from lazily import WorkQueueCell

work = WorkQueueCell[str](ctx, visibility_timeout=10, max_deliveries=3)
work.push("job")
delivery = work.claim("worker-a", 100)
assert delivery is not None
assert work.ack("worker-a", delivery.delivery_id)
```

The reader-kind independence law is explicit: every successful operation
derives its changed-reader set from the before/after state and resets those
memoized handles in one batch. Unchanged reader kinds remain warm.

## Transport-agnostic reactive ingress — `lazily.ingress`

A client consuming a remote stream usually grows four accidental mechanisms: a
`refresh()` loop that re-reads whether the connection is healthy, a hand-rolled
"is this message still relevant?" check, a reconnect path that forgets what was
already applied, and transport-shaped consumer code that disagrees with itself
per transport. Every one of those is a *derive* being simulated with a call.
`IngressCell` makes them derives, and makes the transport a value the primitive
never touches.

An envelope carries its own provenance (`generation` / `sequence` /
`stamped_at`), so a WebSocket frame, an RPC response, and a polled page are the
same input once decoded. Admission applies a normative order — lifecycle →
generation fence → freshness → generation handoff → dedupe → ordering →
backpressure → merge — and each keyed scope exposes four independent reader kinds
plus three receipt channels.

```python
from lazily import IngressCell, IngressEnvelope, IngressPolicy, Sum

ctx: dict = {}
ingress = IngressCell[str, int](ctx, IngressPolicy(reorder_window=4), Sum)

ingress.admit(IngressEnvelope("alpha", 1, 0, 0, 5))
assert ingress.value("alpha") == 5
assert ingress.readiness("alpha") == "ready"     # a derive, not a poll

# Out of order: buffered, so nothing a reader can observe moved.
ingress.admit(IngressEnvelope("alpha", 1, 2, 0, 4))
assert ingress.value_is_valid("alpha")            # the value reader stays warm

# The delivery that closes the gap flushes the run as ONE coalesced window.
ingress.admit(IngressEnvelope("alpha", 1, 1, 0, 2))
assert ingress.value("alpha") == 11
assert ingress.drain("alpha") == 11               # an egress, never an ack
assert ingress.suspend("alpha").from_sequence == 3
```

`ThreadSafeIngressCell` and `AsyncIngressCell` are the other two flavors of the
same contract; all three replay the canonical
`lazily-spec/conformance/ingress/*.json` corpus. Nothing in the family is
async-coloured — an admission decision is a function of the fence, the watermark,
the reorder buffer, and the observed clock, so there is nothing to await.
Awaiting belongs to the transport, and the transport is outside the primitive by
construction.

## IPC — the `lazily-spec` wire protocol

`lazily.ipc` implements the language-agnostic [`lazily-spec`](https://github.com/lazily-hub/lazily-spec)
wire protocol, so a Python graph's state can be mirrored to remote observers
across processes and languages. The JSON encoding is **byte-compatible** with the
Rust (`lazily-rs`), Zig (`lazily-zig`), and TypeScript (`@lazily/signaling`)
bindings, and is validated against the canonical `lazily-spec/conformance`
fixtures (vendored under `tests/conformance/`).

Two message kinds flow over any transport (WebSocket text, WebRTC data, FFI
buffer):

- **`Snapshot`** — the full graph state at an epoch (`nodes`, `edges`, `roots`).
- **`Delta`** — an ordered batch of the 7 `DeltaOp` variants (`CellSet`,
  `SlotValue`, `Invalidate`, `NodeAdd`, `NodeRemove`, `EdgeAdd`, `EdgeRemove`)
  applied with epoch sequencing and fail-closed resync.

```python
from lazily import (
    Snapshot, NodeSnapshot, EdgeSnapshot, ShmBlobRef,
    Delta, DeltaOp, IpcMessage,
)

# Build and serialize a snapshot — encode_json() returns transport-agnostic bytes.
snap = Snapshot(
    epoch=7,
    nodes=[
        NodeSnapshot.payload(1, "i32", bytes([1, 2, 3])),
        NodeSnapshot.opaque(2, "opaque-type"),
        NodeSnapshot.shared_blob(3, "text/plain", ShmBlobRef(0, 16, 1, 7, 999)),
    ],
    edges=[EdgeSnapshot(2, 1), EdgeSnapshot(3, 1)],
    roots=[1, 2],
)
wire = IpcMessage.of_snapshot(snap).encode_json()
assert IpcMessage.decode_json(wire).snapshot == snap

# An incremental delta carrying mutations.
delta = Delta.next(40, [
    DeltaOp.cell_set(1, bytes([10])),
    DeltaOp.invalidate(3),
])
IpcMessage.of_delta(delta).encode_json()

# The same frames over `msgpack`, the cross-language binary default: an
# externally tagged envelope over MessagePack maps keyed by the *json* field
# names. Map key order is encoder-defined, so conformance is a semantic round
# trip — `decode(encode(m)) == m` — never a golden byte string.
packed = IpcMessage.of_snapshot(snap).encode_msgpack()
assert IpcMessage.decode_msgpack(packed).snapshot == snap
```

A `PeerPermissions` boundary gates what is shared: it is **default-deny**, so only
nodes a peer is explicitly allowed to read are serialized into a snapshot or
delta — non-allowlisted nodes are omitted entirely.

### Shared-memory blobs — `ShmBlobArena`

`ShmBlobArena` lets a Python process **host** blob payloads (not just carry
`ShmBlobRef` descriptors). It is a `bytearray`-backed, append-only arena with a
40-byte header (`LZSH` magic + FNV-1a-64 checksum) and wraparound, ported from
the `lazily-rs` `ShmBlobArena<B>` and byte-compatible with the Rust and Zig
arenas. The module exports `ShmBlobArena`, `ShmBlobArenaError` (with its variant
subclasses), and `SHM_BLOB_HEADER_LEN`.

## Lossless tree CRDT — `lazily.lossless_tree_crdt`

`LosslessTreeCrdt` (#lzlosstree) is a single rooted concrete-syntax tree whose
**leaves own every rendered byte** — `render(tree) == source_text` for valid,
invalid, and unknown source alike. Where `TextCrdt` is a flat lossless floor,
this is the structured tree that can itself be the wire authority. Element nodes
own structure only; all text lives in leaf nodes tagged `Token` / `Trivia` /
`Raw` / `Error`, so unknown/invalid spans round-trip exactly as `Raw`/`Error`
leaves rather than being discarded.

```python
from lazily import LeafKind, LosslessTreeCrdt, SeedElement, SeedLeaf
from lazily.lossless_tree_crdt import ROOT

tree = LosslessTreeCrdt(peer=1)
heading = tree.create_node(ROOT, None, SeedElement("heading"))
tree.create_node(heading, None, SeedLeaf(LeafKind.TOKEN, "# "))
title = tree.create_node(heading, None, SeedLeaf(LeafKind.RAW, "Título"))
assert tree.render() == "# Título"

# Op-based delta sync: fork, diverge, converge through a dotted frontier.
other = tree.fork(peer=2)
other.edit_leaf(title, 0, 0, "X")
tree.apply_update(other.diff(tree.frontier()))
assert tree.render() == other.render()
```

Leaf text embeds `TextCrdt` wholesale; child order is a fractional index
(`key_between`); the clock is a Lamport `TreeOpId`. Anti-entropy is op-based over
a **dotted, non-contiguous version frontier** (`TreeVersionFrontier`) — a dot
*set* (contiguous prefix + sparse holes), never a per-peer max, so a missing
interior op stays representable and re-requestable. Leaf-local wire offsets are
UTF-8 bytes (`byte_to_char`). The wire codec (`tree_update_to_wire` /
`tree_update_from_wire`) validates against `lazily-spec`'s
`lossless-tree-delta.json`, and all nine `conformance/lossless-tree/` fixtures
replay.

## Command / RPC message plane — `lazily.command`

`command-plane-v1` is an **additive sibling** to `Snapshot` / `Delta` /
`CrdtSync`: four evented frames (`CommandSubmit` / `CommandCancel` /
`CommandEvents` / `CommandProjection`) that carry command traffic, not cell
state. lazily owns the envelope; the namespace owns the `IpcValue` payload, which
lazily never decodes.

The single hard rule: **terminal authority is the causal receipt.** A command is
terminal only when a terminal `CausalReceipt` for its `command_id` folds in
(`applied`, or `rejected` — including the `cancelled` / `superseded` /
`timed_out` reasons). `observed` / `accepted` / `started` events are progress
only; a transport ACK is never terminal.

```python
from lazily import (
    CommandPolicy, CommandRpcClient, CommandSubmit, DedupePolicy,
    applied_receipt,
)
from lazily.ipc import IpcValue

class Transport:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)

client = CommandRpcClient(Transport())
cmd_id = client.submit(CommandSubmit(
    command_id="cmd-1", causation_id="cmd-1", source="plugin",
    target="controller", namespace="agent-doc", name="editor_route",
    authority_generation=1, idempotency_key="doc:run", deadline_ms=0,
    policy=CommandPolicy(DedupePolicy.SAME_IDEMPOTENCY_KEY, False, True),
    payload_type="agent-doc.editor_route.v1", payload_hash="sha256:…",
    payload=IpcValue.of(b"{…}"),
    required_features=["command-plane-v1"],
))
# `call` resolves ONLY on a terminal receipt — never an ACK or `accepted`.
client.ingest_receipt(applied_receipt("rcpt-1", cmd_id, "controller", 1))
assert client.poll_call(cmd_id).kind.value == "resolved"
```

`CommandProjection` is the pure reducer (generation guards, idempotency,
cancel-before-terminal-only, terminal-conflict-fails-closed, reconnect
equivalence); `CommandRpcClient` is the derived RPC facade. The wire codec
validates against `lazily-spec`'s `message-passing.json`, and all eight
`conformance/message-passing/` fixtures replay.

## Benchmarks

Wall-clock benchmarks live in [`BENCHMARKS.md`](BENCHMARKS.md), covering both
the in-library micro-suite (reactive core, keyed reconciliation, `SourceMap`,
`TextCrdt`, CRDT plane) and a large spreadsheet-shaped **scale** suite that
mirrors the lazily-rs / lazily-go `scale` groups (`N` input cells + `N` formula
slots, `formula[i] = input[i] + input[i-1]`). The scale suite is measured up to
a full **10,000,000-cell Google Sheets workbook** (`N = 5,000,000`); a one-cell
edit plus a 1,000-cell viewport read stays in the ~75 µs range regardless of
sheet size, because the lazy pull model recomputes only the ~2 formulas that
read the edited input.

```bash
make bench          # micro-suite
make bench-scale    # scale suite (default N = 1,000,000)

# or directly, with a custom size:
uv run python -m lazily.benchmarks
LAZILY_SCALE_N=5000000 uv run python -m lazily.scale_bench   # 10M-cell workbook
```

See [`BENCHMARKS.md`](BENCHMARKS.md) for the full results, hardware, and honest
notes on CPython's per-node overhead.

## The lazily family

`lazily-py` is one binding in a cross-language reactive family — the same cell
kernel, the same keyed collections and CRDTs, and the same `lazily-spec` wire
protocol — so peers written in different languages talk to each other without a
translation layer.

| Repo | Language |
|---------|----------|
| [`lazily-rs`](https://github.com/lazily-hub/lazily-rs) | Rust — the reference implementation |
| **`lazily-py`** | Python — you are here |
| [`lazily-go`](https://github.com/lazily-hub/lazily-go) | Go |
| [`lazily-kt`](https://github.com/lazily-hub/lazily-kt) | Kotlin / JVM |
| [`lazily-js`](https://github.com/lazily-hub/lazily-js) | JavaScript / TypeScript |
| [`lazily-cs`](https://github.com/lazily-hub/lazily-cs) | C# / .NET |
| [`lazily-cpp`](https://github.com/lazily-hub/lazily-cpp) | C++ |
| [`lazily-zig`](https://github.com/lazily-hub/lazily-zig) | Zig |
| [`lazily-dart`](https://github.com/lazily-hub/lazily-dart) | Dart / Flutter |
| [`lazily-react`](https://github.com/lazily-hub/lazily-react) | React / Preact bindings layered over `lazily-js` (not a separate language binding) |

See [`lazily-spec`](https://github.com/lazily-hub/lazily-spec) for the canonical
Snapshot/Delta schemas, the IPC Lean proofs of the epoch/memo/batch invariants,
and the conformance fixtures every IPC-capable binding validates against. The
language-agnostic formal model — the flat FSM kernel and the full Harel state
chart — lives in [`lazily-formal`](https://github.com/lazily-hub/lazily-formal).

Per-binding parity, with per-cell notes and platform carve-outs, lives in
[`lazily-spec` § Cross-Language Coverage](https://github.com/lazily-hub/lazily-spec/blob/main/docs/coverage.md)
— it is generated from `coverage.json`, so it does not rot the way a hand-copied
table would.

## Development

This project uses [`uv`](https://github.com/astral-sh/uv). Run the local
CI-equivalent suite — type-check (`ty`), lint (`ruff`), the runnable README
example, and the test suite — with:

```bash
uv run poe precommit
```

`SPEC.md` is the authoritative specification for the Python primitives and the
`lazily-spec` compliance notes.

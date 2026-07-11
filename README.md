# lazily

Lazy reactive primitives for Python — Slots, Cells, and Signals with automatic
dependency tracking and cache invalidation, plus the language-agnostic
`lazily-spec` wire protocol for mirroring graph state across processes and
languages.

[![PyPI](https://img.shields.io/pypi/v/lazily.svg)](https://pypi.org/project/lazily/)

## Overview

`lazily` provides a small reactive family for context-aware computation:

- **Slot** — a lazily-computed cached value that automatically tracks its dependencies (`slot` / `slot_def`).
- **Cell** — a mutable value that invalidates dependent Slots when it changes (`cell` / `cell_def`, `Cell`, `CellSlot`).
- **Signal** — an *eager* derived value that recomputes the instant a dependency invalidates, with no intermediate unset value (`signal` / `signal_def`).

Values are **lazy by default**: dependents are marked dirty on invalidation but
only recompute when accessed. When you need eager push-style semantics —
recompute immediately, observe `v1 → v2` with no unset window — reach for
**`Signal`**, which layers a puller over a memoized Slot. The
`Slot → Cell → Signal` progression lets you choose lazy or eager per derived
value within one graph. An equal recompute is suppressed by a `PartialEq`/memo
guard, so unchanged values never cascade downstream work.

There is **no dedicated `Context` class** — a plain `dict` is the context. Slots
use themselves as dictionary keys to cache values, so any dict works as the
reactive "world."

## Feature coverage

The full `lazily` capability set across every binding. Legend: ✅ shipped ·
`~` partial · `—` absent or not applicable. The canonical matrix with per-cell
notes and platform carve-outs lives in
[`lazily-spec` § Cross-Language Coverage](../lazily-spec/docs/coverage.md).

<!-- coverage-table:start -->
| Feature | Rust | Python | Kotlin | JS | Dart | Zig | Go | C++ |
| --------- | :----: | :------: | :------: | :--: | :----: | :---: | :--: | :---: |
| Reactive graph — `Cell` / `Slot` / `Signal` / `Effect` / memo / batch | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Reactive family (`ReactiveFamily`) — keyed cell/slot family + materialization mode (`#lzmatmode`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — | ✅ |
| Thread-safe context (lock-backed) | ✅ | ✅ | ✅ | — | — | ✅ | ✅ | ✅ |
| Async reactive context | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Flat state machine | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Harel state charts | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Keyed cell collections (`CellMap` / `CellTree`) + reconcile | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Memoized semantic tree (`SemTree`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Stable-id alignment (manufactured identity) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Reactive queue (`QueueCell` SPSC/MPSC + `QueueStorage` adapter) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Free-text character CRDT (`TextCrdt`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `TextCrdt` delta sync (`version_vector` / `delta_since` / `apply_delta`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Move-aware sequence CRDT (`SeqCrdt`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Lossless tree CRDT core (`LosslessTreeCrdt`, M1) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Lossless tree — dotted-frontier anti-entropy | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Lossless tree — concurrent merge convergence | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Registers (LWW / MV) + `PnCounter` + `CellCrdt` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| IPC wire — `Snapshot` + `Delta` + `CrdtSync` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Shared-memory blob path (`ShmBlobArena`) | ✅ | ✅ | ✅ | ~ | ~ | ✅ | ✅ | ✅ |
| Cross-process zero-copy transport (`BlobBackend` / shm / arrow) | ✅ | ✅ | ✅ | — | ✅ | — | ✅ | ✅ |
| Distributed CRDT plane (`CrdtPlaneRuntime` / anti-entropy) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Distributed plane — WebRTC transport + signaling | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| State projection / mirror | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Causal receipts (`CausalReceipts` outcome projection) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Message-passing + RPC command plane (`command-plane-v1`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| C-ABI FFI boundary | ✅ | ✅ | ✅ | — | ✅ | ✅ | ✅ | ✅ |
| Permission boundary (`PeerPermissions` / `RemoteOp`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Capability negotiation (`SessionHandshake`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Instrumentation / benchmarks | ✅ | ✅ | ✅ | — | ✅ | ✅ | ✅ | ✅ |
<!-- coverage-table:end -->

## Installation

```
pip install lazily
```

## Example usage

```python
from lazily import CellSlot, cell, slot

# Cells hold a value that can be updated.
name = CellSlot[dict, dict, str]()


# Slots are functions that depend on cells and other slots.
@slot
def greeting(ctx: dict) -> str:
    print("Calculating greeting...")
    return f"Hello, {name(ctx).value}!"


# A CellSlot can also have a default value.
@cell
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
| `Slot[C_in, C_ctx, T]` | Slot with dependency tracking and invalidation |
| `slot` | Decorator: `Slot` with an identity context resolver |
| `slot_def(resolve_ctx)` | Decorator factory for a custom context resolver |

### Cell

A `Cell` holds a mutable value. Reading `cell.value` inside a Slot auto-subscribes
that Slot; assigning `cell.value = x` (or `cell.set(x)`) compares old and new via
`!=` and, only if changed, cascades invalidation to dependents.

| Type | Purpose |
|------|---------|
| `Cell[T]` | Mutable value with subscription support |
| `CellSlot[C_in, C_ctx, T]` | Slot that returns a `Cell` |
| `cell` | Decorator: `CellSlot` with an identity resolver |
| `cell_def(resolve_ctx)` | Decorator factory for a custom context resolver |

### Signal

A `Signal` is the **eager** counterpart to a lazy Slot — one step further along
the `Slot → Cell → Signal` progression. Where a Slot marks itself dirty on
invalidation and recomputes on the next read, a Signal recomputes *the instant a
dependency is invalidated*, before the mutating call returns. The value is always
materialized, so observers never see an intermediate unset value.

```python
from lazily import CellSlot, signal

n = CellSlot[dict, dict, int]()


@signal
def doubled(ctx: dict) -> int:
    return n(ctx).value * 2


ctx: dict = {}
n(ctx).value = 1

s = doubled(ctx)   # eager: materialized now
print(s.value)     # 2

n(ctx).value = 5   # doubled recomputes immediately
print(s.value)     # 10 — already current, no lazy read needed
```

A Signal is **composed from existing primitives**, not a parallel engine: a
memoized Slot supplies glitch-free, memo-guarded recomputation, and a small
puller re-materializes it after every invalidation to supply the eagerness.
Consequently a Signal inherits the memo guard (an equal recompute suppresses the
downstream cascade). `signal.dispose()` removes the eager puller — the value
stays readable but reverts to lazy (recompute-on-read) behavior.

| Type | Purpose |
|------|---------|
| `Signal[T]` | Eager derived value bound to a single context |
| `signal` | Decorator: context-cached eager-Signal factory (one Signal per context) |
| `signal_def(resolve_ctx)` | Decorator factory with a custom context resolver |

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

- **`CellMap` / `CellFamily` / `CellTree`** — keyed reactive collections with
  independent value/membership/order signals and atomic move.
- **`QueueCell`** — a reactive FIFO queue (SPSC primitive with an MPSC-via-`batch`
  usage rule) with a pluggable `QueueStorage` backend. Reader-kind invalidation
  (head/len/is_empty/is_full/closed), bounded reactive backpressure via `is_full`,
  and the closure lifecycle (drain / Closed-distinct-from-Empty / idempotent).
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

## Reactive queue — `lazily.queue`

`QueueCell` is a FIFO collection composed of reactive cells — **not a new cell
kind** — that adds queue semantics (push to tail, pop from head) to the reactive
graph. It is an SPSC primitive; MPSC is a *usage rule* on the same primitive —
multiple producers push inside one `batch`, which serializes the pushes into a
deterministic order. The reactive shell wraps a pluggable `QueueStorage` backend
(default `VecDequeStorage`); the shell owns the reader-kind version cells and
invalidates by reader kind — a push to a non-empty queue does NOT invalidate the
`head` reader, a pop does.

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

The reader-kind independence law (a push to a non-empty queue does not change
`head`, so the `head` reader is not invalidated) comes for free from the Cell
`!=` (PartialEq) guard: after each op the shell re-derives each reader-kind cell
from storage and writes it back, and a cell whose value did not change is not
invalidated.

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
the in-library micro-suite (reactive core, keyed reconciliation, `CellMap`,
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

`lazily-py` is one binding in a cross-language reactive family that shares the
`lazily-spec` wire protocol:

| Binding | Language | Package |
|---------|----------|---------|
| [`lazily-rs`](https://github.com/lazily-hub/lazily-rs) | Rust | `lazily` (crates.io) |
| **`lazily-py`** | Python | `lazily` (PyPI) |
| [`lazily-zig`](https://github.com/lazily-hub/lazily-zig) | Zig | GitHub |
| [`@lazily/signaling`](https://github.com/lazily-hub/lazily-js) | TypeScript / Cloudflare Worker | npm |
| [`lazily-spec`](https://github.com/lazily-hub/lazily-spec) | — | wire protocol + conformance fixtures |
| [`lazily-formal`](https://github.com/lazily-hub/lazily-formal) | — | Lean 4 formal model (FSM kernel + Harel state chart) |

See [`lazily-spec`](https://github.com/lazily-hub/lazily-spec) for the canonical
Snapshot/Delta schemas, the IPC Lean proofs of the epoch/memo/batch invariants,
and the conformance fixtures every IPC-capable binding validates against. The
language-agnostic formal model — the flat FSM kernel and the full Harel state
chart — lives in [`lazily-formal`](https://github.com/lazily-hub/lazily-formal).

## Development

This project uses [`uv`](https://github.com/astral-sh/uv). Run the local
CI-equivalent suite — type-check (`ty`), lint (`ruff`), the runnable README
example, and the test suite — with:

```bash
uv run poe precommit
```

`SPEC.md` is the authoritative specification for the Python primitives and the
`lazily-spec` compliance notes.

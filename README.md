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

See [`lazily-spec`](https://github.com/lazily-hub/lazily-spec) for the canonical
Snapshot/Delta schemas, the Lean 4 formal model of the epoch/memo/batch
invariants, and the conformance fixtures every IPC-capable binding validates
against.

## Development

This project uses [`uv`](https://github.com/astral-sh/uv). Run the local
CI-equivalent suite — type-check (`ty`), lint (`ruff`), the runnable README
example, and the test suite — with:

```bash
uv run poe precommit
```

`SPEC.md` is the authoritative specification for the Python primitives and the
`lazily-spec` compliance notes.

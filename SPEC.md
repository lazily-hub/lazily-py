# lazily-py Specification

Python library for lazy evaluation with context-aware dependency injection and cache invalidation.

## Core Concepts

### Context

A plain `dict` serves as the context. Slots use themselves as dictionary keys to store cached values. No dedicated Context class — any dict works.

### Slot

Lazily-computed cached value with automatic dependency tracking via a global `slot_stack`.

**Types:**

| Type | Purpose |
|------|---------|
| `BaseSlot[C_in, C_ctx, T]` | Base slot without subscriber support |
| `Slot[C_in, C_ctx, T]` | Slot with dependency tracking and invalidation |
| `slot[C_ctx, T]` | Convenience: Slot with identity context resolver |
| `slot_def(resolve_ctx)` | Decorator factory for custom context resolvers |

**BaseSlot methods:**

| Method | Purpose |
|--------|---------|
| `__call__(ctx)` | Compute or return cached value |
| `get(ctx)` | Return cached value or `None` |
| `reset(ctx)` | Clear cached value |
| `is_in(ctx)` | Check if value is cached |

**Slot additions:**

| Method | Purpose |
|--------|---------|
| `subscribe(subscriber)` | Register invalidation callback |
| `touch(ctx)` | Notify all subscribers |
| `reset(ctx)` | Clear cache + notify subscribers + clear subscriber list (re-entrancy-safe: subscribers are cleared before notification so a subscriber-triggered reset cannot mutually recurse) |

### Cell

Mutable value holder that notifies dependent slots when changed.

**Types:**

| Type | Purpose |
|------|---------|
| `Cell[T]` | Mutable value with subscription support |
| `CellSlot[C_in, C_ctx, T]` | Slot that returns a Cell |
| `cell[C_ctx, T]` | Convenience: CellSlot with identity resolver |
| `cell_def(resolve_ctx)` | Decorator factory for custom context resolvers |

**Cell operations:**

| Property/Method | Purpose |
|-----------------|---------|
| `cell.value` (get) | Read value; auto-subscribes calling slot |
| `cell.value = x` (set) | Update value; invalidate dependents if changed |
| `cell.get()` | Alias for value getter |
| `cell.set(x)` | Alias for value setter |
| `cell.subscribe(callback)` | Register change callback |
| `cell.touch()` | Notify all subscribers |

### Signal

Eager derived value — the third member of the `Slot → Cell → Signal` family.
Where a `Slot` is **lazy** (invalidation marks it dirty and the value recomputes
on the next read), a `Signal` is **eager**: it computes once at construction and
recomputes immediately whenever a tracked dependency changes. It is composed from
existing primitives — a memoized `Slot` plus a puller that re-pulls the slot on
invalidation — and applies a memo/PartialEq guard so an eager recompute that
yields an equal value suppresses the downstream cascade.

**Types:**

| Type | Purpose |
|------|---------|
| `Signal[T]` | Eager derived value bound to a single context |
| `signal(callable)` | Decorator: context-cached eager-Signal factory (one Signal per context) |
| `signal_def(resolve_ctx)` | Decorator factory with a custom context resolver |

**Signal operations:**

| Property/Method | Purpose |
|-----------------|---------|
| `signal.value` (get) | Read current value; auto-subscribes calling slot |
| `signal.get()` / `signal()` | Aliases for the value getter |
| `signal.subscribe(callback)` | Register change callback |
| `signal.touch()` | Notify all subscribers |
| `signal.is_active()` | Whether the eager puller is still installed |
| `signal.dispose()` | Remove the eager puller; value reverts to lazy (recomputed on next read) |

**Semantics:**

- **Eager activation:** the value is materialized at creation, so there is no
  intermediate unset state.
- **Memo guard:** an eager recompute that yields an equal value suppresses
  `touch()` and downstream invalidation, exactly like a memoized slot.
- **Wire representation:** a Signal is not a separate wire type. On the
  `lazily-spec` wire it is the ordinary backing slot node that stores its
  materialized value; the puller is local execution state and is never
  serialized. See **lazily-spec Compliance** below.

## Dependency Tracking

Uses a global `slot_stack: list[Slot]` (acts as thread-local execution context).

1. When a Slot computes, it pushes itself onto `slot_stack`
2. Any child Slot or Cell accessed during computation sees the parent on the stack
3. The child registers a subscriber that calls `parent.reset()` when the child changes
4. When a Cell value changes (and differs from old value), `touch()` cascades invalidation

**Key invariant:** Subscribers are cleared on `reset()`, forcing re-registration on next access. This prevents stale subscriptions.

## Invalidation Semantics

- `Cell.value = new_value` → if changed: `touch()` → subscribers → `parent.reset()` → cascade
- `Slot.__call__(ctx)` → compute or return cached value; does **not** cascade invalidation (computation must not trigger subscriber resets)
- `Slot.reset(ctx)` → clear cache → snapshot + clear subscribers → notify snapshot → cascade up dependency tree. Clearing before notification makes reset re-entrancy-safe: a subscriber that itself triggers a reset finds an empty subscriber set, preventing mutual recursion.
- Value equality check: Cells only invalidate when `new_value != old_value`

## Context Resolvers

Custom context resolvers allow non-dict inputs to resolve to the underlying context dict:

```python
@slot_def(resolve_ctx)
def my_slot(ctx: dict) -> str:
    return "computed"

# Can be called with CustomCtxResolver or plain dict
result = my_slot(custom_resolver)
```

## Type System

- `LazilyCallable[C, T]` — Protocol for context-consuming callables
- `ResolveCallable[R, C]` — Protocol for context resolvers
- Full generic type annotations with `C_in`, `C_ctx` (bound to dict), `T`

## lazily-spec Compliance (IPC Wire Protocol)

`lazily.ipc` implements the language-agnostic [`lazily-spec`](https://github.com/lazily-hub/lazily-spec)
wire protocol so a Python reactive graph's state can be mirrored to remote
observers across processes and languages. The JSON representation is
**byte-compatible** with the Rust reference (`lazily-rs`) and the Zig binding.

### Wire types

| Type | Wire form |
|------|-----------|
| `IpcMessage` | Externally-tagged: `{"Snapshot": …}`, `{"Delta": …}`, or `{"CrdtSync": …}` |
| `Snapshot` | `{ epoch, nodes[], edges[], roots[] }` |
| `NodeSnapshot` | `{ node, type_tag, state, key? }` (`key` omitted when absent) |
| `NodeState` | `{"Payload": [u8…]}` \| `{"SharedBlob": {…}}` \| `"Opaque"` |
| `NodeKey` | Bare string path (`scores/alice`); optional on `NodeSnapshot` / `NodeAdd` |
| `EdgeSnapshot` | `{ dependent, dependency }` |
| `Delta` | `{ base_epoch, epoch, ops[] }` |
| `DeltaOp` | 7 variants: `CellSet`, `SlotValue`, `Invalidate`, `NodeAdd`, `NodeRemove`, `EdgeAdd`, `EdgeRemove` (`NodeAdd` carries an optional `key`) |
| `IpcValue` | `{"Inline": [u8…]}` \| `{"SharedBlob": {…}}` |
| `ShmBlobRef` | `{ offset, len, generation, epoch, checksum }` |
| `WireStamp` | `{ wall_time, logical, peer }` (CRDT HLC stamp mirror) |
| `CrdtOp` | `{ node, key, stamp, state }` (state-based / CvRDT) |
| `CrdtSync` | `{ frontier[], ops[] }` (anti-entropy multi-writer plane) |
| `CapabilityHandshake` | Standalone frame: `{ protocol_id, protocol_major_version, codec, … }` |

**Conventions matching the normative fixtures:**

- Enums are **externally tagged** (the variant name is the single JSON key).
- Wire-stable identifiers (`NodeId`, `PeerId`) are bare JSON integers; keep them
  ≤ `2**53` for JavaScript/TypeScript peers.
- Serialized value bytes are JSON **arrays of `u8`**, not base64.
- `NodeKey` is **additive**: a missing `key` field decodes to `None` (`null`),
  so pre-`key` encoders and existing conformance fixtures round-trip unchanged.
  A `None` `key` is omitted from `NodeSnapshot` / `NodeAdd` (self-describing
  codecs); `CrdtOp.key` is emitted as `null` when unset (matches the Rust
  derived struct). Path bounds (`NODE_KEY_MAX_LEN = 1024`, `NODE_KEY_MAX_SEGMENTS = 32`)
  are enforced on construction and on the wire.
- `IpcMessage.encode_json()` / `decode_json()` move transport-agnostic bytes
  (unix socket, pipe, WebSocket, WebRTC data channel, shared memory).

### NodeKey

A `NodeKey` is a `/`-joined path (`scores/alice`, `outer/k1/inner/k2`) — an
optional wire-stable keyed address that survives `NodeId` churn. Unlike
`NodeId` (a volatile internal handle a producer may re-mint after a resync or
remove-then-readd), a key is producer-defined and stable, so a peer can
subscribe to "entry `scores/alice`" without an out-of-band key→NodeId map.

- `NodeKey.new(path)` / `NodeKey.from_segments(parts)` — validated construction
  (raises `NodeKeyError`: `Empty`, `TooLong`, `TooManySegments`, `EmptySegment`).
- `NodeSnapshot.with_key(key)` / `DeltaOp.node_add(node, type_tag, state, key)`
  attach a key; the `key` field is omitted from JSON when unset.

### Distributed: CRDT cell plane

`CrdtSync` rides the same `lazily-ipc` transport as `Snapshot`/`Delta` as a
third `IpcMessage` variant. It is the multi-writer anti-entropy plane
(`merge: crdt`): each `CrdtOp` ships a converged state-based register value
tagged with a `WireStamp` (the wire mirror of the runtime HLC stamp); the
`frontier` advertises the sender's per-peer highest observed stamp so the
receiver can compute the causal-stability watermark. Merges are commutative,
associative, and idempotent, so out-of-order or duplicated delivery converges.

- `CrdtSync.filter_readable(permissions, peer)` omits ops for non-readable
  nodes entirely (omission, not redaction) while retaining the full frontier.
- Wiring the plane to live `merge: crdt` root cells is a follow-on runtime slice;
  this binding ships the codec-stable wire types.

### Capability negotiation

`CapabilityHandshake` is the standalone frame exchanged before any graph state
flows (it is not an `IpcMessage` variant). Peers that disagree on
`protocol_major_version`, `codec`, or `ordered_reliable` fail closed before any
`Snapshot` or `Delta` is applied.

- `CapabilityHandshake.new(peer_id, session_id)` — protocol defaults (JSON codec,
  1 MiB frame, ordered-reliable, no features).
- `handshake.is_compatible_with(other)` — fail-closed compatibility check.
- Constants `PROTOCOL_ID = "lazily-ipc"`, `PROTOCOL_MAJOR_VERSION = 1`.

### Epoch sequencing

A context-level monotonic `ipc_epoch` advances once per outermost batch flush.
Each `Delta` carries `{ base_epoch, epoch }` with `epoch == base_epoch + 1`.

- `Delta.is_next_after(last_epoch)` — whether the delta applies in sequence.
- `Delta.apply_status(last_epoch)` — `DeltaApplyStatus.apply()` when sequential,
  else `DeltaApplyStatus.resync_required(last_epoch, base_epoch, epoch)`, which
  tells the receiver to discard the delta and request a fresh `Snapshot`.

### Shared-memory blob arena (host)

`ShmBlobArena` ports the `lazily-rs` `ShmBlobArena<B>` host capability
(`ipc.rs`) and mirrors `lazily-zig` `ShmBlobArena` (`ipc.zig`), so a Python
process can **host** shared-memory blob payloads rather than only carry
`ShmBlobRef` descriptors produced elsewhere. The arena is a flat `bytearray`
plus an append-only write cursor; each write emits a `ShmBlobRef` descriptor and
prepends a 40-byte header (`LZSH` magic, version, header length, generation,
epoch, payload length, FNV-1a-64 checksum). Reads validate bounds, the header,
generation/epoch/length, and the checksum before returning a zero-copy
`memoryview`. Append-only with wraparound; each write bumps a generation counter
so a stale descriptor landing on an overwritten region fails validation instead
of returning torn data. Descriptors are byte-compatible with the Rust and Zig
arenas (identical header layout + FNV-1a-64 constants).

- `ShmBlobArena.with_capacity(n)` / `ShmBlobArena.from_buffer(buf)` — allocate
  a fresh `bytearray` or wrap externally-owned storage (e.g. an `mmap` region
  cast to `bytearray`); caller keeps `buf` ownership in the latter case.
- `arena.write_blob(epoch, payload) -> ShmBlobRef`
- `arena.read_blob(ref) -> memoryview` (zero-copy, read-only)
- `arena.capacity` / `arena.max_blob_len` / `arena.write_offset`
- Errors: `ShmBlobArenaError` base with variants `ShmBlobCapacityTooSmall`,
  `ShmBlobTooLarge`, `ShmBlobDescriptorOutOfBounds`, `ShmBlobDescriptorMismatch`,
  `ShmBlobChecksumMismatch`, `ShmBlobGenerationOverflow` — matching the Rust enum
  and Zig error set. `SHM_BLOB_HEADER_LEN` is exported.

True cross-process OS shared memory (`/dev/shm`, `mmap`) is out of scope here
and is a follow-on that swaps the backing buffer; this port establishes the
in-process arena and host parity across siblings.

### Permission boundary (omission, not redaction)

`PeerPermissions` is a default-deny per-peer allowlist gating `read`, `write`,
and `trigger_effect` (`OpKind`) **independently** — a read grant never implies
write or effect-trigger. Non-readable nodes are **omitted entirely** from a
snapshot/delta (not redacted in place), so a peer cannot infer their existence:

- `Snapshot.filter_readable(permissions, peer)` — drops unreadable nodes; keeps
  an edge only when both endpoints are readable; preserves root order.
- `Delta.filter_readable(permissions, peer)` — drops ops whose target node (or
  either edge endpoint) is unreadable.
- `permissions.check(peer, op)` raises `PermissionDenied` (fail-closed).

### Conformance

`tests/test_conformance.py` validates the canonical `lazily-spec/conformance`
fixtures (preferring the sibling spec repo, falling back to a vendored copy under
`tests/conformance/`). Each test parses the fixture `wire` into a native
`IpcMessage`, asserts the language-agnostic `assertions`, and re-serializes to
confirm round-trip fidelity — the same contract the Rust and Zig bindings run.

## Requirements

- Python 3.12+
- Zero external dependencies (the `lazily.ipc` wire protocol uses only the
  standard-library `json` module)

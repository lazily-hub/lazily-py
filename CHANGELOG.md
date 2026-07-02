## Unreleased

## 0.13.0

Full `lazily-spec` wire-protocol compliance: the IPC types the binding was
missing versus `protocol.md` (matching the `lazily-rs` reference for
cross-language byte parity), plus a schema-compliance test proving lazily-py's
serializer output validates against the canonical `lazily-spec` JSON Schemas.

* **NodeKey** (`#lzwirekey`) — the optional wire-stable keyed address the spec
  explicitly names lazily-py as needing. `NodeKey`/`NodeKeyError` with
  `NODE_KEY_MAX_LEN`/`NODE_KEY_MAX_SEGMENTS` bounds (validated on construction
  and on the wire); optional `key` on `NodeSnapshot` (`.with_key`) and
  `DeltaOp.node_add`, omitted from JSON when `None` so pre-`key` encoders and
  existing conformance fixtures round-trip unchanged.
* **Distributed CRDT plane** (`#lzcrdtplane5a`) — `WireStamp`, `CrdtOp`
  (`new`/`keyed`, state-based CvRDT), `CrdtSync` (frontier + ops,
  `filter_readable`). `IpcMessage` gains a third variant `{"CrdtSync": …}`.
* **Capability negotiation** — `CapabilityHandshake` +
  `PROTOCOL_ID`/`PROTOCOL_MAJOR_VERSION`; `is_compatible_with` fails closed on
  protocol id / major version / codec / `ordered_reliable` disagreement.
* **Schema compliance** — `tests/test_schema_compliance.py` validates
  lazily-py's `to_wire()` output (Snapshot, Delta with keyed NodeAdd, CrdtSync)
  against the sibling `lazily-spec/schemas`, closing the binding↔schema loop.

## 0.12.0

* Add `StateChart` — a full Harel/SCXML state-chart interpreter backed by a
  reactive `Cell[frozenset[str]]`, conforming to `lazily-spec`
  `docs/state-charts.md` and the Lean `StateChart` formal model
  (`lazily-formal`). The `Cell`'s `!=` (PartialEq) guard suppresses no-op
  self-transitions, and any `Slot`/`Signal` reading `configuration()` /
  `active_leaves()` / `matches()` is invalidated on a real transition.
* Implemented subset: compound states, orthogonal (parallel) regions, shallow +
  deep history, entry/exit/transition actions, named guards (fail-closed),
  external + internal transitions, `final`-as-leaf. `run` actions and
  `{expr: ...}` context guards are rejected explicitly per spec.
* Replays all `lazily-spec/conformance/statechart/*.json` fixtures (flat cycle,
  hierarchical player, guarded door, parallel regions, shallow/deep history,
  entry/exit actions).
* Exports `ChartDef` and `StateChart`.

## 0.11.1

* Docs: refresh README — Signal family, `lazily.ipc` wire protocol, ShmBlobArena,
  and the lazily language family.
* Docs: reference the sibling `lazily-spec` (wire protocol + conformance) and
  `lazily-formal` (Lean 4 formal model) projects.

## 0.11.0

* Add `StateMachine[S, E]` — a finite state machine backed by a reactive `Cell`,
  so any `Slot` that reads the machine's state is invalidated on transition.
  Mirrors `lazily-rs` `StateMachine<S, E>` and `lazily-zig` `StateMachine(S, E)`.
* Add `ShmBlobArena` host capability to `lazily.ipc` — ports the `lazily-rs`
  `ShmBlobArena<B>` shared-memory blob arena and mirrors `lazily-zig`
  `ShmBlobArena`: a `bytearray`-backed arena with a 40-byte header
  (`LZSH` magic + FNV-1a-64 checksum), append-only writes with wraparound, and
  full read validation. A Python process can now host blob payloads (not just
  carry `ShmBlobRef` descriptors). Descriptors are byte-compatible with the
  Rust and Zig arenas. Exports `ShmBlobArena`, `ShmBlobArenaError` (with six
  variant subclasses), and `SHM_BLOB_HEADER_LEN`.
* Add `lazily.ipc`: the language-agnostic `lazily-spec` IPC wire protocol
  (Snapshot / Delta) with JSON byte-compatible with the Rust and Zig bindings —
  `IpcMessage`, `Snapshot`, `Delta`, the 7 `DeltaOp` variants, `NodeState` /
  `IpcValue` / `ShmBlobRef`, epoch sequencing (`apply_status` / resync), and a
  default-deny `PeerPermissions` boundary that omits non-readable nodes.
* Add conformance tests against the canonical `lazily-spec/conformance` fixtures
  (vendored under `tests/conformance/` for standalone CI).
* Add the eager `Signal` primitive — the third member of the
  `Slot → Cell → Signal` family (`signal` / `signal_def` decorators, eager
  recompute with a memo guard, `dispose()` reverts to lazy).
* Fix `RecursionError` when a `Signal` reads a `Slot` dependency: `Slot.__call__`
  no longer fires `touch()` during computation (computation must not cascade
  invalidation), and `Slot.reset` clears subscribers before notification so a
  subscriber-triggered reset cannot mutually recurse.

## D.10.0

* Fix cell type inference.
* Drop support for python version <3.12.
* Use uv + mise for development package management.

## 0.9.0

* Add type-aware context to slot and cell functions.
* The ctx argument type can be enforced via mypy.
* mypy tests now pass

## 0.8.0

* Add Cell.get() as an alias to Cell.value
* Add Cell.set(value) as an alias to Cell.value = value

## 0.7.0

* Add @slot_def and @cell_def decorators...which both take a resolve_ctx callable to resolve the ctx object.
* Allows for calling a slot function with an object that contains the ctx, such as a Request or Graphene
  GraphQLResolveInfo.

## 0.6.0

* cell() is supported with a default value of None.

## 0.5.0

* Slot now support subscriptions. Resetting a slot will also reset it's descendents.

## 0.4.0

* BREAKING CHANGE: Rename Cell to Slot and @cell to @slot.
* BREAKING CHANGE: Cell is now a subscribable that resets a parent Slots when it changes.

## 0.3.0

* BREAKING CHANGE: Rename be to cell and Be to Cell.

## 0.2.0

* BREAKING CHANGE: Remove be_class due to complexity. If a subclass of Be is needed, then users are encouraged to use
  create a singleton instance of the subclass.

## 0.1.0

* Initial release
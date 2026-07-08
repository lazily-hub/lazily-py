## Unreleased

## 0.17.0

Completes the two remaining `lazily-spec` feature rows for Python — the lossless
tree CRDT and the command / RPC message plane — so every row in the
`lazily-spec` Feature coverage table is `✅` for Python. Each is pinned by its
canonical `lazily-spec/conformance/` fixtures and a wire-schema compliance test.

* **Lossless tree CRDT** (`lazily.lossless_tree_crdt`) — `LosslessTreeCrdt`, the
  M1 syntax-agnostic core (#lzlosstree): a single rooted concrete-syntax tree
  whose leaves own every rendered byte (`render(tree) == source_text` for valid,
  invalid, and unknown source). Create / tombstone / intra-parent reorder /
  leaf-edit / split-leaf / merge-adjacent-leaves; leaf text embeds `TextCrdt`
  wholesale; child order is a fractional index (`key_between`); the clock is a
  Lamport `TreeOpId`.
  * **Dotted-frontier anti-entropy** — `TreeVersionFrontier` is a dot *set*
    (contiguous prefix + sparse holes), never a per-peer max, so a missing
    non-contiguous op stays representable and re-requestable (`diff` /
    `apply_update` converge through delivery gaps). UTF-8 byte-offset leaf edits
    (`byte_to_char`), idempotent + order-tolerant apply, and the externally-tagged
    wire codec (`tree_update_to_wire` / `tree_update_from_wire`).
  * **Concurrent merge convergence** — `Reorder` is LWW on `sort_stamp`;
    `Tombstone` is sticky-min; concurrent inserts in the same gap survive with
    byte-identical sort keys; incompatible shapes both survive (text
    preservation wins over semantic shape).
* **Command / RPC message plane** (`lazily.command`, `command-plane-v1`) — the
  additive sibling to `Snapshot` / `Delta` / `CrdtSync`: `CommandSubmit` /
  `CommandCancel` / `CommandEvents` / `CommandProjection`. The pure
  `CommandProjection` reducer folds frames + terminal `CausalReceipt` authority;
  `CommandRpcClient` is the derived RPC facade. Terminal authority is the receipt
  (never a transport ACK / `accepted` / `started` event); generation guards,
  idempotency, cancel-before-terminal-only, terminal-conflict-fails-closed, and
  reconnect equivalence all hold.

## 0.16.0

Full cross-language feature coverage. Every row in the `lazily-spec` Feature
coverage table is now `✅` for Python, each pinned by its canonical
`lazily-spec/conformance/` fixture and a property test.

* **Reactive graph completed** — sync `Effect` (`lazily.effect`: a side-effecting
  observer that reruns on dependency change, with cleanup-before-body and
  terminal `dispose`) plus the top-level `batch(run)` / `batch_context()`
  boundary (`lazily.batch`: coalesces cell writes into one invalidation + effect
  flush at the outermost boundary). `Cell` now tracks auto-discovered parents by
  identity (fixing a `functools.partial` non-deduplication fan-out leak) and
  routes changes through the batch-aware `notify_change` hook.
* **Cell-model layers** — `SemTree` (memoized semantic tree: ancestor-chain-only
  recompute + memo equality guard), `stable_id` (manufactured identity for text:
  anchors / content hashes / word-LCS alignment), `TextCrdt` (Fugue/RGA character
  CRDT + `#lztextsync` delta sync — `version_vector` / `delta_since` /
  `apply_delta`), `SeqCrdt` (move-aware sequence CRDT — single-LWW-reassignment
  moves converge without duplication).
* **CRDT registers** (`lazily.crdt_registers`) — `LwwRegister`, `MvRegister`
  (causal-context multi-value), `PnCounter`, and `CellCrdt` (CRDT cell that
  propagates into the reactive `Cell` plane, PartialEq-guarded after merge).
* **Distributed CRDT plane** (`lazily.crdt_plane`) — `CrdtPlaneRuntime`
  anti-entropy: state-based `CrdtOp` ingress, greatest-stamp-wins per
  `(node, key)`, `(node, stamp)` op-log dedup (idempotent redelivery), per-peer
  stamp frontier, and the causal-stability watermark.
* **Signaling plane** (`lazily.signaling`) — `SignalingFrame` envelope +
  `RoomCore` room state machine implementing the anti-spoof routing invariant
  (server-stamped `from`, self-excluding `welcome` roster, `to`/`from` never
  both present). `open` and `allowlist` permission modes.
* **State projection / mirror** (`lazily.projection`) — `StateMirror` projects a
  local reactive context onto the `Snapshot`/`Delta` wire plane with the
  value-mirror default and `PeerPermissions` omission filtering.
* **Instrumentation / benchmarks** (`lazily.benchmarks`) — `run_benchmarks()`
  micro-benchmarks for the reactive core, keyed reconciliation, `CellMap`,
  `TextCrdt`, and `CrdtPlaneRuntime`; runnable as `python -m lazily.benchmarks`.
* **Coverage table** — all Python marks `✅` (including the Reactive graph row);
  the table is regenerated from `lazily-spec/coverage.json` via
  `sync-coverage.mjs`.

## 0.15.0

`lazily-spec` causal-receipt compliance. The receipt plane is the generic
outcome projection for commands and effect requests keyed by a stable
`causation_id` — deliberately **not** a transport ACK. This release ships the
full `CausalReceipts` wire frame, the pure projection kernel that mirrors
`LazilyFormal.Receipt`, and replays the canonical
`lazily-spec/conformance/receipts/causal_receipts.json` fixture.

* **Causal receipts** (`#lzreceipts`) — `CausalReceipt` / `CausalReceipts`
  wire types (a standalone externally-tagged frame, not an `IpcMessage`
  variant, matching `receipts.json`) plus `ReceiptOutcome`
  (`observed`/`accepted` non-terminal, `applied`/`rejected` terminal).
  `to_wire()`/`from_wire()`/`encode_json()`/`decode_json()` round-trip the
  canonical fixture byte-for-byte.
* **`ReceiptProjection`** — the pure reducer that folds receipts into the
  authoritative outcome for one causation id, porting
  `LazilyFormal.Receipt.apply`: duplicate `receipt_id`s are idempotent
  no-ops, stale-generation receipts are discarded, non-terminal receipts
  record without conflicting, the first terminal receipt fixes the outcome,
  and a second *different* terminal outcome fails closed (no winner). The
  authority `current_generation` is caller-supplied; `from_receipts`
  defaults it to the max generation seen when replaying a frame without
  external authority.
* **Conformance + schema coverage** — `test_conformance.py` replays the
  `causal_receipts.json` fixture (parse, round-trip, projection assertions);
  `test_schema_compliance.py` proves lazily-py's `to_wire()` output validates
  against the normative `receipts.json` schema; `test_receipt_properties.py`
  replays every named `LazilyFormal.Receipt` theorem.

## 0.14.0

Full `lazily-spec` compute-layer compliance + `lazily-formal` test-suite
integration. This release closes the remaining `MUST` layers of the
`lazily-spec` conformance matrix (keyed collections, async reactive context,
thread-safe context, C-ABI FFI) by porting the corresponding Lean formal models
in `lazily-formal`, and makes `lazily-formal` part of the lazily-py test suite:
`lake build` gates the suite on every theorem still checking.

* **Keyed reactive collections** (`#lzcellmap`) — `CellMap` and `CellFamily`
  with the three independent reactive signals (per-entry value, set-membership,
  order), atomic move (`move_to`/`move_before`/`move_after`), and per-key
  identity-stable minting. A pure reorder bumps the order signal only — `len`/
  `contains` readers are not invalidated (the wire-level "a pure reorder MUST
  NOT invalidate set-membership readers" invariant). Ports
  `LazilyFormal.Collection`.
* **Ordered keyed tree** (`CellTree`) — per-node value reactivity and per-level
  membership/order reactivity; atomic child move preserves identity. Ports
  `LazilyFormal.Tree`.
* **Keyed reconciliation** (`reconcile_ops`) — the move-minimized
  `{insert, remove, move, update}` op set over a longest-increasing-subsequence
  (LIS) kernel: stable (LIS) keys never move, and a stable entry with an
  unchanged value is neither moved nor updated. Replays the
  `lazily-spec/conformance/collections/keyed_reconciliation_lis.json` fixture.
  Ports `LazilyFormal.Reconciliation`.
* **Async reactive context** (`#lzasync`) — `AsyncSlot` with the exact
  `Empty / Computing / Resolved / Error` lifecycle and revision-tracked
  stale-completion discard (a stale completion is never published); `AsyncEffect`
  with cleanup-before-body ordering and batch-boundary scheduling (invalidation
  only queues, never runs inline); disposal is terminal. Pure `step` kernels
  mirror `LazilyFormal.AsyncSlotState` / `AsyncEffect`; the theorems are
  replayed as property tests.
* **Thread-safe reactive context** (`ThreadSafeContext`) — a lock-serialized
  `batch` boundary that coalesces concurrent cell writes into one invalidation
  pass (the coalesced frontier); a singleton batch refines the single-threaded
  `setCell`. Ports `LazilyFormal.ThreadSafe`.
* **C-ABI FFI boundary** (`lazily.ffi`) — `LazilyFfiStatus` (0..5),
  `LazilyFfiMessageKind` (including `CrdtSync = 3`, as the spec mandates),
  `LazilyFfiBytes` (`ptr`/`len`), and `encode_message`/`decode_message`
  re-encoding `IpcMessage` to canonical JSON bytes byte-compatible with the
  Rust/Zig FFI boundaries.
* **lazily-formal test-suite integration** — `tests/test_formal_build.py` runs
  `lake build` over the sibling `lazily-formal` Lean model and fails the suite
  if any theorem regresses (skipped when `lake`/`lazily-formal` is absent, e.g.
  a standalone PyPI sdist). Property tests mirror the named Lean theorems for
  every new module.

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
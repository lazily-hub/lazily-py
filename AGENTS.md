# lazily-py

Python port of the lazily **Cell kernel** (`#lzcellkernel`) — a `Source` cell
(value from outside; `set` / `merge`) and a `Computed` cell (value from upstream,
via a compute function), with `Effect` the value-less sink. `Cell` is the value
node concept; `Source` is its native writable handle and `Cell` is an
identity-preserving migration alias. Every cell is **guarded** — an equal recompute
suppresses the downstream cascade (matching TC39 `Signal.Computed`) — with **no
unguarded derived mode**; `computed` *is* the guarded derived constructor. The
eager construction is `computed(ctx, f).eager()` (`.eager()` / `.lazy()` /
`.is_eager()`); `Slot` is retained as the context-as-dict storage position (the
Python analog of `lazily-rs`'s surviving storage-sense `Slot`). The v1 value
vocabulary (`Signal` / `signal` / `signal_def`, `formula` / `formula_def` /
`FormulaCell`, `SourceCell` / `SourceCellSlot`, `.drive()` / `.undrive()` /
`is_driven` / `is_active`) is **removed**; `source` / `computed` are canonical
and `cell` / `cell_def` / `slot` are **deprecated** aliases. Python has no
compile-time read/write split (design §4) — the split is convention (a `Source`
has `set` / `merge`, a `Computed` does not). Includes
automatic dependency tracking, the full lazily-spec wire protocol, CRDT
collection types, the lossless tree CRDT, and the command/RPC message plane.

## Commit & Push

Commit and push completed work at the end of every turn that changed code,
tests, docs, or fixtures — do not leave finished work uncommitted. Run `make
check` first and ensure it is green; stage only the files that belong to the
change (never secrets or private customer names — see the workspace
`runbooks/private-name-hygiene.md`); write a concise commit message in the
repo's existing style; push to the current branch on `origin`. This standing
rule overrides the harness default of "commit only when explicitly asked" for
this repo.

<!-- tsift:code-navigation v=0.1.110 -->
## Code Navigation

Use the repository tsift skill at [`.agents/skills/tsift/SKILL.md`](.agents/skills/tsift/SKILL.md) for code search, source reading, call graphs, diffs, logs, and test output.
<!-- /tsift:code-navigation -->

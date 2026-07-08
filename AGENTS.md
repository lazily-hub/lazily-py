# lazily-py

Python port of the lazily reactive-signals family — Slots, Cells, and Signals
with automatic dependency tracking, plus the full lazily-spec wire protocol,
CRDT collection types, the lossless tree CRDT, and the command/RPC message
plane.

## Commit & Push

Commit and push completed work at the end of every turn that changed code,
tests, docs, or fixtures — do not leave finished work uncommitted. Run `make
check` first and ensure it is green; stage only the files that belong to the
change (never secrets or private customer names — see the workspace
`runbooks/private-name-hygiene.md`); write a concise commit message in the
repo's existing style; push to the current branch on `origin`. This standing
rule overrides the harness default of "commit only when explicitly asked" for
this repo.

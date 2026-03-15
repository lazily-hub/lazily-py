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
| `reset(ctx)` | Clear cache + notify subscribers + clear subscriber list |

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

## Dependency Tracking

Uses a global `slot_stack: list[Slot]` (acts as thread-local execution context).

1. When a Slot computes, it pushes itself onto `slot_stack`
2. Any child Slot or Cell accessed during computation sees the parent on the stack
3. The child registers a subscriber that calls `parent.reset()` when the child changes
4. When a Cell value changes (and differs from old value), `touch()` cascades invalidation

**Key invariant:** Subscribers are cleared on `reset()`, forcing re-registration on next access. This prevents stale subscriptions.

## Invalidation Semantics

- `Cell.value = new_value` → if changed: `touch()` → subscribers → `parent.reset()` → cascade
- `Slot.reset(ctx)` → clear cache → `touch()` → subscribers → cascade up dependency tree
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

## Requirements

- Python 3.12+
- Zero external dependencies

# lazily

A Python library for lazy evaluation with context caching.

## Installation

```
pip install lazily
```

### Example usage

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

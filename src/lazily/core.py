"""Focused Lazily Core facade.

This module only re-exports the established public objects. Importing from
``lazily`` and ``lazily.core`` therefore preserves class and function identity;
the facade is an additive package path, not a second reactive kernel.
"""

from .async_context import AsyncComputed, AsyncContext, AsyncSource, AsyncTeardownScope
from .batch import batch, batch_context, in_batch
from .cell import Source, source
from .compute import Compute, ComputeOps, Context
from .effect import Effect, effect
from .signal import Computed, computed, computed_ripple_when
from .teardown import TeardownScope, dispose_node, teardown_scope
from .thread_safe import ThreadSafeContext


__all__ = [
    "AsyncComputed",
    "AsyncContext",
    "AsyncSource",
    "AsyncTeardownScope",
    "Compute",
    "ComputeOps",
    "Computed",
    "Context",
    "Effect",
    "Source",
    "TeardownScope",
    "ThreadSafeContext",
    "batch",
    "batch_context",
    "computed",
    "computed_ripple_when",
    "dispose_node",
    "effect",
    "in_batch",
    "source",
    "teardown_scope",
]

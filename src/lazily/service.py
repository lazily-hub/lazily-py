"""Embedded-service plane (``#lzservice``).

The Python counterpart of ``lazily-rs/src/service.rs`` and
``lazily-spec/docs/service.md``. The story is "an instance is also a host of
services": :class:`HealthCell` / :class:`ReadinessCell` / :class:`DiscoveryCell`
/ :class:`ServiceRegistry`, each a pure compute **core** (an aggregation / keyed
map / durable log) split from a reactive **cell** projecting the composed view
onto a :class:`~lazily.cell.Cell`.

Each reactive cell holds one internal :class:`~lazily.cell.Cell` per asserted
reader. After every op the cell recomputes the reader value from its core and
assigns it back through the cell's ``!=`` (PartialEq) guard, so a reader
invalidates only when the projected value actually changes — the worst-component
health aggregate, the all-conditions readiness gate, the live discovery map, and
the replayable registry projection each invalidate exactly on a real change.
"""

from __future__ import annotations


__all__ = [
    "DiscoveryCell",
    "DiscoveryCore",
    "Health",
    "HealthCell",
    "HealthCore",
    "ReadinessCell",
    "ReadinessCore",
    "RegistryOp",
    "ServiceRegistry",
    "ServiceRegistryCore",
]

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .cell import Cell


# ===========================================================================
# Health
# ===========================================================================


class Health(str, Enum):
    """Composed health status; the worst component dominates."""

    Healthy = "Healthy"
    Degraded = "Degraded"
    Unhealthy = "Unhealthy"


@dataclass(slots=True)
class HealthCore:
    """Composed liveness-probe core. Each probe reports ``up`` and whether it is
    ``critical``."""

    probes: dict[str, tuple[bool, bool]] = field(default_factory=dict)

    def set(self, name: str, up: bool, critical: bool) -> None:
        """Set/refresh a probe by name."""
        self.probes[name] = (up, critical)

    def health(self) -> Health:
        """The aggregate: Unhealthy if any critical probe is down, else Degraded
        if any is down, else Healthy."""
        if any(critical and not up for up, critical in self.probes.values()):
            return Health.Unhealthy
        if any(not up for up, _critical in self.probes.values()):
            return Health.Degraded
        return Health.Healthy


class HealthCell:
    """Reactive health: projects the aggregate onto a :class:`Cell` for
    ``/health``. A ``health`` reader invalidates only when the aggregate
    changes."""

    __slots__ = ("_core", "_health")

    def __init__(self, ctx: dict) -> None:
        self._core = HealthCore()
        self._health: Cell[Health] = Cell(ctx, Health.Healthy)

    def _refresh(self) -> None:
        self._health.value = self._core.health()

    def set(self, name: str, up: bool, critical: bool) -> None:
        """Set/refresh a named probe, then reproject the aggregate."""
        self._core.set(name, up, critical)
        self._refresh()

    def health(self, ctx: Any = None) -> Health:
        """Reactive read of the composed health status. Invalidated only when the
        worst-component aggregate changes. Pass the caller's compute view
        (``ctx``) to value-thread the edge; omit for an untracked read
        (``#lzcellkernel``)."""
        if ctx is None:
            return self._health.value
        return ctx.read(self._health)

    def health_cell(self) -> Cell[Health]:
        """Handle to the underlying ``health`` cell (advanced wiring)."""
        return self._health


# ===========================================================================
# Readiness
# ===========================================================================


@dataclass(slots=True)
class ReadinessCore:
    """Composed readiness-probe core: ready iff every condition holds."""

    conditions: dict[str, bool] = field(default_factory=dict)

    def set(self, name: str, ready: bool) -> None:
        """Set/refresh a named readiness condition."""
        self.conditions[name] = ready

    def ready(self) -> bool:
        """Ready iff every recorded condition is true."""
        return all(self.conditions.values())


class ReadinessCell:
    """Reactive readiness: projects ``ready`` onto a :class:`Cell` for
    ``/ready``. A ``ready`` reader invalidates only when the gate flips."""

    __slots__ = ("_core", "_ready")

    def __init__(self, ctx: dict) -> None:
        self._core = ReadinessCore()
        self._ready: Cell[bool] = Cell(ctx, True)

    def _refresh(self) -> None:
        self._ready.value = self._core.ready()

    def set(self, name: str, ready: bool) -> None:
        """Set/refresh a named condition, then reproject the gate."""
        self._core.set(name, ready)
        self._refresh()

    def ready(self, ctx: Any = None) -> bool:
        """Reactive read of the readiness gate. Invalidated only when it flips.
        Pass the caller's compute view (``ctx``) to value-thread the edge; omit
        for an untracked read (``#lzcellkernel``)."""
        if ctx is None:
            return self._ready.value
        return ctx.read(self._ready)

    def ready_cell(self) -> Cell[bool]:
        """Handle to the underlying ``ready`` cell (advanced wiring)."""
        return self._ready


# ===========================================================================
# Discovery
# ===========================================================================


@dataclass(slots=True)
class DiscoveryCore:
    """Service-discovery core: ``service → (endpoint, owner peer)``. A peer's
    departure (:meth:`evict`) removes its endpoints."""

    entries: dict[str, tuple[str, Any]] = field(default_factory=dict)

    def register(self, service: str, endpoint: str, peer: Any) -> None:
        """Register (or replace) ``service``'s endpoint, owned by ``peer``."""
        self.entries[service] = (endpoint, peer)

    def deregister(self, service: str) -> None:
        """Drop a single ``service`` entry."""
        self.entries.pop(service, None)

    def evict(self, peer: Any) -> None:
        """Remove all endpoints owned by ``peer`` (membership loss)."""
        self.entries = {
            service: (endpoint, owner)
            for service, (endpoint, owner) in self.entries.items()
            if owner != peer
        }

    def resolve(self, service: str) -> str | None:
        """Look up ``service``'s endpoint without changing the map."""
        entry = self.entries.get(service)
        return entry[0] if entry is not None else None

    def discovery(self) -> dict[str, str]:
        """The live ``service → endpoint`` map (a fresh snapshot)."""
        return {
            service: endpoint for service, (endpoint, _owner) in self.entries.items()
        }


class DiscoveryCell:
    """Reactive service discovery. The ``discovery`` reader invalidates only when
    the live ``service → endpoint`` map changes; :meth:`resolve` reads without
    changing the map."""

    __slots__ = ("_core", "_discovery")

    def __init__(self, ctx: dict) -> None:
        self._core = DiscoveryCore()
        self._discovery: Cell[dict[str, str]] = Cell(ctx, {})

    def _refresh(self) -> None:
        # A fresh dict snapshot so the cell's ``!=`` guard compares by value
        # (order-independent) rather than by identity.
        self._discovery.value = self._core.discovery()

    def register(self, service: str, endpoint: str, peer: Any) -> None:
        """Register ``service`` at ``endpoint`` owned by ``peer``, then
        reproject the map."""
        self._core.register(service, endpoint, peer)
        self._refresh()

    def deregister(self, service: str) -> None:
        """Deregister ``service``, then reproject the map."""
        self._core.deregister(service)
        self._refresh()

    def evict(self, peer: Any) -> None:
        """Remove all endpoints owned by ``peer``, then reproject the map."""
        self._core.evict(peer)
        self._refresh()

    def resolve(self, service: str) -> str | None:
        """Resolve ``service``'s endpoint. Non-reactive: reads the core directly
        and never invalidates the ``discovery`` reader."""
        return self._core.resolve(service)

    def discovery(self, ctx: Any = None) -> dict[str, str]:
        """Reactive read of the live ``service → endpoint`` map. Invalidated only
        on a change. Pass the caller's compute view (``ctx``) to value-thread the
        edge; omit for an untracked read (``#lzcellkernel``)."""
        if ctx is None:
            return self._discovery.value
        return ctx.read(self._discovery)

    def discovery_cell(self) -> Cell[dict[str, str]]:
        """Handle to the underlying ``discovery`` cell (advanced wiring)."""
        return self._discovery


# ===========================================================================
# Service registry (durable)
# ===========================================================================


@dataclass(frozen=True, slots=True)
class RegistryOp:
    """A durable registry op (the ordered log entry). ``kind`` is ``"register"``
    or ``"deregister"``; ``endpoint`` is ``None`` for a deregister."""

    kind: str
    service: str
    endpoint: str | None = None


@dataclass(slots=True)
class ServiceRegistryCore:
    """Durable service-registry core: an ordered log (the ``DurableOutbox``
    pattern) whose left-fold is the projection, so replay reconstructs it."""

    log: list[RegistryOp] = field(default_factory=list)
    projection: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def _apply(projection: dict[str, str], op: RegistryOp) -> None:
        if op.kind == "register":
            assert op.endpoint is not None
            projection[op.service] = op.endpoint
        elif op.kind == "deregister":
            projection.pop(op.service, None)
        else:  # pragma: no cover - guard
            raise AssertionError(f"unknown registry op kind: {op.kind}")

    def register(self, service: str, endpoint: str) -> None:
        """Append a register op and fold it into the projection."""
        op = RegistryOp("register", service, endpoint)
        self._apply(self.projection, op)
        self.log.append(op)

    def deregister(self, service: str) -> None:
        """Append a deregister op and fold it into the projection."""
        op = RegistryOp("deregister", service)
        self._apply(self.projection, op)
        self.log.append(op)

    def replay(self) -> None:
        """Rebuild the projection from the durable log (restart / crash-replay).
        The left-fold of the log is unchanged, so the projection survives."""
        projection: dict[str, str] = {}
        for op in self.log:
            self._apply(projection, op)
        self.projection = projection

    def snapshot(self) -> dict[str, str]:
        """A fresh copy of the current projection."""
        return dict(self.projection)


class ServiceRegistry:
    """Reactive durable service registry. The ``projection`` reader invalidates
    only when the projected ``service → endpoint`` table changes; :meth:`replay`
    rebuilds an identical projection and therefore does not invalidate."""

    __slots__ = ("_core", "_projection")

    def __init__(self, ctx: dict) -> None:
        self._core = ServiceRegistryCore()
        self._projection: Cell[dict[str, str]] = Cell(ctx, {})

    def _refresh(self) -> None:
        self._projection.value = self._core.snapshot()

    def register(self, service: str, endpoint: str) -> None:
        """Register ``service`` at ``endpoint`` (durable), then reproject."""
        self._core.register(service, endpoint)
        self._refresh()

    def deregister(self, service: str) -> None:
        """Deregister ``service`` (durable), then reproject."""
        self._core.deregister(service)
        self._refresh()

    def replay(self) -> None:
        """Rebuild the projection from the durable log, then reproject. The
        rebuilt projection is identical, so the reader stays cached."""
        self._core.replay()
        self._refresh()

    def projection(self, ctx: Any = None) -> dict[str, str]:
        """Reactive read of the live registry projection. Invalidated only on a
        change. Pass the caller's compute view (``ctx``) to value-thread the
        edge; omit for an untracked read (``#lzcellkernel``)."""
        if ctx is None:
            return self._projection.value
        return ctx.read(self._projection)

    def projection_cell(self) -> Cell[dict[str, str]]:
        """Handle to the underlying ``projection`` cell (advanced wiring)."""
        return self._projection

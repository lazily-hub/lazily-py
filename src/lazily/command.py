"""Command / RPC message plane — ``command-plane-v1`` (#lzcmdplane).

The Python port of ``lazily-rs::command`` (and a structural twin of the Kotlin
/ JS bindings). This is an **additive sibling** to the ``Snapshot`` / ``Delta``
/ ``CrdtSync`` state plane: command frames ride the same transports and reflect
into the normal state graph, but carry command traffic, not cell state.

The four frames are :class:`CommandSubmit`, :class:`CommandCancel`,
:class:`CommandEvents`, and :class:`CommandProjection`. lazily owns the
envelope; the **namespace owns the payload** — :class:`~lazily.ipc.IpcValue`
is never decoded here.

The single hard rule: **terminal authority is the causal receipt.** A command
is terminal only when a terminal :class:`~lazily.ipc.CausalReceipt` for its
``command_id`` folds into the projection (``applied``, or ``rejected`` —
including the ``cancelled`` / ``superseded`` / ``timed_out`` reasons).
``observed`` / ``accepted`` / ``started`` events are non-terminal progress; a
transport ACK is never terminal. RPC (``call`` / ``submit`` / ``cancel``) is a
**derived facade** over the pure :class:`CommandProjection` reducer.

The conformance fixtures in ``lazily-spec/conformance/message-passing`` are the
cross-binding test contract; the Lean model
``LazilyFormal.Command`` pins the negative properties (progress is not proof,
cancel cannot override applied, terminal conflict fails closed).
"""

from __future__ import annotations


__all__ = [
    "COMMAND_PLANE_FEATURE",
    "CallState",
    "CallStateKind",
    "CommandApplyStatus",
    "CommandCancel",
    "CommandEvent",
    "CommandEventKind",
    "CommandEvents",
    "CommandMessage",
    "CommandPolicy",
    "CommandProjection",
    "CommandProjectionEntry",
    "CommandProjectionImage",
    "CommandRpcClient",
    "CommandStatus",
    "CommandSubmit",
    "CommandTransport",
    "DedupePolicy",
    "StaleGeneration",
    "TerminalConflict",
    "applied_receipt",
    "rejected_receipt",
]


from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from .ipc import CausalReceipt, IpcValue, ReceiptOutcome


# The capability-negotiation feature token. A peer that lacks it fails closed
# before accepting command traffic; a command that requires the plane is never
# silently downgraded.
COMMAND_PLANE_FEATURE = "command-plane-v1"


class DedupePolicy(Enum):
    """How the admitter collapses concurrent/duplicate submits."""

    NONE = "none"
    SAME_IDEMPOTENCY_KEY = "same_idempotency_key"
    SAME_COMMAND_ID = "same_command_id"

    @classmethod
    def from_wire(cls, value: str) -> DedupePolicy:
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(
                f"unknown dedupe policy: {value!r} "
                "(expected none/same_idempotency_key/same_command_id)"
            ) from exc


@dataclass(frozen=True, slots=True)
class CommandPolicy:
    """Per-command admission policy."""

    dedupe: DedupePolicy
    supersede: bool
    cancel_on_preempt: bool

    def to_wire(self) -> dict[str, Any]:
        return {
            "dedupe": self.dedupe.value,
            "supersede": self.supersede,
            "cancel_on_preempt": self.cancel_on_preempt,
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CommandPolicy:
        return cls(
            dedupe=DedupePolicy.from_wire(d["dedupe"]),
            supersede=bool(d["supersede"]),
            cancel_on_preempt=bool(d["cancel_on_preempt"]),
        )


@dataclass(frozen=True, slots=True)
class CommandSubmit:
    """Submit a command — the request envelope plus a domain payload.

    ``command_id`` is the stable, replay-safe id (the correlation key and
    dedupe/reconnect key). ``causation_id`` is the causal parent (self-caused
    == ``command_id``). ``payload`` is an :class:`~lazily.ipc.IpcValue` lazily
    never interprets; ``required_features`` gates the target.
    """

    command_id: str
    causation_id: str
    source: str
    target: str
    namespace: str
    name: str
    authority_generation: int
    idempotency_key: str
    deadline_ms: int
    policy: CommandPolicy
    payload_type: str
    payload_hash: str
    payload: IpcValue
    required_features: list[str] = field(default_factory=list)

    def to_wire(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "causation_id": self.causation_id,
            "source": self.source,
            "target": self.target,
            "namespace": self.namespace,
            "name": self.name,
            "authority_generation": self.authority_generation,
            "idempotency_key": self.idempotency_key,
            "deadline_ms": self.deadline_ms,
            "policy": self.policy.to_wire(),
            "payload_type": self.payload_type,
            "payload_hash": self.payload_hash,
            "payload": self.payload.to_wire(),
            "required_features": list(self.required_features),
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CommandSubmit:
        return cls(
            command_id=d["command_id"],
            causation_id=d["causation_id"],
            source=d["source"],
            target=d["target"],
            namespace=d["namespace"],
            name=d["name"],
            authority_generation=int(d["authority_generation"]),
            idempotency_key=d["idempotency_key"],
            deadline_ms=int(d["deadline_ms"]),
            policy=CommandPolicy.from_wire(d["policy"]),
            payload_type=d["payload_type"],
            payload_hash=d["payload_hash"],
            payload=IpcValue.from_wire(d["payload"]),
            required_features=list(d.get("required_features", [])),
        )


@dataclass(frozen=True, slots=True)
class CommandCancel:
    """Preempt a still-non-terminal command by ``command_id``.

    Non-terminal by itself; the terminal outcome folds through the matching
    rejected receipt. A cancel after a terminal outcome is recorded but
    changes nothing.
    """

    command_id: str
    causation_id: str
    source: str
    authority_generation: int
    reason: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "causation_id": self.causation_id,
            "source": self.source,
            "authority_generation": self.authority_generation,
            "reason": self.reason,
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CommandCancel:
        return cls(
            command_id=d["command_id"],
            causation_id=d["causation_id"],
            source=d["source"],
            authority_generation=int(d["authority_generation"]),
            reason=d.get("reason"),
        )


class CommandEventKind(Enum):
    """Progress/detail event kinds — UX/diagnostics only, NEVER terminal proof.

    ``cancelled`` / ``superseded`` / ``timed_out`` surface here for UX but
    their terminal authority is a matching rejected receipt.
    """

    OBSERVED = "observed"
    ACCEPTED = "accepted"
    STARTED = "started"
    PROGRESS = "progress"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    TIMED_OUT = "timed_out"

    @classmethod
    def from_wire(cls, value: str) -> CommandEventKind:
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(f"unknown command event kind: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class CommandEvent:
    """One progress/detail event for a command."""

    event_id: str
    command_id: str
    kind: CommandEventKind
    generation: int
    detail: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "command_id": self.command_id,
            "kind": self.kind.value,
            "generation": self.generation,
            "detail": self.detail,
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CommandEvent:
        return cls(
            event_id=d["event_id"],
            command_id=d["command_id"],
            kind=CommandEventKind.from_wire(d["kind"]),
            generation=int(d["generation"]),
            detail=d.get("detail"),
        )


@dataclass(frozen=True, slots=True)
class CommandEvents:
    """A batch of :class:`CommandEvent` frames."""

    events: list[CommandEvent] = field(default_factory=list)

    def to_wire(self) -> dict[str, Any]:
        return {"events": [e.to_wire() for e in self.events]}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CommandEvents:
        return cls(events=[CommandEvent.from_wire(e) for e in d.get("events", [])])


class CommandStatus(Enum):
    """Folded projection status.

    ``SUBMITTED`` / ``ACCEPTED`` / ``RUNNING`` are non-terminal;
    ``APPLIED`` / ``REJECTED`` / ``CANCELLED`` / ``SUPERSEDED`` / ``TIMED_OUT``
    are terminal and backed by a terminal :class:`~lazily.ipc.CausalReceipt`.
    """

    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    RUNNING = "running"
    APPLIED = "applied"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        return self in (
            CommandStatus.APPLIED,
            CommandStatus.REJECTED,
            CommandStatus.CANCELLED,
            CommandStatus.SUPERSEDED,
            CommandStatus.TIMED_OUT,
        )

    @classmethod
    def from_wire(cls, value: str) -> CommandStatus:
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(f"unknown command status: {value!r}") from exc


@dataclass(slots=True)
class CommandProjectionEntry:
    """One command's folded state in the projection."""

    command_id: str
    status: CommandStatus
    terminal: bool
    generation: int
    reason: str | None = None
    terminal_receipt_id: str | None = None
    last_event_id: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "status": self.status.value,
            "terminal": self.terminal,
            "generation": self.generation,
            "reason": self.reason,
            "terminal_receipt_id": self.terminal_receipt_id,
            "last_event_id": self.last_event_id,
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CommandProjectionEntry:
        return cls(
            command_id=d["command_id"],
            status=CommandStatus.from_wire(d["status"]),
            terminal=bool(d["terminal"]),
            generation=int(d["generation"]),
            reason=d.get("reason"),
            terminal_receipt_id=d.get("terminal_receipt_id"),
            last_event_id=d.get("last_event_id"),
        )

    def copy(self) -> CommandProjectionEntry:
        return CommandProjectionEntry(
            command_id=self.command_id,
            status=self.status,
            terminal=self.terminal,
            generation=self.generation,
            reason=self.reason,
            terminal_receipt_id=self.terminal_receipt_id,
            last_event_id=self.last_event_id,
        )


@dataclass(frozen=True, slots=True)
class CommandProjectionImage:
    """Queryable projection snapshot — also the reconnect resync frame."""

    generation: int
    commands: list[CommandProjectionEntry]

    def to_wire(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "commands": [c.to_wire() for c in self.commands],
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CommandProjectionImage:
        return cls(
            generation=int(d["generation"]),
            commands=[
                CommandProjectionEntry.from_wire(c) for c in d.get("commands", [])
            ],
        )


# ---------------------------------------------------------------------------
# The wire message (externally tagged, sibling to IpcMessage/ReceiptMessage)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandMessage:
    """One command-plane wire frame — an externally-tagged sibling family.

    Serialized as a single-key object whose key is the variant
    (``CommandSubmit`` / ``CommandCancel`` / ``CommandEvents`` /
    ``CommandProjection``). Not an :class:`~lazily.ipc.IpcMessage` variant.
    """

    submit: CommandSubmit | None = None
    cancel: CommandCancel | None = None
    events: CommandEvents | None = None
    projection: CommandProjectionImage | None = None

    @classmethod
    def of_submit(cls, submit: CommandSubmit) -> CommandMessage:
        return cls(submit=submit)

    @classmethod
    def of_cancel(cls, cancel: CommandCancel) -> CommandMessage:
        return cls(cancel=cancel)

    @classmethod
    def of_events(cls, events: CommandEvents) -> CommandMessage:
        return cls(events=events)

    @classmethod
    def of_projection(cls, image: CommandProjectionImage) -> CommandMessage:
        return cls(projection=image)

    def to_wire(self) -> dict[str, Any]:
        if self.submit is not None:
            return {"CommandSubmit": self.submit.to_wire()}
        if self.cancel is not None:
            return {"CommandCancel": self.cancel.to_wire()}
        if self.events is not None:
            return {"CommandEvents": self.events.to_wire()}
        if self.projection is not None:
            return {"CommandProjection": self.projection.to_wire()}
        raise ValueError("empty CommandMessage")

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CommandMessage:
        if not (isinstance(d, dict) and len(d) == 1):
            raise ValueError(f"malformed CommandMessage wire value: {d!r}")
        tag, body = next(iter(d.items()))
        if tag == "CommandSubmit":
            return cls(submit=CommandSubmit.from_wire(body))
        if tag == "CommandCancel":
            return cls(cancel=CommandCancel.from_wire(body))
        if tag == "CommandEvents":
            return cls(events=CommandEvents.from_wire(body))
        if tag == "CommandProjection":
            return cls(projection=CommandProjectionImage.from_wire(body))
        raise ValueError(f"unknown CommandMessage variant: {tag!r}")


# ---------------------------------------------------------------------------
# Apply status (fold result)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StaleGeneration:
    """An event/receipt/cancel outside the command's current generation."""

    expected: int
    actual: int


@dataclass(frozen=True, slots=True)
class TerminalConflict:
    """A second terminal receipt with a different outcome — fail closed."""

    command_id: str
    existing: CommandStatus
    incoming: CommandStatus


class CommandApplyStatus(Enum):
    """Result of folding one frame into the projection.

    Only ``RECORDED`` mutates the authoritative projection; the other variants
    are no-ops on the terminal state. ``STALE_GENERATION`` and
    ``TERMINAL_CONFLICT`` carry the detail via the companion accessors below.
    """

    RECORDED = "recorded"
    DUPLICATE = "duplicate"
    UNKNOWN = "unknown"
    STALE_GENERATION = "stale_generation"
    TERMINAL_CONFLICT = "terminal_conflict"


# ---------------------------------------------------------------------------
# Status helpers (mirror the Rust free functions)
# ---------------------------------------------------------------------------


def _terminal_status_of(outcome: ReceiptOutcome, reason: str | None) -> CommandStatus:
    if outcome is ReceiptOutcome.APPLIED:
        return CommandStatus.APPLIED
    if outcome is ReceiptOutcome.REJECTED:
        if reason == "cancelled":
            return CommandStatus.CANCELLED
        if reason == "superseded":
            return CommandStatus.SUPERSEDED
        if reason == "timed_out":
            return CommandStatus.TIMED_OUT
        return CommandStatus.REJECTED
    # Non-terminal outcomes never reach here (guarded by is_terminal).
    return CommandStatus.ACCEPTED


def _progress_status_of(kind: CommandEventKind) -> CommandStatus | None:
    if kind in (CommandEventKind.OBSERVED, CommandEventKind.ACCEPTED):
        return CommandStatus.ACCEPTED
    if kind in (CommandEventKind.STARTED, CommandEventKind.PROGRESS):
        return CommandStatus.RUNNING
    # cancelled/superseded/timed_out events are UX only; the status change
    # waits for the terminal receipt.
    return None


def _phase_rank(status: CommandStatus) -> int:
    if status is CommandStatus.SUBMITTED:
        return 0
    if status is CommandStatus.ACCEPTED:
        return 1
    if status is CommandStatus.RUNNING:
        return 2
    return 3  # terminal


# ---------------------------------------------------------------------------
# The reducer
# ---------------------------------------------------------------------------


class CommandProjection:
    """The folded command projection reducer — pure, transport-agnostic.

    Folds :class:`CommandMessage` frames and :class:`~lazily.ipc.CausalReceipt`
    terminal authority into a queryable projection. Rules
    (``lazily-spec/protocol.md § Command / RPC Message Plane``):

    * **Terminal authority is the receipt** — progress events and transport
      ACKs never complete a command.
    * **Generation guards** — events/receipts outside the command's current
      authority generation are ignored (audit data only).
    * **Idempotency** — replaying a submit/event/receipt/cancel with a known
      id is a no-op.
    * **Cancel before terminal only** — a cancel after ``applied`` is ignored.
    * **Terminal conflict fails closed** — ``applied`` vs ``rejected`` at the
      same generation is not resolved by winner selection.
    * **Reconnect equivalence** — folding a ``CommandProjection`` image equals
      folding the events and receipts it summarizes.
    """

    __slots__ = (
        "_conflicts",
        "_entries",
        "_generation",
        "_seen_cancel_ids",
        "_seen_event_ids",
        "_seen_receipt_ids",
        "last_conflict",
        "last_stale",
    )

    def __init__(self) -> None:
        self._generation: int = 0
        self._entries: dict[str, CommandProjectionEntry] = {}
        self._seen_event_ids: set[str] = set()
        self._seen_receipt_ids: set[str] = set()
        self._seen_cancel_ids: set[str] = set()
        self._conflicts: set[str] = set()
        # Detail carried by the most recent STALE_GENERATION / TERMINAL_CONFLICT
        # fold, so callers can inspect why a frame was a no-op.
        self.last_stale: StaleGeneration | None = None
        self.last_conflict: TerminalConflict | None = None

    @property
    def generation(self) -> int:
        """The current authority generation the projection has folded to."""
        return self._generation

    def apply_message(self, message: CommandMessage) -> CommandApplyStatus:
        """Fold one command-plane message, dispatching on the variant."""
        if message.submit is not None:
            return self.submit(message.submit)
        if message.cancel is not None:
            return self.cancel(message.cancel)
        if message.events is not None:
            last = CommandApplyStatus.UNKNOWN
            for event in message.events.events:
                last = self.event(event)
            return last
        if message.projection is not None:
            return self.apply_projection(message.projection)
        return CommandApplyStatus.UNKNOWN

    def submit(self, submit: CommandSubmit) -> CommandApplyStatus:
        if submit.command_id in self._entries:
            return CommandApplyStatus.DUPLICATE
        if submit.authority_generation > self._generation:
            self._generation = submit.authority_generation
        self._entries[submit.command_id] = CommandProjectionEntry(
            command_id=submit.command_id,
            status=CommandStatus.SUBMITTED,
            terminal=False,
            generation=submit.authority_generation,
        )
        return CommandApplyStatus.RECORDED

    def event(self, event: CommandEvent) -> CommandApplyStatus:
        if event.event_id in self._seen_event_ids:
            return CommandApplyStatus.DUPLICATE
        entry = self._entries.get(event.command_id)
        if entry is None:
            return CommandApplyStatus.UNKNOWN
        if event.generation != entry.generation:
            self.last_stale = StaleGeneration(entry.generation, event.generation)
            return CommandApplyStatus.STALE_GENERATION
        self._seen_event_ids.add(event.event_id)
        entry.last_event_id = event.event_id
        nxt = _progress_status_of(event.kind)
        if (
            nxt is not None
            and not entry.terminal
            and _phase_rank(nxt) >= _phase_rank(entry.status)
        ):
            entry.status = nxt
        return CommandApplyStatus.RECORDED

    def cancel(self, cancel: CommandCancel) -> CommandApplyStatus:
        if cancel.causation_id in self._seen_cancel_ids:
            return CommandApplyStatus.DUPLICATE
        entry = self._entries.get(cancel.command_id)
        if entry is None:
            return CommandApplyStatus.UNKNOWN
        if cancel.authority_generation != entry.generation:
            self.last_stale = StaleGeneration(
                entry.generation, cancel.authority_generation
            )
            return CommandApplyStatus.STALE_GENERATION
        self._seen_cancel_ids.add(cancel.causation_id)
        # A cancel after a terminal outcome is ignored (recorded but no change).
        return CommandApplyStatus.RECORDED

    def observe_receipt(self, receipt: CausalReceipt) -> CommandApplyStatus:
        """Fold terminal authority keyed by ``causation_id`` == ``command_id``."""
        if receipt.receipt_id in self._seen_receipt_ids:
            return CommandApplyStatus.DUPLICATE
        entry = self._entries.get(receipt.causation_id)
        if entry is None:
            return CommandApplyStatus.UNKNOWN
        if receipt.generation != entry.generation:
            self.last_stale = StaleGeneration(entry.generation, receipt.generation)
            return CommandApplyStatus.STALE_GENERATION
        if not receipt.outcome.is_terminal:
            # Non-terminal receipt: record id, advance progress, keep non-terminal.
            self._seen_receipt_ids.add(receipt.receipt_id)
            if not entry.terminal and _phase_rank(
                CommandStatus.ACCEPTED
            ) >= _phase_rank(entry.status):
                entry.status = CommandStatus.ACCEPTED
            return CommandApplyStatus.RECORDED
        incoming = _terminal_status_of(receipt.outcome, receipt.reason)
        if entry.terminal:
            if entry.status == incoming:
                self._seen_receipt_ids.add(receipt.receipt_id)
                return CommandApplyStatus.RECORDED
            existing = entry.status
            self._conflicts.add(receipt.causation_id)
            self.last_conflict = TerminalConflict(
                command_id=receipt.causation_id,
                existing=existing,
                incoming=incoming,
            )
            return CommandApplyStatus.TERMINAL_CONFLICT
        self._seen_receipt_ids.add(receipt.receipt_id)
        entry.terminal = True
        entry.status = incoming
        entry.reason = receipt.reason
        entry.terminal_receipt_id = receipt.receipt_id
        return CommandApplyStatus.RECORDED

    def apply_projection(self, image: CommandProjectionImage) -> CommandApplyStatus:
        """Fold a reconnect/handoff resync image (reconnect equivalence)."""
        if image.generation > self._generation:
            self._generation = image.generation
        for entry in image.commands:
            self._entries[entry.command_id] = entry.copy()
            if entry.last_event_id is not None:
                self._seen_event_ids.add(entry.last_event_id)
            if entry.terminal_receipt_id is not None:
                self._seen_receipt_ids.add(entry.terminal_receipt_id)
        return CommandApplyStatus.RECORDED

    # -- queries -------------------------------------------------------- #

    def entry(self, command_id: str) -> CommandProjectionEntry | None:
        return self._entries.get(command_id)

    def terminal_for(self, command_id: str) -> CommandProjectionEntry | None:
        entry = self._entries.get(command_id)
        return entry if (entry is not None and entry.terminal) else None

    def has_conflict(self, command_id: str) -> bool:
        return command_id in self._conflicts

    def to_image(self) -> CommandProjectionImage:
        """Snapshot the projection as a wire image, ordered by command id."""
        return CommandProjectionImage(
            generation=self._generation,
            commands=[self._entries[k].copy() for k in sorted(self._entries)],
        )


# ---------------------------------------------------------------------------
# RPC facade
# ---------------------------------------------------------------------------


class CommandTransport(Protocol):
    """I/O seam — Unix-socket/WebSocket live outside the pure reducer."""

    def send(self, message: CommandMessage) -> None:
        """Send one command-plane frame. Raises on transport failure."""
        ...


class CallStateKind(Enum):
    """Resolution state of an RPC ``call``."""

    PENDING = "pending"
    RESOLVED = "resolved"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class CallState:
    """The resolution state of an RPC ``call``.

    ``PENDING`` until a terminal receipt folds in; ``RESOLVED`` with the
    terminal entry; ``CONFLICT`` when the command failed closed.
    """

    kind: CallStateKind
    entry: CommandProjectionEntry | None = None

    @classmethod
    def pending(cls) -> CallState:
        return cls(kind=CallStateKind.PENDING)

    @classmethod
    def resolved(cls, entry: CommandProjectionEntry) -> CallState:
        return cls(kind=CallStateKind.RESOLVED, entry=entry)

    @classmethod
    def conflict(cls) -> CallState:
        return cls(kind=CallStateKind.CONFLICT)


class CommandRpcClient:
    """RPC facade over the command plane.

    ``submit`` / ``cancel`` build and send frames; incoming frames and receipts
    are folded via :meth:`ingest_command` / :meth:`ingest_receipt`; a unary
    :meth:`poll_call` resolves **only** when the projection reaches a terminal
    outcome. A transport ACK or ``accepted`` / ``started`` event never resolves
    a ``call``.
    """

    __slots__ = ("_projection", "_transport")

    def __init__(self, transport: CommandTransport) -> None:
        self._transport = transport
        self._projection = CommandProjection()

    @property
    def projection(self) -> CommandProjection:
        return self._projection

    def submit(self, submit: CommandSubmit) -> str:
        """Send + locally fold a submit; returns the ``command_id``."""
        self._transport.send(CommandMessage.of_submit(submit))
        self._projection.submit(submit)
        return submit.command_id

    def cancel(self, cancel: CommandCancel) -> None:
        self._transport.send(CommandMessage.of_cancel(cancel))
        self._projection.cancel(cancel)

    def ingest_command(self, message: CommandMessage) -> CommandApplyStatus:
        """Fold an incoming command-plane frame."""
        return self._projection.apply_message(message)

    def ingest_receipt(self, receipt: CausalReceipt) -> CommandApplyStatus:
        """Fold incoming terminal authority."""
        return self._projection.observe_receipt(receipt)

    def poll_call(self, command_id: str) -> CallState:
        """``PENDING`` until a terminal receipt resolves the command."""
        if self._projection.has_conflict(command_id):
            return CallState.conflict()
        terminal = self._projection.terminal_for(command_id)
        if terminal is not None:
            return CallState.resolved(terminal)
        return CallState.pending()


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------


def applied_receipt(
    receipt_id: str,
    command_id: str,
    observer: str,
    generation: int,
) -> CausalReceipt:
    """A terminal ``applied`` receipt keyed by a command id."""
    return CausalReceipt(
        receipt_id=receipt_id,
        causation_id=command_id,
        observer=observer,
        generation=generation,
        outcome=ReceiptOutcome.APPLIED,
    )


def rejected_receipt(
    receipt_id: str,
    command_id: str,
    observer: str,
    generation: int,
    reason: str,
) -> CausalReceipt:
    """A terminal ``rejected`` receipt keyed by a command id, with reason."""
    return CausalReceipt(
        receipt_id=receipt_id,
        causation_id=command_id,
        observer=observer,
        generation=generation,
        outcome=ReceiptOutcome.REJECTED,
        reason=reason,
    )

"""Wire scalars and descriptors shared by the generated IPC wire types.

``NodeKey`` and ``ShmBlobRef`` keep hand-written codecs: each carries a rule
beyond its schema (``NodeKey``'s byte and segment bounds, ``ShmBlobRef``'s
omit-when-default ``backend`` and its null-as-absent leniency,
``#lzblobbackendstrict``). They live outside :mod:`lazily.ipc` so the
generated :mod:`lazily._delta_wire_gen` can import them without a cycle;
:mod:`lazily.ipc` re-exports every name.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Iterable


# ---------------------------------------------------------------------------
# NodeKey (optional wire-stable keyed address)
# ---------------------------------------------------------------------------


#: Maximum encoded byte length of a :class:`NodeKey` path.
NODE_KEY_MAX_LEN = 1024
#: Maximum number of ``/``-separated segments in a :class:`NodeKey`.
NODE_KEY_MAX_SEGMENTS = 32


class NodeKeyError(ValueError):
    """Why a :class:`NodeKey` path failed validation.

    Mirrors the ``lazily-rs`` ``NodeKeyError`` enum: a ``kind`` discriminator
    (one of the class constants below) plus context describing the offending
    path. Bounds are checked on construction and on the wire.
    """

    #: The path was empty.
    EMPTY = "empty"
    #: The path exceeded :data:`NODE_KEY_MAX_LEN` bytes.
    TOO_LONG = "too_long"
    #: The path had more than :data:`NODE_KEY_MAX_SEGMENTS` segments.
    TOO_MANY_SEGMENTS = "too_many_segments"
    #: The path contained an empty segment (leading/trailing/double ``/``).
    EMPTY_SEGMENT = "empty_segment"

    def __init__(
        self,
        kind: str,
        *,
        length: int | None = None,
        segments: int | None = None,
    ) -> None:
        self.kind = kind
        self.length = length
        self.segments = segments
        super().__init__(self._message())

    def _message(self) -> str:
        if self.kind is NodeKeyError.EMPTY:
            return "node key path is empty"
        if self.kind is NodeKeyError.TOO_LONG:
            return f"node key path is {self.length} bytes, exceeds {NODE_KEY_MAX_LEN}"
        if self.kind is NodeKeyError.TOO_MANY_SEGMENTS:
            return (
                f"node key has {self.segments} segments, "
                f"exceeds {NODE_KEY_MAX_SEGMENTS}"
            )
        return "node key path has an empty segment"


@dataclass(frozen=True, slots=True)
class NodeKey:
    """Wire-stable keyed address for a collection entry.

    A ``/``-joined path (e.g. ``scores/alice``, ``outer/k1/inner/k2``). Unlike
    :data:`NodeId` — the volatile internal handle a producer may re-mint after a
    resync or remove-then-readd — a :class:`NodeKey` is producer-defined and
    **stable across NodeId churn**, so a peer can subscribe to "entry
    ``scores/alice``" without an out-of-band key→NodeId map. A multi-segment
    path addresses nested collections with no extra machinery.

    :class:`NodeKey` is **additive**: it never changes :data:`NodeId` semantics.
    It appears only as the optional ``key`` field on :class:`NodeSnapshot` and
    :class:`DeltaOp_NodeAdd`. Length and segment count are bounded
    (:data:`NODE_KEY_MAX_LEN`, :data:`NODE_KEY_MAX_SEGMENTS`) to cap
    attacker-controlled growth; oversized keys are rejected on construction and
    on the wire.

    On the wire :class:`NodeKey` serializes as a bare JSON string, and a missing
    ``key`` field decodes to ``None`` (``null``) so pre-``key`` encoders and the
    existing conformance fixtures round-trip unchanged.
    """

    path: str

    @classmethod
    def new(cls, path: str) -> NodeKey:
        """Construct a validated key from a ``/``-joined path."""
        cls._validate(path)
        return cls(path)

    @classmethod
    def from_segments(cls, segments: Iterable[str]) -> NodeKey:
        """Construct a key from segments joined with ``/`` (then validated)."""
        return cls.new("/".join(segments))

    @staticmethod
    def _validate(path: str) -> None:
        if not path:
            raise NodeKeyError(NodeKeyError.EMPTY)
        byte_len = len(path.encode("utf-8"))
        if byte_len > NODE_KEY_MAX_LEN:
            raise NodeKeyError(NodeKeyError.TOO_LONG, length=byte_len)
        parts = path.split("/")
        if any(segment == "" for segment in parts):
            raise NodeKeyError(NodeKeyError.EMPTY_SEGMENT)
        if len(parts) > NODE_KEY_MAX_SEGMENTS:
            raise NodeKeyError(NodeKeyError.TOO_MANY_SEGMENTS, segments=len(parts))

    def as_str(self) -> str:
        """The full ``/``-joined path."""
        return self.path

    def segments(self) -> list[str]:
        """The path segments."""
        return self.path.split("/")

    def __str__(self) -> str:
        return self.path

    def to_wire(self) -> str:
        """Serialize as a bare JSON string (matches ``serde_str``)."""
        return self.path

    @classmethod
    def from_wire(cls, value: Any) -> NodeKey:
        """Deserialize and validate a bare string path."""
        if not isinstance(value, str):
            raise ValueError(f"NodeKey: expected a string, got {value!r}")
        return cls.new(value)


# ---------------------------------------------------------------------------
# Blob backend discriminator (zero-copy transport)
# ---------------------------------------------------------------------------


class BlobBackendKind(Enum):
    """Which pluggable blob backend holds a :class:`ShmBlobRef` descriptor's bytes.

    The receiver routes descriptor resolution by this discriminator (a ``shm``
    descriptor never resolves in an Arrow table and vice versa). Mirrors the
    ``lazily-rs`` ``BlobBackendKind`` enum and the ``backend`` field of the
    ``ShmBlobRef`` schema (``lazily-spec/schemas/defs.json``,
    ``docs/zero-copy-transport.md``, ``#lzzcpy``).
    """

    #: POSIX shared-memory region (``shm_open`` + ``mmap``) — the default
    #: cross-process backend (same host).
    SHM = "shm"
    #: Apache Arrow IPC stream / Flight-resolved buffer — columnar zero-copy.
    ARROW = "arrow"
    #: An in-process arena (single address space — the FFI host / an editor
    #: plugin loaded in the same process).
    IN_PROCESS = "in_process"

    @classmethod
    def from_wire(cls, value: str) -> BlobBackendKind:
        """Parse a backend discriminator from its wire string.

        **Fails closed, naming the token** (``#lzblobbackendstrict``). An
        ABSENT ``backend`` is the forward-compatibility channel and the only
        one: the field is omit-when-default (see :meth:`is_default`), so every
        descriptor minted before it existed arrives with no token at all and
        :meth:`ShmBlobRef.from_wire` reads that absence as :attr:`SHM`. A
        PRESENT token outside the enum is a different fact. A new backend
        enters the protocol by *adding an enum value* — a spec change carrying
        a fixture (``docs/zero-copy-transport.md`` § Pluggable backends) — so
        an unrecognised token is a corrupt or non-conforming producer, never a
        newer peer.

        Normalizing it to :attr:`SHM` would invert the ``resolve_wrong_backend``
        theorem in that same doc — *a descriptor of one kind never resolves
        against a different backend's table; receivers route by kind* — because
        reading an unknown kind as ``shm`` **is** routing a non-shm descriptor
        into the shm table. That leaves a 64-bit checksum to discharge
        probabilistically what routing was supposed to guarantee structurally,
        and ``shm`` is a backend this build really resolves, so a collision
        returns bytes where a refusal would have been a visible protocol error
        the peer recovers from by resync.

        Two shapes that are not tokens fall out of the same split. An explicit
        ``null`` never reaches here — :meth:`ShmBlobRef.from_wire` reads it as
        the ABSENT form (§ NodeKey, ``#lzkeynullstrict``), because a serde-style
        peer that skipped ``skip_serializing_if`` emits ``null`` where a
        conforming encoder omits, so refusing it would be stricter than the
        reference implementation on a frame the reference implementation
        produces. A present value that is not a string DOES reach here and is
        refused as a :class:`ValueError` — the same family the unknown token
        raises, and the one every caller already guards a decode with, so one
        ``except`` handles both. A refusal raised outside that family still
        rejects the frame but rejects it PAST the handler.

        Replayed by ``codec/blob_backend_discriminator.json``; pinned by
        ``tests/test_library_leniency.py``.
        """
        try:
            return cls(value)
        except ValueError:
            raise ValueError(f"unknown blob backend: {value!r}") from None

    def is_default(self) -> bool:
        """Whether this is the default backend (:attr:`SHM`).

        Used to omit the field on the wire so legacy descriptors round-trip.
        """
        return self is BlobBackendKind.SHM


# ---------------------------------------------------------------------------
# Shared-memory blob descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShmBlobRef:
    """Descriptor for a payload stored in a blob backend (zero-copy transport).

    The standard fields locate and integrity-check a byte range within the
    backend's resolved buffer; :attr:`backend` selects which pluggable backend
    resolves it. ``backend`` is optional and defaults to
    :attr:`BlobBackendKind.SHM`, so every legacy descriptor validates unchanged —
    the transport is a strict superset of the pre-existing shared-memory blob
    path (see ``docs/zero-copy-transport.md``, ``#lzzcpy``).

    The arena header itself is backend-agnostic and does not store ``backend`` —
    the discriminator is wire-level routing, not arena storage.
    """

    offset: int
    len: int
    generation: int
    epoch: int
    checksum: int
    backend: BlobBackendKind = BlobBackendKind.SHM

    def to_wire(self) -> dict[str, int | str]:
        wire: dict[str, int | str] = {
            "offset": self.offset,
            "len": self.len,
            "generation": self.generation,
            "epoch": self.epoch,
            "checksum": self.checksum,
        }
        # Omit the default backend so legacy descriptors and pre-`backend`
        # conformance fixtures round-trip byte-for-byte.
        if not self.backend.is_default():
            wire["backend"] = self.backend.value
        return wire

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> ShmBlobRef:
        # Absence is lenient and presence is strict (``#lzblobbackendstrict``).
        # A missing `backend` is a pre-field descriptor and reads as SHM; a
        # present token outside the enum raises, naming it — see
        # :meth:`BlobBackendKind.from_wire`.
        raw_backend = d.get("backend")
        backend = (
            BlobBackendKind.from_wire(raw_backend)
            if raw_backend is not None
            else BlobBackendKind.SHM
        )
        return cls(
            offset=d["offset"],
            len=d["len"],
            generation=d["generation"],
            epoch=d["epoch"],
            checksum=d["checksum"],
            backend=backend,
        )

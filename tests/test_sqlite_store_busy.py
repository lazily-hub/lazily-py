"""SQLite outbox store under write contention (``#lazilysqlitestoreopen``).

Parity with lazily-rs ``29fde5a`` and the contract in
``lazily-spec/sqlite-stores.json`` / ``docs/durable-outbox.md``:

1. :class:`~lazily.SqliteStore` opens its connection with an EXPLICIT busy
   timeout, :data:`~lazily.SQLITE_STORE_BUSY_TIMEOUT` (30 s), instead of
   ``sqlite3.connect``'s implicit 5 s;
2. a second writer on a database whose write lock another connection holds
   WAITS for that lock and then lands, in both rollback-journal and WAL mode;
3. no connection in ``src/lazily`` ever opens a DEFERRED transaction: a
   deferred read-then-write upgrade is refused with an immediate
   ``database is locked`` that no busy timeout waits out.
"""

from __future__ import annotations

import ast
import json
import queue
import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import lazily
from lazily import (
    SQLITE_STORE_BUSY_TIMEOUT,
    Delta,
    IpcMessage,
    SqliteOutbox,
    SqliteStore,
)


_SRC = Path(lazily.__file__).resolve().parent

#: The spec registry that pins the cross-binding SQLite store contract. Not a
#: conformance fixture (no corpus override moves it), so it is resolved from the
#: canonical sibling directly. CI asserts the sibling is present.
_SQLITE_STORES = (
    Path(__file__).resolve().parents[2] / "lazily-spec" / "sqlite-stores.json"
)

#: How long the second writer's append must stay blocked while the first writer
#: holds the lock. Without a busy handler the append fails in microseconds, so
#: this only bounds how late a descheduled writer thread may reach SQLite before
#: a defective build could pass; it never decides a correct build's outcome
#: (that build waits up to the full :data:`SQLITE_STORE_BUSY_TIMEOUT`).
_STILL_BLOCKED_WINDOW = 0.5

#: Upper bound on the writer thread's lifetime, independent of the store's own
#: timeout so the test still runs (and reddens) when that timeout is zero.
_THREAD_BUDGET = 60.0


def _frame(epoch: int) -> IpcMessage:
    return IpcMessage.of_delta(Delta.new(epoch - 1, epoch, []))


def _busy_timeout_ms(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA busy_timeout").fetchone()[0])


def test_store_installs_the_busy_timeout(tmp_path: Path) -> None:
    """``sqlite3.connect`` defaults to 5 s; the store must install 30 s."""
    store = SqliteStore(tmp_path / "busy.sqlite3", "doc")
    try:
        assert _busy_timeout_ms(store._connection) == int(
            SQLITE_STORE_BUSY_TIMEOUT * 1000
        )
        # Autocommit: the sqlite3 module never opens an implicit transaction.
        assert store._connection.isolation_level is None
        store.put(1, b"frame")
        store.save_cursor(1)
        assert not store._connection.in_transaction
    finally:
        store.close()


def test_busy_timeout_matches_the_spec_registry() -> None:
    """The value is the cross-binding contract, not a local choice."""
    if not _SQLITE_STORES.parent.is_dir():
        pytest.skip(f"no lazily-spec sibling at {_SQLITE_STORES.parent}")
    # A present sibling without the registry is a failure, not a skip.
    registry = json.loads(_SQLITE_STORES.read_text())
    assert registry["contract"]["busy_timeout_ms"] == SQLITE_STORE_BUSY_TIMEOUT * 1000
    (entry,) = [b for b in registry["bindings"] if b["binding"] == "py"]
    store = entry["sqlite"]
    assert store["busy_timeout"]["ms"] == registry["contract"]["busy_timeout_ms"]
    declared = {*store["two_writer_tests"]["names"], store["busy_timeout"]["test"]}
    here = {
        node.name
        for node in ast.parse(Path(__file__).read_text()).body
        if isinstance(node, ast.FunctionDef)
    }
    assert declared <= here, (
        f"registry names tests this module lacks: {declared - here}"
    )


def _assert_append_waits_for_a_concurrent_writer(
    tmp_path: Path, journal_mode: str
) -> None:
    """Two writers on one database. The first holds the write lock
    (``BEGIN IMMEDIATE`` plus an uncommitted append); the second appends
    meanwhile. Without a busy handler the second append fails at once with
    ``database is locked`` and the epoch is lost. It must instead wait for the
    first writer to commit and then land.
    """
    path = tmp_path / "two-writers.sqlite3"
    SqliteStore(path, "other").close()  # schema exists before the lock is taken

    first = sqlite3.connect(path, isolation_level=None)
    try:
        mode = first.execute(f"PRAGMA journal_mode = {journal_mode}").fetchone()[0]
        assert mode.lower() == journal_mode
        first.execute("BEGIN IMMEDIATE")
        first.execute(
            "INSERT INTO reliable_sync_outbox (document_hash, epoch, frame_json) "
            "VALUES (?, ?, ?)",
            ("other", 1, _frame(1).encode_json()),
        )

        reached_append: queue.Queue[None] = queue.Queue()
        outcome: queue.Queue[tuple[str, object]] = queue.Queue()

        def second_writer() -> None:
            outbox = SqliteOutbox(path, "doc")
            try:
                reached_append.put(None)
                try:
                    outbox.append(1, _frame(1))
                except sqlite3.OperationalError as exc:
                    outcome.put(("error", str(exc)))
                    return
                outcome.put(("retained", outbox.retained_epochs()))
            finally:
                outbox.close()

        second = threading.Thread(target=second_writer)
        second.start()
        reached_append.get(timeout=_THREAD_BUDGET)
        try:
            returned_under_lock = outcome.get(timeout=_STILL_BLOCKED_WINDOW)
        except queue.Empty:
            returned_under_lock = None
        first.execute("COMMIT")
        second.join(timeout=_THREAD_BUDGET)
        assert not second.is_alive()
    finally:
        first.close()

    assert returned_under_lock is None, (
        f"[{journal_mode}] the append returned while another writer held the "
        f"lock instead of waiting for it: {returned_under_lock!r}"
    )
    assert outcome.get_nowait() == ("retained", [1]), (
        f"[{journal_mode}] the waited append is durable once the first writer commits"
    )
    reopened = SqliteOutbox(path, "other")
    try:
        assert reopened.retained_epochs() == [1], (
            f"[{journal_mode}] the first writer's committed append is intact"
        )
    finally:
        reopened.close()


def test_an_append_waits_for_a_concurrent_writer_rollback_journal(
    tmp_path: Path,
) -> None:
    _assert_append_waits_for_a_concurrent_writer(tmp_path, "delete")


def test_an_append_waits_for_a_concurrent_writer_wal(tmp_path: Path) -> None:
    _assert_append_waits_for_a_concurrent_writer(tmp_path, "wal")


def test_a_deferred_read_then_write_is_refused_without_waiting(
    tmp_path: Path,
) -> None:
    """Why the deferred-transaction ban exists: the busy timeout does NOT help.

    A DEFERRED transaction that has already read holds a read snapshot; its
    first write must upgrade it, and SQLite refuses that upgrade with an
    immediate ``database is locked`` while another connection holds the write
    lock -- the busy handler is never consulted. ``BEGIN IMMEDIATE`` takes the
    write lock up front, from no open transaction, where the handler does run.
    """
    path = tmp_path / "deferred.sqlite3"
    SqliteStore(path, "doc").close()
    holder = sqlite3.connect(path, isolation_level=None)
    deferred = sqlite3.connect(
        path, timeout=SQLITE_STORE_BUSY_TIMEOUT, isolation_level=None
    )
    try:
        deferred.execute("BEGIN DEFERRED")
        deferred.execute("SELECT COUNT(*) FROM reliable_sync_outbox").fetchone()
        holder.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            deferred.execute(
                "INSERT INTO reliable_sync_outbox (document_hash, epoch, frame_json) "
                "VALUES ('doc', 1, x'00')"
            )
        # Refused without waiting out the 30 s budget the connection carries.
        assert time.monotonic() - started < SQLITE_STORE_BUSY_TIMEOUT / 2
    finally:
        holder.close()
        deferred.close()


# ---------------------------------------------------------------------------
# Guard: no deferred transactions anywhere in the package
# ---------------------------------------------------------------------------


def _sqlite_violations(source: str, filename: str) -> list[str]:
    """Every way ``source`` could open a DEFERRED SQLite transaction.

    * a ``sqlite3.connect(...)`` call that does not pass ``isolation_level=None``
      (the legacy default opens an implicit DEFERRED ``BEGIN`` before every DML
      statement) or does not pass ``timeout=SQLITE_STORE_BUSY_TIMEOUT``;
    * ``autocommit=False`` (3.12+: the module then keeps a DEFERRED transaction
      open at all times);
    * any SQL string literal whose ``BEGIN`` is neither ``IMMEDIATE`` nor
      ``EXCLUSIVE``.
    """
    violations: list[str] = []
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            if name == "connect" and (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "sqlite3"
            ):
                level = keywords.get("isolation_level")
                if not (isinstance(level, ast.Constant) and level.value is None):
                    violations.append(
                        f"{filename}:{node.lineno}: sqlite3.connect without "
                        f"isolation_level=None opens implicit DEFERRED transactions"
                    )
                timeout = keywords.get("timeout")
                if not (
                    isinstance(timeout, ast.Name)
                    and timeout.id == "SQLITE_STORE_BUSY_TIMEOUT"
                ):
                    violations.append(
                        f"{filename}:{node.lineno}: sqlite3.connect without "
                        f"timeout=SQLITE_STORE_BUSY_TIMEOUT"
                    )
            autocommit = keywords.get("autocommit")
            if isinstance(autocommit, ast.Constant) and autocommit.value is False:
                violations.append(f"{filename}:{node.lineno}: autocommit=False")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _deferred_begin(node.value)
        ):
            violations.append(
                f"{filename}:{node.lineno}: a BEGIN that is not IMMEDIATE/EXCLUSIVE: "
                f"{node.value.strip()[:60]!r}"
            )
    return violations


#: A statement-initial ``BEGIN`` that is not ``IMMEDIATE`` / ``EXCLUSIVE``
#: (bare ``BEGIN``, ``BEGIN DEFERRED``, ``BEGIN TRANSACTION`` are all DEFERRED).
_DEFERRED_BEGIN = re.compile(
    r"(?:^|;)\s*BEGIN\b(?!\s+(?:IMMEDIATE|EXCLUSIVE)\b)", re.IGNORECASE
)


def _deferred_begin(sql: str) -> bool:
    return _DEFERRED_BEGIN.search(sql) is not None


def test_sqlite_connections_never_open_deferred_transactions() -> None:
    files = sorted(_SRC.rglob("*.py"))
    assert files, f"guard scanned nothing under {_SRC}"
    connects = 0
    violations: list[str] = []
    for path in files:
        source = path.read_text()
        connects += source.count("sqlite3.connect(")
        violations += _sqlite_violations(source, str(path.relative_to(_SRC)))
    assert connects >= 1, "guard found no sqlite3.connect call to check"
    assert violations == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import sqlite3\nsqlite3.connect('x')\n", 2),
        ("import sqlite3\nsqlite3.connect('x', isolation_level='')\n", 2),
        (
            "import sqlite3\nsqlite3.connect('x', timeout=5.0, isolation_level=None)\n",
            1,
        ),
        (
            "import sqlite3\n"
            "sqlite3.connect('x', timeout=SQLITE_STORE_BUSY_TIMEOUT, "
            "isolation_level=None, autocommit=False)\n",
            1,
        ),
        ("c.execute('BEGIN')\n", 1),
        ("c.execute('BEGIN DEFERRED')\n", 1),
        ("c.execute('begin transaction')\n", 1),
        ("c.execute('BEGIN IMMEDIATE')\n", 0),
        ("c.execute('BEGIN EXCLUSIVE')\n", 0),
        ("c.execute('SELECT 1')\n", 0),
        ("c.executescript('SELECT 1; begin;')\n", 1),
    ],
)
def test_the_deferred_transaction_guard_bites(source: str, expected: int) -> None:
    """The guard is only worth its green if it reddens on each banned shape."""
    assert len(_sqlite_violations(source, "probe.py")) == expected

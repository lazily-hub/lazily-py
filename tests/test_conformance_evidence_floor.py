"""A stamped manifest is not evidence (#lzstampsatisfiesnonempty).

``#lzstalemanifest`` stamped the fixture manifest's first line with the run id,
and ``conformance_manifest`` writes that stamp when it TRUNCATES the file —
before pytest starts. So the smallest file this protocol can leave behind is a
stamp and nothing else: non-empty, carrying the current invocation's id, naming
zero fixtures. Both of the guard's evidence checks are satisfied by it.
``test -s`` is satisfied because a stamp is bytes; the freshness comparison is
satisfied because the id is the right one.

Measured on the guard as it stood before this test: a 24-byte stamp-only
manifest did not pass, but it failed as ``145 problem(s)`` — one
``canonical fixture '<name>' was NOT opened by the suite`` line per unexcused
fixture in the corpus. That is a coverage verdict standing in for an evidence
verdict. It is also correct only by the SHAPE of the corpus: every one of those
145 lines is the same fact — the opened set is empty — restated per fixture and
mislabelled as a coverage regression, and a corpus whose fixtures were all
excused would have produced none of them.

So the guard names the condition once, against RECORDS, before anything reasons
about coverage. These tests drive the real script in a subprocess rather than
re-implementing its logic: the thing under test is a bash file that a different
process runs, and a Python restatement of it would be the second definition this
series keeps deleting.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from conformance_assert import COVERAGE_SCRIPT


REPO_ROOT = Path(__file__).resolve().parents[1]

#: Any id will do: the stamp and the environment have to AGREE, and nothing here
#: depends on the shape of a real one.
_PROBE_RUN_ID = "evidence-floor-probe"


def _run_guard(
    manifest: Path, corpus: Path, **env_overrides: str | None
) -> subprocess.CompletedProcess[str]:
    """Drive the real guard with a scratch manifest and a scratch corpus root.

    The corpus is a scratch directory, never the shared ``lazily-spec``
    checkout: nine bindings read that tree. It only has to EXIST — the guard's
    corpus walk happens after the checks under test, so its contents are not
    part of any assertion here.
    """
    env = dict(os.environ)
    env["LAZILY_CONFORMANCE_MANIFEST"] = str(manifest)
    env["LAZILY_SPEC_CONFORMANCE_DIR"] = str(corpus)
    env["LAZILY_CONFORMANCE_RUN_ID"] = _PROBE_RUN_ID
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        ["bash", str(COVERAGE_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _scratch_corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "conformance"
    corpus.mkdir()
    return corpus


def _stamp_line() -> str:
    from conftest import _RUN_ID_PREFIX

    return f"{_RUN_ID_PREFIX}{_PROBE_RUN_ID}\n"


def test_a_stamp_only_manifest_is_refused_and_named(tmp_path: Path) -> None:
    """The hazard the run-id protocol introduced, driven end to end.

    A pytest that dies after the truncate step — or one that ran with the
    recorder detached — leaves exactly this file.
    """
    manifest = tmp_path / "conformance-fixtures-loaded.txt"
    manifest.write_text(_stamp_line(), encoding="utf-8")
    assert manifest.stat().st_size > 0, (
        "the premise of this test is that a stamp makes the file NON-empty, so "
        "`test -s` can no longer see it. If this fires, the stamp is not being "
        "written and the test below proves nothing."
    )

    result = _run_guard(manifest, _scratch_corpus(tmp_path))

    assert result.returncode != 0, (
        f"the guard ACCEPTED a manifest holding nothing but a run-id stamp, so "
        f"'coverage OK' can be printed over zero recorded reads "
        f"(#lzstampsatisfiesnonempty).\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "NO records" in result.stderr, (
        f"the guard refused, but not AS an evidence problem. The refusal has to "
        f"name the condition — a stamped, record-free manifest — rather than "
        f"reporting every unexcused canonical fixture as uncovered, which is "
        f"the same fact restated per fixture and mislabelled.\n"
        f"stderr:\n{result.stderr}"
    )
    assert str(manifest) in result.stderr, (
        f"the refusal does not name the file it is about, which is the whole of "
        f"the reader's next step.\nstderr:\n{result.stderr}"
    )
    assert "was NOT opened by the suite" not in result.stderr, (
        f"the guard reached the per-fixture coverage loop, so the evidence "
        f"failure is still being reported as a coverage regression.\n"
        f"stderr:\n{result.stderr}"
    )


def test_an_empty_manifest_is_still_refused_as_a_missing_manifest(
    tmp_path: Path,
) -> None:
    """The ordinary empty case keeps its own, different message.

    Zero bytes and stamp-only are different failures with different next steps
    — no recorder attached at all, versus a run that started and did not record
    — so the records floor must not swallow the ``-s`` check it sits below.
    """
    manifest = tmp_path / "conformance-fixtures-loaded.txt"
    manifest.write_text("", encoding="utf-8")

    result = _run_guard(manifest, _scratch_corpus(tmp_path))

    assert result.returncode != 0, f"an empty manifest was accepted:\n{result.stdout}"
    assert "no conformance manifest" in result.stderr, (
        f"an empty manifest no longer reports as a missing manifest.\n"
        f"stderr:\n{result.stderr}"
    )


def test_the_records_floor_does_not_fire_when_records_exist(tmp_path: Path) -> None:
    """The discriminating half: with a record below the stamp, the floor is silent.

    Without this, a floor that failed unconditionally would satisfy both tests
    above while telling the reader nothing. The run still fails here — a scratch
    corpus cannot satisfy the ledger checks — and that is fine: the assertion is
    about WHICH failure, not about reaching green.
    """
    manifest = tmp_path / "conformance-fixtures-loaded.txt"
    manifest.write_text(
        _stamp_line() + "collections/registers_convergence.json\n", encoding="utf-8"
    )

    result = _run_guard(manifest, _scratch_corpus(tmp_path))

    assert "NO records" not in result.stderr, (
        f"the records floor fired on a manifest that HAS a record, so it is not "
        f"counting records — it is failing unconditionally, and the two tests "
        f"above would pass over a guard that rejects everything.\n"
        f"stderr:\n{result.stderr}"
    )


def test_there_is_no_env_flag_that_skips_the_freshness_check(tmp_path: Path) -> None:
    """No blanket opt-out; the escape hatch is naming the id (#lzstalemanifest).

    The guard used to honour ``LAZILY_CONFORMANCE_ALLOW_UNSTAMPED_EVIDENCE=1``,
    which skipped the freshness block entirely and warned. That is a boolean
    someone can set once in a shell profile and never see again, and its whole
    meaning is "do not check the thing this block exists to check". lazily-js's
    shape is adopted instead: to read an existing manifest by hand you ADOPT the
    id it is stamped with, named on the command line. That cannot be set blindly
    — you have to read the id off the file — and it still verifies that a stamp
    is present and that the verdict is about the run you named.

    Asserted behaviourally rather than by grepping for the old variable's name:
    the property is that no environment setting reaches a verdict with the id
    unset, not that one particular spelling is absent from the source.
    """
    manifest = tmp_path / "conformance-fixtures-loaded.txt"
    manifest.write_text(
        _stamp_line() + "collections/registers_convergence.json\n", encoding="utf-8"
    )

    result = _run_guard(
        manifest,
        _scratch_corpus(tmp_path),
        LAZILY_CONFORMANCE_RUN_ID=None,
        LAZILY_CONFORMANCE_ALLOW_UNSTAMPED_EVIDENCE="1",
    )

    assert result.returncode != 0, (
        f"the guard produced a verdict with LAZILY_CONFORMANCE_RUN_ID unset, so "
        f"an env flag still buys a pass over unstamped evidence "
        f"(#lzstalemanifest).\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "LAZILY_CONFORMANCE_RUN_ID is unset" in result.stderr, (
        f"the refusal is not about the unset id.\nstderr:\n{result.stderr}"
    )

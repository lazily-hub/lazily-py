"""The run-id stamp prefix has FOUR live definitions; couple them (#lzstampprefixdrift).

``#lzstalemanifest`` gave the fixture manifest a first line reading
``# lazily-run-id <id>`` so the coverage guard can refuse an earlier run's
evidence. That one string is now spelled in four executable places, in three
languages, across two process boundaries:

1. ``pyproject.toml``'s ``conformance_manifest`` task WRITES it, as a ``printf``
   format, when it truncates the manifest before the suite starts;
2. ``tests/conftest.py``'s :data:`_RUN_ID_PREFIX` both READS it (to tell whether
   a leftover manifest belongs to this run) and re-WRITES it (when it does not);
3. ``scripts/check-conformance-coverage.sh``'s ``STAMP_PREFIX`` READS it, to
   compare the stamped id against the environment's;
4. the same script's ``sed`` expression STRIPS it, so the stamp line does not
   land in the opened-fixture set.

A drift between any two of them fails CLOSED — the guard stops recognising the
stamp and refuses — so nothing is silently green. That is not the problem. The
problem is the DIAGNOSIS: a one-character typo in the ``printf`` format presents
as ``$MANIFEST carries no run-id stamp on its first line``, which reads as stale
or missing evidence and sends the reader looking at the run, the recorder, and
the ordering of the three poe steps. The cause is a character in a string.

So make the coupling machine-checked, and read every side from its REAL
definition. Restating the literal a fifth time here would only add a fifth place
to drift: each value below is parsed out of the file that actually uses it, with
a pattern anchored on the surrounding syntax rather than on the prefix's own
text. ``tests/conftest.py`` is the reference simply because it is the one
definition in a language that can be imported; the assertion is equality across
the set, so which member is named first carries no meaning.

lazily-go's ``TestGuardAndRecorderAgreeOnTheStampPrefix`` is the reference for
this shape, where it is one of four mutations that redden its probe suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conformance_assert import COVERAGE_SCRIPT

#: The one definition that is importable, and therefore the one read as a VALUE
#: rather than parsed as text. pytest has already imported this module as the
#: rootdir conftest, so this binds the same object the recorder itself uses.
from conftest import _RUN_ID_PREFIX


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFTEST = Path(__file__).resolve().parent / "conftest.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _read(path: Path) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:  # pragma: no cover - a missing source file is the bug
        raise AssertionError(
            f"cannot read {path}, which holds one of the four definitions of the "
            f"run-id stamp prefix (#lzstampprefixdrift)."
        ) from exc


#: Each entry: a human name, the file, and a pattern whose single group is the
#: prefix as that file spells it. Every pattern is anchored on the SYNTAX around
#: the literal — an assignment, a `printf` format, a `sed` address — so none of
#: them contains the prefix's own characters. A pattern that had to name the
#: literal to find it would be a fifth copy of the thing under test.
_DEFINITIONS: tuple[tuple[str, Path, str], ...] = (
    (
        "the `conformance_manifest` poe task, which WRITES the stamp at truncate "
        "time (pyproject.toml)",
        PYPROJECT,
        r"""printf '([^%'\n]*)%s\\n' "\$LAZILY_CONFORMANCE_RUN_ID\"""",
    ),
    (
        "`STAMP_PREFIX`, which the coverage guard READS the stamped id with "
        "(scripts/check-conformance-coverage.sh)",
        COVERAGE_SCRIPT,
        r"""^STAMP_PREFIX="([^"\n]*)"$""",
    ),
    (
        "the guard's `sed` address, which STRIPS the stamp out of the opened set "
        "(scripts/check-conformance-coverage.sh)",
        COVERAGE_SCRIPT,
        r"""sed '/\^([^/\n]*)/d'""",
    ),
)


def _parsed_definitions() -> dict[str, str]:
    found: dict[str, str] = {}
    for name, path, pattern in _DEFINITIONS:
        matches = re.findall(pattern, _read(path), re.MULTILINE)
        assert len(matches) == 1, (
            f"expected exactly ONE definition of the run-id stamp prefix at "
            f"{name}; the pattern matched {len(matches)}.\n"
            f"  file:    {path}\n"
            f"  pattern: {pattern}\n"
            f"A count of zero does not mean the definition is gone — it means "
            f"this test can no longer SEE it, and an agreement assertion over a "
            f"set it cannot see is vacuously true (#lzstampprefixdrift). If the "
            f"definition moved or changed shape, move the pattern with it."
        )
        found[name] = matches[0]
    return found


def test_every_definition_of_the_stamp_prefix_agrees() -> None:
    """The four spellings are one string, checked as one string.

    Falsified by changing any single character on any one side: the offending
    file is named, next to the value every other side agrees on.
    """
    parsed = _parsed_definitions()
    reference = "tests/conftest.py `_RUN_ID_PREFIX`, imported as a value"
    everything = {reference: _RUN_ID_PREFIX, **parsed}
    distinct = set(everything.values())
    assert len(distinct) == 1, (
        "the run-id stamp prefix is spelled differently in different places, so "
        "the writer and the reader of the manifest's first line no longer agree "
        "(#lzstampprefixdrift). This fails closed — the guard refuses the stamp "
        "— but it presents as 'no run-id stamp on its first line', which reads "
        "as missing evidence rather than as a typo. Spellings found:\n"
        + "\n".join(f"  {value!r}  <- {where}" for where, value in everything.items())
    )


def test_the_python_side_defines_the_prefix_exactly_once() -> None:
    """No second Python literal, which the value-import above could not see.

    :func:`_parsed_definitions` pins the text-parsed sides to one occurrence
    each. The imported side needs the same floor from the other direction: a
    second assignment in ``conftest.py`` would shadow or diverge from the first
    and the import would silently pick whichever ran last.
    """
    assignments = re.findall(
        r"^_RUN_ID_PREFIX\s*=\s*(.+)$", _read(CONFTEST), re.MULTILINE
    )
    assert len(assignments) == 1, (
        f"tests/conftest.py assigns `_RUN_ID_PREFIX` {len(assignments)} time(s); "
        f"wanted exactly one. Found: {assignments}"
    )
    assert assignments[0].strip() in {
        f'"{_RUN_ID_PREFIX}"',
        f"'{_RUN_ID_PREFIX}'",
    }, (
        f"tests/conftest.py's `_RUN_ID_PREFIX` is no longer a plain string "
        f"literal ({assignments[0].strip()!r}), so the text-parsed sides above "
        f"are being compared against something computed. That may be fine, but "
        f"it has to be looked at: the whole point is that one spelling is the "
        f"only spelling."
    )


@pytest.mark.parametrize(
    "path",
    [COVERAGE_SCRIPT, PYPROJECT],
    ids=["coverage-guard", "pyproject"],
)
def test_no_unparsed_executable_spelling_of_the_prefix(path: Path) -> None:
    """A FIFTH live definition would drift outside the agreement check.

    The agreement test above couples the definitions it knows about. A new
    executable occurrence — a second `sed`, a `grep` pattern, a `case` label —
    would not be in that set, and could carry a different spelling forever.

    Comment and prose mentions are exempt by design: they do not run, and the
    two scripts explain this protocol at length. The rule is per-file-type and
    deliberately crude — a line whose first non-space character is ``#`` is
    prose — which is exactly the comment syntax both of these files use.
    """
    text = _read(path)
    # 1-based line numbers the known definitions sit on, found by MATCH POSITION
    # rather than by re-matching each line: two of the three patterns are
    # line-anchored and would never match a single line pulled out of context.
    definition_lines = {
        text.count("\n", 0, match.start()) + 1
        for _, definition_path, pattern in _DEFINITIONS
        if definition_path == path
        for match in re.finditer(pattern, text, re.MULTILINE)
    }
    stray = [
        f"{number}: {line}"
        for number, line in enumerate(text.splitlines(), start=1)
        if _RUN_ID_PREFIX in line
        and not line.lstrip().startswith("#")
        and number not in definition_lines
    ]
    assert not stray, (
        f"{path} spells the run-id stamp prefix on an executable line that the "
        f"agreement check does not cover, so that spelling can drift from the "
        f"other four without anything noticing (#lzstampprefixdrift). Either "
        f"derive it from an existing definition, or add it to _DEFINITIONS. "
        f"Lines:\n" + "\n".join(f"  {line}" for line in stray)
    )

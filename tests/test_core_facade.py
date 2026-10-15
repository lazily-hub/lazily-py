from __future__ import annotations

from pathlib import Path

from setuptools import Distribution, find_namespace_packages
from setuptools.command.build_py import build_py

import lazily
import lazily.core as core


def test_core_facade_preserves_public_identities() -> None:
    for name in (
        "Context",
        "Source",
        "Computed",
        "Effect",
        "TeardownScope",
        "ThreadSafeContext",
        "AsyncContext",
        "batch",
    ):
        assert getattr(core, name) is getattr(lazily, name)


def test_built_package_contains_core_facade_and_metadata(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    distribution = Distribution(
        {
            "packages": find_namespace_packages(where=str(root / "src")),
            "package_dir": {"": str(root / "src")},
        }
    )
    distribution.script_name = str(root / "setup.py")
    command = build_py(distribution)
    command.build_lib = str(tmp_path / "wheel-root")
    command.ensure_finalized()
    command.run()

    assert (Path(command.build_lib) / "lazily" / "core.py").is_file()
    for metadata in ("LICENSE", "NOTICE", "README.md", "pyproject.toml"):
        assert (root / metadata).is_file()

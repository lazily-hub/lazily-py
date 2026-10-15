#!/usr/bin/env python3
"""Check the committed Lazily Core API snapshot against the source package."""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

from griffe import Alias, Class, GriffeLoader


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = ROOT / "api" / "core-v1.json"
COMPARISON_KEYS = ("path", "kind", "target", "signature", "annotation", "labels")


def _describe(path: str, obj: Any, *, target: str) -> dict[str, Any]:
    signature = str(obj.signature()) if hasattr(obj, "signature") else None
    annotation = getattr(obj, "annotation", None)
    return {
        "path": path,
        "kind": obj.kind.value,
        "target": target,
        "signature": signature,
        "annotation": str(annotation) if annotation is not None else None,
        "labels": sorted(obj.labels),
    }


def extract_surface() -> list[dict[str, Any]]:
    """Extract the exported facade and public members of its exported classes."""
    loader = GriffeLoader(search_paths=[ROOT / "src"])
    module = loader.load("lazily.core")
    surface: list[dict[str, Any]] = []

    for name in sorted(module.exports or ()):
        member = module.members[name]
        target = member.target if isinstance(member, Alias) else member
        target_path = (
            member.target_path if isinstance(member, Alias) else target.canonical_path
        )
        surface.append(_describe(name, target, target=str(target_path)))

        if not isinstance(target, Class):
            continue
        for child_name, child_member in sorted(target.all_members.items()):
            if not child_member.is_public or child_name in {"__init__", "__slots__"}:
                continue
            child_target = (
                child_member.target if isinstance(child_member, Alias) else child_member
            )
            surface.append(
                _describe(
                    f"{name}.{child_name}",
                    child_target,
                    target=str(child_target.canonical_path),
                )
            )

    return sorted(surface, key=lambda item: item["path"])


def _validate_metadata(snapshot: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    generation = snapshot.get("core_api_generation")
    support = snapshot.get("module_compatibility", {})
    oldest = support.get("oldest_supported_core_api_generation")
    current = support.get("current_core_api_generation")
    if not isinstance(generation, int) or generation < 1:
        errors.append("core_api_generation must be a positive integer")
    if not isinstance(oldest, int) or not isinstance(current, int):
        errors.append("module compatibility generations must be integers")
    elif not isinstance(generation, int) or not oldest <= generation <= current:
        errors.append("core_api_generation must fall inside the supported range")

    policy = snapshot.get("deprecation_policy", {})
    minimum_window = policy.get("minimum_compatibility_window_generations")
    if not isinstance(minimum_window, int) or minimum_window < 2:
        errors.append("deprecation policy must retain at least two generations")

    required = {
        "path",
        "introduced_generation",
        "deprecated_since_generation",
        "replacement",
        "compatibility_window_generations",
        "earliest_removal_generation",
    }
    for index, deprecation in enumerate(snapshot.get("deprecations", [])):
        missing = required - deprecation.keys()
        prefix = f"deprecations[{index}]"
        if missing:
            errors.append(f"{prefix} is missing {sorted(missing)}")
            continue
        introduced = deprecation["introduced_generation"]
        deprecated = deprecation["deprecated_since_generation"]
        window = deprecation["compatibility_window_generations"]
        earliest = deprecation["earliest_removal_generation"]
        if not isinstance(introduced, int) or not isinstance(deprecated, int):
            errors.append(f"{prefix} introduction/deprecation must be integers")
        elif deprecated < introduced:
            errors.append(f"{prefix} predates its introduction")
        if (
            not isinstance(deprecation["replacement"], str)
            or not deprecation["replacement"]
        ):
            errors.append(f"{prefix} must name a replacement")
        if not isinstance(window, int) or not isinstance(minimum_window, int):
            errors.append(f"{prefix} compatibility window must be an integer")
        elif window < minimum_window:
            errors.append(f"{prefix} is shorter than the policy minimum")
        if not isinstance(earliest, int):
            errors.append(f"{prefix} earliest removal must be an integer")
        elif (
            isinstance(deprecated, int)
            and isinstance(window, int)
            and earliest < deprecated + window
        ):
            errors.append(f"{prefix} earliest removal violates its window")

    paths = [item.get("path") for item in snapshot.get("surface", [])]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        errors.append("surface paths must be unique and sorted")
    for item in snapshot.get("surface", []):
        introduced = item.get("introduced_generation")
        if not isinstance(introduced, int) or not isinstance(generation, int):
            errors.append(f"{item.get('path', '<unknown>')} has invalid introduction")
        elif not 1 <= introduced <= generation:
            errors.append(f"{item['path']} introduction is outside the snapshot")
    return errors


def _comparable(surface: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: item.get(key) for key in COMPARISON_KEYS} for item in surface]


def check_snapshot(snapshot_path: Path) -> list[str]:
    snapshot = json.loads(snapshot_path.read_text())
    errors = _validate_metadata(snapshot)
    expected = _comparable(snapshot.get("surface", []))
    actual = _comparable(extract_surface())
    if expected != actual:
        expected_text = json.dumps(expected, indent=2, sort_keys=True).splitlines()
        actual_text = json.dumps(actual, indent=2, sort_keys=True).splitlines()
        diff = "\n".join(
            difflib.unified_diff(
                expected_text,
                actual_text,
                fromfile=str(snapshot_path),
                tofile="Griffe(lazily.core)",
                lineterm="",
            )
        )
        errors.append(f"Core API snapshot does not match the package surface:\n{diff}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument(
        "--dump-surface",
        action="store_true",
        help="print the normalized Griffe surface without changing files",
    )
    args = parser.parse_args()
    if args.dump_surface:
        print(json.dumps(extract_surface(), indent=2, sort_keys=True))
        return 0

    errors = check_snapshot(args.snapshot)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Core API snapshot OK: {args.snapshot.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

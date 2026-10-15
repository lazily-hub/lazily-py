#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
work="$(mktemp -d "${TMPDIR:-/tmp}/lazily-py-core-consumer.XXXXXX")"
trap 'rm -rf "$work"' EXIT

artifact_dir="$work/artifact"
consumer_dir="$work/consumer"
mkdir -p "$artifact_dir" "$consumer_dir"
uv build --wheel --out-dir "$artifact_dir" "$root"
wheel="$(find "$artifact_dir" -maxdepth 1 -type f -name '*.whl' -print -quit)"
test -n "$wheel"

uv venv --seed "$work/venv"
uv pip install --python "$work/venv/bin/python" "$wheel"
cp "$root/tests/core_artifact_consumer.py" "$consumer_dir/main.py"
cd "$consumer_dir"
"$work/venv/bin/python" -I main.py

if find "$work/venv/lib" -path '*/lazily-*.dist-info/direct_url.json' -exec grep -F "$root" {} + 2>/dev/null | grep -q .; then
  echo "Core artifact consumer resolved the source checkout" >&2
  exit 1
fi

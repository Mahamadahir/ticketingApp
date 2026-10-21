#!/usr/bin/env bash
# Recompile requirements.lock and requirements-dev.lock from the two input files.
# Run from the repo root with uv installed (pip install uv==0.13.4). Extra arguments
# go to uv, e.g. --upgrade to move transitive packages forward. CI runs this with no
# arguments and fails if the committed locks differ (pip-audit.yml, lock-current).
set -euo pipefail

for name in requirements requirements-dev; do
  uv pip compile "$name.txt" --universal --python-version 3.14 --generate-hashes \
    --custom-compile-command "uv pip compile $name.txt --universal --python-version 3.14 --generate-hashes -o $name.lock" \
    --quiet -o "$name.lock" "$@"
done

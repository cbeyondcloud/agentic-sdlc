#!/bin/sh
# Runs INSIDE the container: build the index on Linux at a given commit and export it.
# args: <repo-name> <sha>
set -e
NAME="$1"
SHA="$2"
export PYTHONDONTWRITEBYTECODE=1
git config --global --add safe.directory '*'
git clone -q "/src/$NAME" "/work/$NAME"
cd "/work/$NAME"
git checkout -q "$SHA"
uv venv /tmp/v -q
uv pip install -q --python /tmp/v/bin/python code-review-graph==2.3.9
START=$(date +%s.%N)
/tmp/v/bin/code-review-graph build --repo "/work/$NAME" -q 2>/dev/null
END=$(date +%s.%N)
echo "linux_build_s $(echo "$END - $START" | bc 2>/dev/null || echo n/a)"
ls -l "/work/$NAME/.code-review-graph/graph.db"
/tmp/v/bin/python /spike/crg_export.py export "/work/$NAME" "/out/exp_linux_$NAME" file

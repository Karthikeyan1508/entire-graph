#!/usr/bin/env bash
# Pays the cold committed-tree index cost ONCE (17-73s on this repo, see NOTES.md), so every
# --head --profile full adapter call in tower/core.py hits a warm cache instead of rebuilding
# per prompt. Run this before a demo/session, and again after every commit.
set -uo pipefail
cd "$(dirname "$0")/.."

entire graph index --repo . --profile full --format text
entire graph search --repo . --head --profile full --query "flight plan lease separation" --top-k 1 --format json > /dev/null
entire graph impact --repo . --head --profile full --symbol internal/sem/search.go:683 --depth 2 --format json > /dev/null
echo "prewarm complete"

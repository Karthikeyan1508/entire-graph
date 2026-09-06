#!/usr/bin/env bash
# Get this machine ready to demo a live deny. Run it AFTER your last commit and BEFORE you present.
#
# Two things it fixes, both of which have bitten us:
#   1. Any commit invalidates Entire's committed-tree cache. The next flight plan then pays the full
#      cold index cost (~57s), overruns its hook timeout, and leases NOTHING -- TOWER looks like it
#      is running while protecting nobody.
#   2. `impact` is called once per seed, and a COLD impact is 13-45s. A prompt whose seeds were never
#      warmed will stall or time out. So we warm the exact two prompts the demo uses, not just the index.
set -uo pipefail
cd "$(dirname "$0")/.."

PY="${TOWER_PY:-D:/tower/.venv/Scripts/python.exe}"
WARM_DB="${TMPDIR:-/tmp}/tower-demo-warm.db"

A_PROMPT="I need to change how SearchRepository ranks results"
B_PROMPT="Remove the diversity reserve in selectDiverseCandidates in internal/sem/search.go"

echo "==> 1/3  building the committed-tree index (~57s, expected)"
entire graph index --repo . --profile full --format text || echo "    (index failed -- continuing)"

echo "==> 2/3  warming the exact prompts the demo uses"
rm -f "$WARM_DB"
W_PROMPT="Add a test case to internal/sem/search_callee_test.go covering an empty callee list"
C_PROMPT="Add doc comments to the exported functions in internal/termsafe/termsafe.go"

for q in "$A_PROMPT" "$B_PROMPT" "$W_PROMPT" "$C_PROMPT"; do
  printf '  %.60s...\n' "$q"
  START=$(date +%s)
  printf '{"session_id":"warmup","cwd":".","hook_event_name":"UserPromptSubmit","prompt":"%s"}' "$q" \
    | TOWER_DB_PATH="$WARM_DB" "$PY" hooks/tower_flightplan.py >/dev/null 2>&1
  echo "     warmed in $(( $(date +%s) - START ))s"
done
rm -f "$WARM_DB"

echo "==> 3/3  checks"
"$PY" - <<'PYEOF'
import pathlib, sqlite3, yaml
pol = yaml.safe_load(open("policy.yaml", encoding="utf-8"))
k = pol.get("seed_top_k")
print(f"     seed_top_k = {k}" + ("" if k == 3 else "   <-- expected 3 for the demo"))
db = pathlib.Path.home()/".tower"/"tower.db"
c = sqlite3.connect(str(db)); c.row_factory = sqlite3.Row
n = c.execute("select count(*) n from prior_cache").fetchone()["n"]
print(f"     prior_cache = {n} rows" + ("" if n else "   <-- empty: prior will be 0.0, deny scores 0.65 not 0.88"))
live = c.execute("select count(*) n from leases where released_at is null and expires_at > strftime('%s','now')").fetchone()["n"]
print(f"     stale active leases = {live}" + ("" if not live else "   <-- close old sessions, or B may collide with the wrong one"))
PYEOF

echo
echo "READY.  Two fresh terminals in this directory, then:"
echo "  session A:  $A_PROMPT"
echo "  session B:  $B_PROMPT"
echo "Do not commit between now and the demo."

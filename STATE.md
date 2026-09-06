# STATE — where a fresh session should start reading

Architecture and rejected options: `ARCHITECTURE.md`. Submission writeup: `BUILDATHON.md`.
Real CLI shapes: `NOTES.md`. This file is the fast path for someone (or something) joining mid-flight.

## Runbook rule that will silently break the demo if skipped

**`scripts/prewarm.sh` must run after EVERY commit, not once at the start of the day.** Proven this
session: a commit changes the tree hash, which invalidates Entire's committed-tree cache, which every
`--head --profile full` adapter call depends on. Without a fresh prewarm, the first `search`/`impact`
call after a commit pays the full cold-index cost again (17s-70s+ on this repo) against a hook timeout
that will kill it first — `tower_flightplan.py` then leases 0 symbols and (as of this session) says so
loudly instead of failing open silently.

**Specifically: prewarm must run after the 11:40 freeze commit and again after any Curveball commit,
before the noon demo / the Curveball session's first prompt.** Skipping this means the first flightplan
of that session silently (well — now loudly, but still) leases nothing, and TOWER protects nobody until
someone notices and re-runs it.

No `scripts/demo_up.sh`/`demo_reset.sh` exist yet — if
one gets written, the prewarm step belongs at its start, before the first prompt is sent.

## Intent

TOWER makes a coding agent file a flight plan (prompt -> graph search + impact -> a lease of symbols)
before it edits, and denies a second agent's edit inside or near that lease at the PreToolUse hook,
carrying the first session's intent so the second agent reroutes. Prevention, not review; call-graph
space, not file space.

## Architecture in 10 lines

1. `tower/core.py` — one module: SQLite store + the only `entire` adapter surface + scoring. No server.
2. `hooks/tower_flightplan.py` (UserPromptSubmit) — `file_flight()`, prints lease size / warns if 0.
3. `hooks/tower_separation.py` (PreToolUse Edit|Write|MultiEdit) — `check()`, pure SQL, denies or
   prints nothing.
4. `hooks/tower_session.py` (SessionEnd) — releases this session's leases.
5. Leases store `hops` + `via` at flight-plan time (from `impact.callers/callees/type_consumers`
   entries' `depth` field) so the separation check never shells out (CUT 3).
6. Symbol identity is Entire's own compound `id`, not a hand-rolled `path::name`.
7. `score = 0.65*structural + 0.35*prior`; structural from hops {0:1.0, 1:0.6, 2:0.35}; prior from
   `prior_cache`, default 0.0 until Phase D.
8. Every adapter call is `--head --profile full` against a cache warmed once by `scripts/prewarm.sh`
   (see the runbook rule above) — never working-tree, never per-prompt cold.
9. Every hook wraps everything in try/except and always `sys.exit(0)` — fail-open everywhere.
10. `render_deny()` is a plain-text template, no LLM call, no network on the hot path.

## Done

- Recon (`NOTES.md`, `fixtures/`) — graph evidence #1, committed.
- `tower/core.py` + `tests/test_core.py` (9/9 passing, `TOWER_FIXTURES=1`, isolated DB via
  `TOWER_DB_PATH`) — committed.
- Three hooks + `.claude/settings.json` merge, proven end-to-end with piped fake payloads
  (flightplan leases real symbols; separation denies cross-session, clears same-session).

## Unresolved / open risks

1. **Measured latency floor on the separation hook: ~500-800ms, not the <200ms target.** Root cause
   isolated: bare `python.exe` process startup on this Windows machine is ~400-450ms by itself, before
   any TOWER code runs; `check()`'s own SQL logic is a small fraction of that. The 200ms budget is
   achievable for the logic, not for "spawn a fresh interpreter per PreToolUse call" — and the
   architecture that would fix it (a persistent process) is the daemon V2 explicitly cut. Not resolved,
   not silently accepted either — flagged for a decision.
2. Prewarm-after-every-commit (above) is a manual step with no automation or reminder besides this
   file and the prewarm.sh comment. Nothing currently stops someone from forgetting it.
3. `check()`'s hops=0 prior lookup was a same-file `(file, file)` key in `prior_cache` (the original
   literal reading) — real co-change data never produces same-file pairs, so the 0.63->0.87
   calibration currently depends on staging that exact row by hand. Deferred to Phase D.
4. Real two-terminal, two-session proof (the actual C2 gate) has not yet run — only piped fake
   payloads have been verified.

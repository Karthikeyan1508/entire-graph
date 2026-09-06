# TOWER — architecture

**Air traffic control for coding agents.** BTW Buildathon 2026 · Track 02 (Graph Intelligence) · solo, ~3h05m of coding.

Authoritative plan: this file, plus `STATE.md` for current state and `NOTES.md` for the real
`entire` CLI shapes captured on this machine.

---

## Intent

Multiple coding agents work one repo in parallel with zero awareness of each other. Collisions surface
hours later as merge conflicts or silently broken callers. TOWER makes each agent **file a flight plan**
before it edits: its prompt is resolved through the Entire Graph into the symbols it will plausibly
touch, and that set becomes a **lease**. A second agent editing inside or near that lease is **denied at
its own PreToolUse hook**, and the denial carries the first session's **intent** (from its Entire
Checkpoint) so it can reroute.

*We don't resolve conflicts between agents. We maintain separation.*

Prevention, not review. Call-graph space, not file space.

---

## Data flow — four phases

1. **File** — `UserPromptSubmit`: prompt → `entire graph search` → `entire graph impact --depth 2` →
   lease rows (symbol, path, line_start, line_end, hops, via) written to SQLite. Slow work happens
   here, once per prompt, where a few seconds is invisible.
2. **Check** — `PreToolUse` on `Edit|Write|MultiEdit`: file path → SQL against *other* sessions'
   unexpired lease rows → lowest hops wins. Pure SQLite, no subprocess, no network.
3. **Decide** — score the conflict, write a squawk, and either print deny JSON or print nothing.
4. **Learn** — squawks drain from `outbox` to Delta out-of-band; the co-change prior flows back into
   `prior_cache` and sharpens the next decision.

## Scoring — fixed, not to be "improved"

```
structural = {0: 1.00, 1: 0.60, 2: 0.35}[hops]     # else 0.0
score      = 0.65 * structural + 0.35 * prior      # prior from Databricks, default 0.0
denied  if score >= 0.55
warned  if score >= 0.30
cleared otherwise
```

hops 0 with prior 0.63 must produce exactly **0.87**. There is a test for it, and it is the number in
the pitch.

## The seams

| Seam | Contract | Why it is a seam |
|---|---|---|
| **Adapter** | `search_symbols`, `symbol_at_file`, `impact_set`, `session_intent` in `tower/core.py` | Real Entire JSON shapes are unknown until recon. Everything else is written against these four functions, never against Entire's JSON. `TOWER_FIXTURES=1` swaps in `fixtures/`. |
| **Scoring** | pure functions over `(hops, prior)` | Unit-testable with no Entire and no Databricks. |
| **Policy** | `policy.yaml`, re-read per call | Curveball insurance. Thresholds, depths and exempt globs are config, not code. |
| **Sink** | `outbox` table | Delta is never on the request path. Databricks down ⇒ prior 0.0 ⇒ structural alone still denies at hops 0 (0.65 ≥ 0.55). |

## What TOWER guards

This repo: a fork of `entireio/entire-graph` (454 Go files, 1,334 commits), chosen because the
buildathon host designated it. TOWER is built inside the codebase it protects, so v2's line holds
literally — *"TOWER guards the repo it lives in."* Worth saying once in the demo: TOWER uses the
Entire Graph to protect the Entire Graph's own source.

The vendored tree-sitter grammar C (`parser.c`, `scanner.c` — some blobs 30–54 MB) is already excluded
by the repo's own `.graphignore`, added upstream after those files produced `E_FILE_TOO_LARGE` /
`E_PARSE_ERROR` and a "degraded" graph. We inherit that fix. Do not remove it.

**Calibration note:** the original design sketch calibrated the prior against `gorilla/mux`, which
is not this repository. The prior is therefore computed from *this* repo's own history instead. The
formula, the weights and the thresholds are unchanged.

## Rejected options (and why)

- **A FastAPI daemon on :8765** — deleted. ~45 min of work and the single most likely demo
  failure ("the daemon wasn't running"). Hooks talk to SQLite directly; WAL handles concurrency.
- **BFS + adjacency map + `path_between`** — deleted. `impact_set(depth=2)` already returns hop counts.
  Distance becomes a SQLite lookup, not a traversal. Store `via` so the deny message still prints a chain.
- **Subprocess on the hot path** — deleted. All graph work happens at flight-plan time. The check is ~5 ms.
- **Git worktrees** — deleted. Two terminals, one repo, identity from `session_id`. A same-tree
  collision is a more honest demo.
- **A second repo to guard** — deleted. TOWER guards the Entire fork it lives in.

## Verified on this machine (2026-09-06)

- Python 3.11.9; SQLite 3.45.1; **WAL confirmed** at `C:\Users\This PC\.tower\` — the space in the
  profile path does not break it when built via `Path.home()`.
- Deps installed into `D:\tower\.venv` (outside the clone, deliberately: nothing to gitignore, and
  hooks are required to use an absolute interpreter path anyway).

## Open risks

1. **Windows hook invocation.** A POSIX-style `python3 $CLAUDE_PROJECT_DIR/hooks/x.py` does not run here.
   Hooks must use `D:\tower\.venv\Scripts\python.exe` with JSON-escaped backslashes, and every path
   touching the profile directory must be quoted. Highest-probability silent failure.
2. **Unknown Entire CLI output shapes.** Mitigated by recon-first and the adapter seam; any
   command syntax is treated as a guess until `NOTES.md` says otherwise.
3. **Lease quality.** If `graph search` returns weak seeds the lease is wrong, and TOWER either denies
   nothing or denies everything. Cap 300 symbols; inspect the first real lease by hand.
4. **Demo depends on a real deny.** Everything else is cuttable. Record the GIF the second it works.
5. **Fail-open is load-bearing.** Any hook exception must `sys.exit(0)`. A wedged agent is worse than
   no TOWER.

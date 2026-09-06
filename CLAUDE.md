@AGENTS.md

<!-- entire-graph:begin -->
<!-- Entire Graph instructions are inherited through AGENTS.md. -->
<!-- entire-graph:end -->

# TOWER — standing rules for this repo

## What we are building
TOWER stops two AI coding agents from breaking each other's work. Before an agent edits, its prompt is
resolved through the Entire Graph into the set of symbols it will touch — that set becomes its **lease**.
When another agent tries to edit inside or near that lease, the edit is **denied at the PreToolUse hook**,
and the denial carries the first session's **intent** (from its Entire Checkpoint) so the blocked agent
can reroute. Prevention, not review. Call-graph space, not file space.

## Read before acting
1. `TOWER_V2_NO_HOMEWORK.md` — the current plan. **It overrides the spec wherever they disagree.**
2. `TOWER_BUILD_SPEC.md` — data model, scoring, hook JSON contracts, non-goals.
3. `NOTES.md` — the real Entire CLI output shapes, captured on this machine. Trust this over any
   command syntax written in the spec.

**Once `NOTES.md` exists, read it before you run any `entire` command or write any parser.** Every
`entire` invocation printed in the spec — §4.5, §9, the Prompt R block — is a guess written before the
CLI was ever run. `NOTES.md` is the only record of what this machine actually returned. If the two
disagree, `NOTES.md` wins and the spec is wrong. This applies to a fresh session as much as to this one.

## Rules I will not repeat
- **Do not redesign.** The architecture is decided. If something in the spec is wrong, fix it in one
  place, tell me in one line, and continue. Never open an alternatives discussion.
- **No daemon, no server, no threads, no BFS, no ORM, no React, no Docker, no CI.** Hooks talk to
  SQLite directly.
- **All Entire calls live in the adapter functions in `tower/core.py`.** Nowhere else, ever.
- **Fail-open everywhere.** Any error, timeout or unknown → the edit is `cleared`. A hook must never
  raise, never block on the network, and always `sys.exit(0)`.
- **The separation check must stay under 200 ms.** It reads SQLite only — no subprocess, no network.
- **Never overwrite `.claude/settings.json`.** Entire owns hooks in there too. Merge, and show me the
  file before and after.
- **Never touch anything under `.entire/`.**
- **No secrets** in code, prompts, commits, screenshots or `BUILDATHON.md`. Databricks credentials live
  in my shell only. Never echo `$DATABRICKS_TOKEN`.
- **Do not add features I did not ask for.** If you think of one, say so in one sentence and move on.
- When a phase's acceptance test passes, **stop and tell me.** Do not roll into the next phase.

## Scoring formula — do not change it
```
structural = {0: 1.00, 1: 0.60, 2: 0.35}[hops]        # else 0.0
score      = 0.65 * structural + 0.35 * prior         # prior from Databricks, default 0.0
denied  if score >= 0.55
warned  if score >= 0.30
cleared otherwise
```
hops 0 with prior 0.63 must produce exactly **0.87**. There is a test for this. It is also the number
in the pitch, so it does not get "improved".

## Deliverables that are scored directly — remind me if I drift
Four checkpoints: **CP1** initial architecture · **CP2** last stable state before noon ·
**CP3** Curveball response · **CP4** final implementation and verification.

Three graph evidences, all saved under `evidence/`:
1. a graph search or definition lookup (recon output),
2. an impact analysis run **before** a high-risk change (the noon assessment),
3. a **final semantic diff** — `entire graph diff pre-noon-stable..HEAD`.

Checkpoint commits should capture decisions, rejected options, failures, assumptions and open risks —
not just "added feature X".

## Today's clock
Build stops **14:20**. Submission closes **15:00**. If it is past 13:45 and something is unfinished,
say so and propose what to cut rather than starting it.

# TOWER v2 — the no-homework plan
**This supersedes §7 (daemon) of TOWER_BUILD_SPEC.md and the whole phase table.**
Everything else in the spec — scoring, data model, hook JSON contracts, policy.yaml — still stands.

You have ~3h05m of actual coding after setup and freezes. The idea survives intact; three pieces of
machinery get deleted.

---

## The three cuts (and why they cost you nothing)

### CUT 1 — No daemon. No FastAPI. No port. No uvicorn.
Hooks talk to SQLite directly. A hook is a short-lived process; SQLite in WAL mode handles concurrent
readers and writers fine. This deletes ~45 minutes of work *and* the single most likely demo failure
("the daemon wasn't running"). Lease expiry is a timestamp comparison at read time, not a sweeper thread.

### CUT 2 — No BFS, no adjacency map, no `path_between`.
When you file a flight plan you already compute `impact_set(symbol, depth=2)`, which gives you every
symbol **and its hop count**. Store those rows. Distance at check time is then a **SQLite lookup**, not
a graph traversal. Store `via` (the parent symbol that pulled it into the halo) so the deny message can
still print `target ← via ← their_symbol`.

### CUT 3 — No subprocess on the hot path.
Store `path`, `line_start`, `line_end`, `hops`, `via` on every lease row at flight-plan time. The
PreToolUse check then resolves the target **by file path against the lease table** — pure SQL, ~5 ms,
zero calls to `entire`. The expensive graph work happens once per prompt, in the UserPromptSubmit hook,
where a 5-second pause is invisible.

**Also:** two agent sessions in the **same repo, two terminals** — no worktrees. Session identity comes
from `session_id` in the hook payload. Simpler, and a same-tree collision is a *more* honest demo.

**And:** the codebase TOWER guards is **the Entire fork you're building in**. No second repo to clone or
index, and the story is better: *"TOWER guards the repo it lives in."* If `entire graph index` is slow on
it, scope the index to one subdirectory.

---

## v2 architecture — five files that matter

```
hooks/tower_flightplan.py   UserPromptSubmit, timeout 60
    prompt -> entire graph search -> entire graph impact(depth 2)
    -> write lease rows (symbol, path, line_start, line_end, hops, via) to SQLite
    -> print additionalContext

hooks/tower_separation.py   PreToolUse "Edit|Write|MultiEdit", timeout 10
    file_path -> SQL against other sessions' active leases -> lowest hops wins
    -> score -> write squawk -> print deny JSON, or print nothing

hooks/tower_session.py      SessionEnd -> mark leases released

tower/core.py               store + adapter + scoring, one module, no server
tower/sync.py               drains the outbox table to Delta; run in a spare terminal:
                            while true; do python -m tower.sync; sleep 10; done
app/app.py                  Streamlit radar
```

Hooks import the `tower` package directly and wrap **everything** in `try/except: sys.exit(0)`.
Use the venv's absolute python path in `.claude/settings.json` so a PATH surprise can't wedge your agent.

Scoring is unchanged: `score = 0.65×structural + 0.35×prior`, structural = 1.0 / 0.6 / 0.35 by hops,
deny at ≥ 0.55. Distance 0 with prior 0.63 still lands on exactly **0.87**.

---

## Re-planned phases

| Phase | Window | Mins | Gate |
|---|---|---|---|
| **S · Setup** | 09:00–09:35 | 35 | Fork mirrored + cloned, checkpoints on, graph active, **CP1 committed** |
| **R · Recon** | 09:35–09:50 | 15 | Real graph JSON in `NOTES.md` + `fixtures/` — *graph evidence #1* |
| **C · Core** | 09:50–11:00 | 70 | **A live agent session is DENIED. Record the GIF the second it works.** |
| **D · Databricks** | 11:00–11:40 | 40 | `cochange` table built; prior in the score; squawks in Delta |
| **F · Freeze** | 11:40–12:00 | 20 | Tag, **CP2**, and **70% of BUILDATHON.md already written** |
| **— Curveball** | 12:00–13:00 | 60 | Fresh session, reconstruct, **impact analysis before editing** — *evidence #2*. Eat while it runs. |
| **X · Adapt** | 13:00–14:00 | 60 | Constraint implemented + tested → **CP3** |
| **E · Evidence** | 14:00–14:20 | 20 | `entire graph diff` semantic diff — *evidence #3*; screenshots; GIF filed |
| **W · Write** | 14:20–14:45 | 25 | BUILDATHON.md finished → **CP4** |
| **Z · Submit** | 14:45–15:00 | 15 | Checklist, every link opened signed-out, **submitted** |

**Write BUILDATHON.md during the freeze, not at 14:20.** Problem, user, track, why Entire is essential,
architecture — none of that changes at noon. At 14:20 you are only adding the Curveball and verification
sections. This is the single biggest time saving available to you, and mentors review that file first.

---

## Phase S — 09:00, run these in parallel

**Browser tab 1 (start it first, it processes while you work):** Databricks Free Edition signup +
LinkedIn verification. Do not wait on it.

**Terminal 1 (network, runs unattended):**
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install pyyaml requests pytest databricks-sql-connector databricks-sdk streamlit
```

**Terminal 2 (you, hands on):**
```bash
# fork the Track 2 repo on GitHub now, then:
entire login
entire repo mirror create
entire repo clone /gh/<YOUR-HANDLE>/<REPO>      # choose your fork + INDIA region
cd <REPO>

entire enable -y --agent claude-code
entire status

entire plugin install graph
entire graph version
entire graph init-agents --repo .
git add .entire/graph-agent.md AGENTS.md CLAUDE.md && git commit -m "Enable entire-graph"
```

Then **start a fresh agent session** (required — it picks up the graph instructions), and inside it:
```
/plugin marketplace add entireio/skills
/plugin install entire
```

Write `ARCHITECTURE.md` (intent, the seams, the scoring formula, open risks), commit → **CP1**.

Databricks CLI and the SQL MCP are **deferred to Phase D**. Do not touch them now.

---

## The prompt sequence — paste these into Claude Code

### Prompt R — 09:35 · Recon (15 min)
```
Read TOWER_BUILD_SPEC.md and TOWER_V2_NO_HOMEWORK.md. V2 overrides the spec where they disagree:
no daemon, no BFS, no subprocess on the hot path.

I have never run the Entire CLI before, so I do not know its real output shapes. First, capture them.
Write and run scripts/recon.sh against THIS repo:
  entire graph --help                         -> NOTES.md
  entire graph capabilities                   -> NOTES.md
  entire graph search --query "<pick a real concept from this repo>" --format json --top-k 8
                                              -> fixtures/search.json + NOTES.md
  entire graph def --symbol <a real file:line from this repo> --json
                                              -> fixtures/def.json + NOTES.md
  entire graph impact --symbol <that symbol> --depth 2 --json
                                              -> fixtures/impact.json + NOTES.md
  entire checkpoint list --json               -> fixtures/checkpoints.json
On any failure, record the exact error in NOTES.md and keep going.

Run it, then TELL ME the real field names before writing a single parser. In particular: how impact
reports hop distance, and whether def returns line ranges. Do not write tower/ yet.
```

### Prompt C1 — 09:50 · The core module (35 min)
```
Write tower/core.py as ONE module. No FastAPI, no server, no threads.

1. SQLite at ~/.tower/tower.db, WAL mode, tables: flights, leases, squawks, prior_cache, outbox
   (schema from spec §3.2, but leases also carry: path, line_start, line_end, hops, via).
2. Adapter functions parsing the REAL shapes you just captured, each wrapped in try/except returning
   empty, each with a subprocess timeout of 3s:
     search_symbols(prompt, top_k)   symbol_at_file(path)   impact_set(symbol_id, depth)
     session_intent(session_id)      # entire checkpoint explain, short form
   TOWER_FIXTURES=1 makes them read fixtures/ instead.
3. file_flight(session_id, prompt, repo, branch) -> builds core+halo leases, caps at 300 symbols,
   TTL 20 min, stores every row with hops and via.
4. check(session_id, file_path) -> looks up OTHER sessions' unexpired lease rows for that file,
   takes the lowest hops, computes score = 0.65*structural + 0.35*prior with
   structural = {0:1.0, 1:0.6, 2:0.35}, prior from prior_cache (default 0.0),
   returns Decision(decision, score, structural, prior, hops, other_session, other_intent, via, target).
   Exempt globs, self-owned leases, and any exception -> "cleared".
5. render_deny(decision) -> the exact plain-text block from spec §8. Template only, no LLM.
6. record_squawk() writes to squawks AND enqueues the same row into outbox.

Then tests/test_core.py with TOWER_FIXTURES=1 proving:
  hops 0 + prior 0.63 -> 0.87 -> denied      hops 1 + prior 0 -> 0.39 -> warned
  hops 2 + prior 0 -> 0.23 -> cleared        own lease -> cleared      exempt glob -> cleared
Run pytest and show me the table.
```

### Prompt C2 — 10:25 · Hooks and the live deny (35 min) ⛔ THE GATE
```
Write the three hooks from V2. Each reads stdin JSON, wraps everything in try/except, exits 0 always.
Use the absolute venv python path in the command.

Register them in .claude/settings.json — MERGE with whatever Entire already put there, do not
overwrite. Show me the file before and after.

PreToolUse must print exactly:
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"<render_deny output>"}}
and print NOTHING when cleared or warned.

Then prove it end to end, in front of me:
  1. pipe a fake UserPromptSubmit payload into tower_flightplan.py; show the lease row count and timing
  2. pipe a fake PreToolUse payload for a file in that lease, from a DIFFERENT session_id;
     show me the deny JSON on stdout
  3. print the measured wall time of step 2 — it must be under 200 ms

Then I will run two real agent sessions in two terminals and you will fix whatever breaks.
```

**When the real deny appears in a real session: STOP. Record the screen immediately.** That recording is
your submission's fallback asset and it is required by the guide.

### Prompt D — 11:00 · Databricks (40 min)
```
Databricks Free Edition, credentials in my shell as DATABRICKS_HOST / DATABRICKS_HTTP_PATH /
DATABRICKS_TOKEN. Never print the token, never write it to a file in this repo.

1. notebooks/01_bootstrap.sql: CREATE SCHEMA workspace.tower + tables squawks, flights, leases, cochange.
2. notebooks/02_cochange.py: from `git log -300 --name-only --pretty=format:%H` on THIS repo, build
   file-pair co-change counts, prior = pair_count / min(a_count, b_count) clipped to [0,1],
   keep pairs with count >= 2, insert into workspace.tower.cochange. Run it and show me the top 10 pairs.
3. tower/prior.py: refresh() pulls cochange into the local prior_cache table. Call it at the start of
   file_flight(). If Databricks is unreachable, prior stays 0.0 and everything still works.
4. tower/sync.py: drains outbox into Delta with batched INSERTs. scripts/sync_loop.sh runs it every 10s.
5. app/app.py: a 30-line Streamlit radar — active flights, squawk stream, averted count. Read from
   Delta, with TOWER_SOURCE=local falling back to SQLite. Deploy it NOW:
     databricks apps create tower-radar
     databricks sync ./app /Workspace/Users/<me>/tower-radar
     databricks apps deploy tower-radar --source-code-path /Workspace/Users/<me>/tower-radar
   Attach the SQL warehouse as an app resource and grant the app's service principal SELECT.

Acceptance: re-run the live deny and show me a deny message whose prior came from Databricks, plus
SELECT * FROM workspace.tower.squawks ORDER BY ts DESC LIMIT 3.
```

If 11:20 arrives and the deny still isn't working, **cut steps 3–5**: build only the `cochange` table,
screenshot it, and keep prior at 0.0. Structural score alone still denies at hops 0.

### Prompt F — 11:40 · Freeze + write ahead (20 min)
```
Freeze for the Curveball.

1. Run the tests, save output to evidence/tests-pre-noon.txt.
2. Write STATE.md: intent, architecture in 10 lines, DONE, UNRESOLVED, top 3 risks, where a fresh
   session should start reading.
3. Write BUILDATHON.md now, using exactly these headings, filling every section that cannot change at noon:
   # TOWER / ## One-sentence summary / ## Problem, intended user and why it matters /
   ## Selected Entire track and why Entire is essential / ## Architecture and main workflow /
   ## Entire Graph findings and verification / ## Noon Curveball: what changed and how we adapted /
   ## Checkpoint links and what each checkpoint proves / ## Setup, run and test instructions /
   ## Databricks use, data sources and limitations / ## Known limitations and next steps
   Leave the Curveball section as a stub. Be blunt in "why Entire is essential": without the graph
   there is no airspace, without checkpoints there is no intent to hand over.
4. Run /entire:session-handoff and commit the result as HANDOFF.md.
5. git commit, git tag pre-noon-stable, push. Print the SHA. This commit is CHECKPOINT 2.
```

Then **close the session.**

### Prompt X — 12:00 · Fresh session
```
You are joining mid-flight with no context. Read HANDOFF.md, STATE.md, TOWER_V2_NO_HOMEWORK.md,
ARCHITECTURE.md, policy.yaml. Summarise back in 8 lines. Do not edit anything.

The constraint is: <PASTE THE CURVEBALL>

Before writing code: run `entire graph impact` and `entire graph search` on the symbols it touches.
Tell me the blast radius, which of our seams it lands in, and whether policy.yaml alone can express it.
Save that output to evidence/curveball-impact.txt and wait for my go-ahead.
```
Then, after you approve: *implement the smallest complete response, keep every test green, add a test for
the new behaviour, commit as CHECKPOINT 3.*

### Prompt E — 14:00 · Evidence (20 min)
```
1. entire graph diff pre-noon-stable..HEAD   -> evidence/semantic-diff.txt   (this is mandatory evidence)
2. Full test run -> evidence/tests-final.txt
3. /entire:review on the branch -> evidence/review.md
4. Collect: the deny recording, a radar screenshot, the cochange top-10, the curveball impact output.
5. Print a 10-line summary: what changed after noon, what stayed intact, how it was verified.
```

### Prompt W — 14:20 · Finish and submit (25 min)
```
Finish BUILDATHON.md: fill the Curveball section, the graph findings section (all three evidences with
file paths), checkpoint links with one line each on what each proves, and Databricks provenance —
this repo's own git history and checkpoints, permitted use, file-level co-change stated as an
approximation, nothing mocked without a label.

Commit as CHECKPOINT 4. Print: final SHA, fork URL, mirror URL, the four checkpoint links, app URL,
and a checkbox list of every submission field so I can fill the form in one pass.
```

---

## If you fall behind — cut in this exact order

1. The Databricks App deployment (keep the tables + a screenshot).
2. The collision prior (`prior = 0.0`; structural alone still denies at hops 0).
3. The `warned` tier — deny or clear, nothing in between.
4. The `via` chain in the deny message — show `target ← their_symbol` without the intermediate.
5. **Never cut:** the live deny, the four checkpoints, the three graph evidences, BUILDATHON.md.

---

## What actually wins this

Four teams will build a nicer dashboard than you. The judging line that decides it is
*"a developer or agent completes a useful task more accurately than they could from a Git diff alone."*

So in the demo, show the **counterfactual** for ten seconds: run the same collision with TOWER disabled,
let the second agent happily overwrite the first agent's contract, then re-enable and show the deny.
Git saw nothing wrong in either run. That contrast is the whole argument, and almost nobody will show it.

# TOWER

**Air traffic control for coding agents.**
BTW Buildathon 2026 · Track 02 (Graph Intelligence) · solo build.

> Status markers: sections marked **[PENDING]** are filled after the noon Curveball and final
> verification. Everything else is complete and verified as written.

---

## One-sentence summary

TOWER resolves each agent's prompt through the Entire Graph into a **lease** over the symbols it will
plausibly touch, and denies a second agent's edit inside or near that lease *at its own PreToolUse
hook* — returning the first session's intent so it can reroute instead of clobbering.

---

## Problem, intended user and why it matters

Multiple coding agents now work the same repository in parallel — two terminals, background agents, CI
agents. None of them can see each other.

The failure is not the one people expect. A merge conflict is the *lucky* case: git noticed. The real
damage is two agents editing **different files** where one calls the other. Agent A changes a
function's ranking behaviour; Agent B, ten minutes later, edits one of its callers on the assumption
that the old behaviour holds. Both edits apply cleanly. Git reports nothing. The break surfaces hours
later, and the reasoning that caused it is gone.

**Intended user:** a developer running more than one coding agent against one repository — which is
now the normal way to use them, not an edge case.

**Why it matters:** every existing safeguard is *post-hoc*. Code review, CI and merge conflicts all
run after both agents have already written. TOWER is the only point in that pipeline where the second
edit can still be stopped, and the only one where the first agent's *intent* still exists to hand over.

We do not resolve conflicts. We maintain separation. That restraint is deliberate: an auto-merge would
be a guess, and a wrong guess here is worse than a stop.

---

## Selected Entire track and why Entire is essential

**Track 02 — Graph Intelligence.**

This is not a project that uses Entire as a convenience. Remove either half and there is no product:

**Without the Graph, there is no airspace.** A lease has to be a set of *symbols with call-graph
distances*, because the dangerous collisions are the ones that span files. A file-name comparison —
"you're both editing search.go" — catches the cases git would have caught anyway and misses every case
that actually hurts. `entire graph impact --depth 2` is what turns a prompt into a blast radius. On
this repo it yields 11,626 symbols and 57,443 relations; nothing we could build in a day approximates
that.

**Without Checkpoints, there is no handoff.** A deny that says "blocked, try later" is an obstruction.
A deny that says *"another session is changing how SearchRepository ranks results — here are two things
you can do instead"* is a routing decision. That intent comes from Entire's session record. Without
it, TOWER is a lock; with it, TOWER is a controller.

The demo makes this concrete by guarding **the Entire Graph's own source** — TOWER uses the graph to
protect the repository the graph is built from.

---

## Architecture and main workflow

Five files matter. There is **no daemon, no server, no port, no thread pool, and no graph traversal of
our own**.

```
UserPromptSubmit ──► hooks/tower_flightplan.py
                     prompt ──► entire graph search   (--head --profile full)
                            ──► entire graph impact   (--depth 2, per seed)
                            ──► lease rows into SQLite (symbol_id, path, lines, hops, via)

PreToolUse       ──► hooks/tower_separation.py        [Edit|Write|MultiEdit]
   Edit(file) ──► pure SQL against OTHER sessions' unexpired leases
              ──► lowest hops wins ──► score ──► squawk
              ──► deny JSON, or print nothing

SessionEnd       ──► hooks/tower_session.py           release leases

tower/core.py    store + adapter + scoring, one module
tower/sync.py    drains an outbox table to Delta, out of band
app/app.py       Streamlit radar
```

**The load-bearing design decision** is that all expensive work happens once per *prompt*, never per
*edit*. The flight plan pays the graph cost at UserPromptSubmit. The separation check that runs before
every single edit touches nothing but SQLite — no subprocess, no network.

### Scoring

```
structural = {0: 1.00, 1: 0.60, 2: 0.35}[hops]      # else 0.0
score      = 0.65 * structural + 0.35 * prior       # prior = co-change, from Databricks
denied  if score >= 0.55
warned  if score >= 0.30
cleared otherwise
```

Exemptions checked first: exempt globs, self-owned leases, no other active lease. **Any exception at
all resolves to `cleared`** — a hook that wedges the user's agent is worse than no TOWER.

---

## Entire Graph findings and verification

### Evidence 1 — recon (`scripts/recon.sh`, `NOTES.md`, `fixtures/`, commit `ea248e2c`)

We assumed nothing about the CLI. Every command shape in the original spec was a guess, and **every
one of them was wrong**:

| Function | Spec guessed | Reality |
|---|---|---|
| `capabilities` | `graph capabilities` | needs `--json` |
| `search` | `--query --format json --top-k` | correct, plus `--repo .` |
| `def` | `--symbol <file>:<line>` | positional, no `--symbol` flag |
| `impact` | `--depth N --json` | `--format json`, not `--json` |

Three findings changed the design:

1. **Symbol identity already exists.** Every endpoint returns ids like
   `local/entire-graph:Go:internal/sem/search.go:function:SearchRepository`. The spec's invented
   `path::name` scheme was redundant; leases are keyed on Entire's own id.
2. **Hop distance lives at `callers.entries[].depth`**, not a top-level field. `callees` and
   `type_consumers` are always depth 1. This is what makes distance a SQLite lookup rather than a graph
   traversal we implement ourselves.
3. **`co_changes` entries carry no depth**, because they are file-level, not graph hops. They are
   deliberately excluded from the halo — including them would fake a structural distance *and*
   double-count the co-change signal that already appears in the `prior` term.

Search returns two line-range pairs; the lease uses `symbol_start_line`/`symbol_end_line`, not
`start_line`/`end_line`, which is only the ranked snippet region and under-leases large functions.

### Evidence 2 — impact analysis before the Curveball change **[PENDING]**

### Evidence 3 — final semantic diff **[PENDING]**

### Measured behaviour on this repo

```
index (full profile)   53.6s   646/650 files · 11,626 symbols · 57,443 relations · completeness: ok
search --head warm      6.6s
flight plan (hook)      9.2s   50 symbols leased (2 core, 48 halo)
separation check     0.5-0.8s  of which ~0.4-0.45s is bare python.exe startup
```

---

## Noon Curveball: what changed and how we adapted **[PENDING]**

---

## Checkpoint links and what each checkpoint proves

| | Commit | What it proves |
|---|---|---|
| **CP1** | `f872c0ac` | Architecture fixed before code: the repo decision with its rejected alternatives, the v2 cuts, and five open risks — one of which (Windows hook invocation) is exactly what later failed |
| — | `ea248e2c` | Graph evidence #1: real CLI shapes captured before a parser was written |
| — | `71ab979e` | `tower/core.py` — store, adapter, scoring; 9 tests green with no Entire and no Databricks |
| — | `f5f5aa17` | Hooks wired; a CUT 3 violation found and fixed (`check()` was shelling out to `entire checkpoint list` on the hot path) |
| — | `1e248467` | The bash-escaping fix, plus the counterfactual evidence it accidentally produced |
| **CP2** | **[PENDING]** | Last stable state before noon |
| **CP3** | **[PENDING]** | Curveball response |
| **CP4** | **[PENDING]** | Final implementation and verification |

---

## Setup, run and test instructions

```bash
# 1. deps (venv lives outside the repo; hooks reference it by absolute path)
python -m venv .venv
.venv/Scripts/python.exe -m pip install pyyaml requests pytest databricks-sql-connector databricks-sdk streamlit

# 2. Entire
entire enable -y --agent claude-code
entire plugin install graph
entire graph init-agents --repo .

# 3. warm the graph cache — REQUIRED, and required again after EVERY commit
bash scripts/prewarm.sh

# 4. tests (no Entire, no Databricks needed)
.venv/Scripts/python.exe -m pytest tests/ -q
```

**To see a deny:** open two Claude Code sessions in this repo. In the first, ask for a change to
`SearchRepository`'s ranking. In the second, ask it to edit `internal/sem/search.go`. The second is
denied, and the denial names what the first session is doing.

⚠️ **`scripts/prewarm.sh` must be re-run after every commit.** A commit changes the tree hash and
invalidates Entire's committed-tree cache; the next flight plan then pays the full cold index cost,
exceeds its timeout, and leases nothing. See `STATE.md`.

---

## Databricks use, data sources and limitations **[PARTIAL]**

**Data source:** this repository's own git history (`git log --name-only`), used to compute file-pair
co-change: `prior = pair_count / min(a_count, b_count)`, clipped to [0,1], pairs with count ≥ 2. This
is our own fork's public history — no third-party or personal data.

**Stated as an approximation:** co-change is measured at *file* level, while separation is measured at
*symbol* level. Two files changing together is weaker evidence than two symbols changing together. We
use it only as the 0.35-weighted term, never alone.

**Write path** never touches the request path: hooks append to a local `outbox` table, and a separate
process drains it to Delta.

**[PENDING]** — final table contents, the deployed radar, and the co-change top-10.

---

## Known limitations and next steps

1. **TOWER reasons about the committed call graph.** Adapter calls use `--head`, because working-tree
   queries are never cached and cost 17–73s each. A symbol created but not yet committed is invisible
   to the lease; it is still caught at file granularity by the `<path>::*` pseudo-symbol. This was a
   deliberate trade — the alternative was a 90–140s pause on every prompt.

2. **The separation check is ~500–800 ms, not the 200 ms originally specified.** We measured why: bare
   `python.exe` startup on this machine is ~400–450 ms before any of our code runs. TOWER's own logic
   is tens of milliseconds and touches only SQLite. Closing the gap would require a persistent
   process, which we removed on purpose as the most likely thing to be broken during a demo. The
   original spec's real budget — 800 ms end to end — is met.

3. **The co-change prior is not yet reachable at distance 0.** At hops 0 the target's file *is* the
   leased symbol's file, so the lookup degenerates to a self-pair, which co-change over distinct file
   pairs will never contain. The fix is to score against the other *distinct* core files in the same
   lease. Until then a distance-0 deny scores 0.65 on structural alone — above the 0.55 threshold, so
   it denies correctly, but below the 0.87 the calibration targets.

4. **The adapter's subprocess timeout does not reliably bound wall time on Windows.** A 20 s timeout
   was observed taking 57 s to actually kill the child. Raised to 55 s, because killing the call lost
   the data *and* the time.

5. **No auto-resolution, by design.** TOWER denies and informs. Choosing a merge on the agent's behalf
   would be a guess with no way to verify it.

### Next steps

- Symbol-level co-change instead of file-level, which would make the prior far sharper.
- Lease-quality feedback: record whether a denied session's reroute actually avoided the collision, and
  use it to tune `separation_depth`.
- A pre-warm daemon that watches for commits and re-indexes — the one place a background process is
  clearly worth it.

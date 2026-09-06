# TOWER — Build Spec
**Air traffic control for coding agents.** BTW Buildathon 2026 · Track 02 (Graph Intelligence) · solo build, 6 hours.

> Give this file to Claude Code as the source of truth. It is a specification, not code.
> Every contract below is deliberately pinned — do not redesign, do not add layers, do not
> generalise. If something here is wrong, fix it in one place and keep going.

---

## 0. One-paragraph statement of the product

Multiple coding agents work the same repo in parallel (worktrees, background agents, CI agents) with
zero awareness of each other. Collisions surface hours later as merge conflicts or silently broken
callers. TOWER makes each agent **file a flight plan** before it edits: its prompt is resolved through
the Entire Graph into the set of symbols it will plausibly touch, and that set becomes a **lease**.
When a second agent tries to edit a symbol that is leased — or sits within `separation_depth` hops of
one on the call graph — TOWER **denies the edit through the agent's own permission hook** and returns
the first session's intent (recovered from its Entire Checkpoint) as a **handoff brief**, so the second
agent reroutes instead of clobbering. Every decision is logged to Databricks; the collision likelihood
shown in the deny message is computed there from historical co-change.

**One sentence for judges:** *We don't resolve conflicts between agents. We maintain separation.*

---

## 1. Non-negotiable constraints

| Constraint | Value |
|---|---|
| Language | Python 3.11+, stdlib-first. No frameworks beyond FastAPI + Streamlit. |
| Hook path latency | **< 800 ms end to end**, always. Fail-open on any error or timeout. |
| Network on hook path | **Never.** Databricks is read from a *local cache table* only. |
| Persistence | SQLite is the source of truth at runtime. Delta is the log + the analytics brain. |
| Entire calls | Only inside `tower/entire_adapter.py`. Nowhere else. Ever. |
| Config | `policy.yaml`, re-read on every request (cheap, and it is Curveball insurance). |
| Tests | pytest, must pass in fixture mode with no Entire and no Databricks. |

---

## 2. Repository layout (create exactly this)

```
tower/
├── README.md
├── BUILDATHON.md            # submission doc, written at P7
├── NOTES.md                 # P1 recon: real Entire JSON shapes, pasted raw
├── policy.yaml
├── requirements.txt
├── .env.example             # DATABRICKS_* names only, never values
├── tower/
│   ├── __init__.py
│   ├── config.py            # policy.yaml + env loading
│   ├── models.py            # dataclasses / pydantic models
│   ├── entire_adapter.py    # THE ONLY place that shells out to `entire`
│   ├── store.py             # sqlite schema + all queries
│   ├── airspace.py          # lease building + separation algorithm (pure functions)
│   ├── prior.py             # collision prior: local cache read + refresh from Databricks
│   ├── briefing.py          # handoff brief: template first, FM API second
│   ├── sink.py              # background batched writer to Delta
│   └── daemon.py            # FastAPI app
├── hooks/
│   ├── tower_flightplan.py  # UserPromptSubmit
│   ├── tower_separation.py  # PreToolUse  (Edit|Write|MultiEdit)
│   └── tower_session.py     # SessionEnd  (release lease)
├── app/
│   ├── app.py               # Streamlit radar (deployed as a Databricks App)
│   ├── app.yaml
│   └── requirements.txt
├── notebooks/
│   ├── 01_bootstrap.sql     # catalog/schema/table DDL
│   └── 02_cochange.py       # builds the co-change prior
├── fixtures/                # written by P1 recon from REAL command output
├── scripts/
│   ├── recon.sh             # P1: capture real Entire output into fixtures/ + NOTES.md
│   ├── demo_up.sh           # start daemon + two worktrees
│   └── demo_reset.sh        # clear leases/squawks between rehearsals
└── tests/
    ├── test_airspace.py
    ├── test_adapter_fixture.py
    └── test_separation_e2e.py
```

---

## 3. Core data model

### 3.1 Symbol identity
A **symbol_id** is `"<repo-relative-path>::<name>"`, e.g. `mux.go::Router.Match`.
If a target cannot be resolved to a named symbol (new file, YAML, markdown), use the
**file pseudo-symbol** `"<path>::*"`. File pseudo-symbols only ever collide at distance 0.

### 3.2 SQLite (`~/.tower/tower.db`) — source of truth at runtime

```sql
CREATE TABLE flights (
  session_id TEXT PRIMARY KEY,
  agent TEXT, repo TEXT, branch TEXT, worktree TEXT,
  prompt TEXT, intent TEXT,          -- intent = short summary of prompt
  filed_at REAL, last_seen REAL,
  status TEXT                        -- active | released | expired
);

CREATE TABLE leases (
  lease_id TEXT PRIMARY KEY,         -- uuid4 hex
  session_id TEXT, symbol_id TEXT,
  kind TEXT,                         -- core | halo
  hops INTEGER,                      -- 0 for core, 1..n for halo
  path TEXT,                         -- file path, for prior lookup
  created_at REAL, expires_at REAL, released_at REAL
);
CREATE INDEX idx_lease_symbol ON leases(symbol_id);

CREATE TABLE squawks (
  squawk_id TEXT PRIMARY KEY,        -- uuid4 hex, 4 digits shown in UI
  ts REAL, session_id TEXT,
  tool TEXT, file_path TEXT, line INTEGER,
  target_symbol TEXT,
  decision TEXT,                     -- cleared | warned | denied
  score REAL, structural REAL, prior REAL, distance INTEGER,
  other_session_id TEXT, other_intent TEXT,
  path_json TEXT,                    -- the graph path proving the conflict
  latency_ms INTEGER,
  rerouted INTEGER DEFAULT 0         -- set to 1 when the denied session next edits elsewhere
);

CREATE TABLE prior_cache (           -- refreshed FROM Databricks, read on hook path
  file_a TEXT, file_b TEXT, prior REAL, refreshed_at REAL,
  PRIMARY KEY (file_a, file_b)
);

CREATE TABLE graph_cache (           -- memoises expensive adapter calls
  key TEXT PRIMARY KEY, value TEXT, created_at REAL
);

CREATE TABLE outbox (                -- rows waiting to go to Delta
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT, payload_json TEXT, created_at REAL
);
```

### 3.3 Delta (`workspace.tower.*`) — mirrors flights / leases / squawks, plus:

```sql
cochange(file_a STRING, file_b STRING, pair_count INT, a_count INT, b_count INT, prior DOUBLE)
graph_symbols(symbol_id STRING, path STRING, name STRING, kind STRING)
graph_edges(src STRING, dst STRING, relation STRING)
```

---

## 4. `entire_adapter.py` — the only Entire surface

**Why it exists:** the real CLI output shape is unknown until 09:15 on event day. Everything else in
the codebase is written against *these six functions*, never against Entire's JSON.

```python
def search_symbols(prompt: str, top_k: int = 8) -> list[SymbolRef]
def symbol_at(file_path: str, line: int | None) -> SymbolRef | None
def impact_set(symbol_id: str, depth: int = 2) -> list[tuple[SymbolRef, int]]   # (symbol, hops)
def path_between(a: str, b: str, max_depth: int = 2) -> list[str] | None        # symbol_ids, a..b
def session_intent(session_id: str) -> str | None      # from Entire checkpoints
def export_graph(dest_dir: str) -> dict                # writes symbols.ndjson / edges.ndjson
```

`SymbolRef = {symbol_id, path, name, line_start, line_end, kind}`

Rules:
1. Each function shells out with `subprocess.run(..., timeout=cfg.adapter_timeout)` and parses JSON.
2. **Every function is wrapped in try/except and returns an empty/None result on failure.** No raising.
3. Results are memoised in `graph_cache` keyed by `sha1(cmd)`, TTL `cfg.cache_ttl_s` (600).
4. `TOWER_FIXTURES=1` makes every function read `fixtures/<fn>.json` instead of shelling out.
   All tests run in this mode. The radar demo can run in this mode if Entire misbehaves.
5. Candidate commands (VERIFY IN P1, then correct here and only here):
   - `entire graph search --query "<q>" --format json --top-k N`
   - `entire graph def --symbol <path:line> --json`
   - `entire graph impact --symbol <name> --depth N --json`
   - `entire graph neighbors --symbol <name> --direction both --depth N --json`
   - `entire graph symbols` / `entire graph edges`  (NDJSON on stdout)
   - `entire checkpoint list --json` and `entire checkpoint explain <id> --short`

`path_between` is implemented **locally**, not by Entire: build an adjacency map once per repo from
`entire graph edges` (cached), then BFS to `max_depth`. This is faster and gives you the actual
path list to print in the deny message.

---

## 5. The separation algorithm (`airspace.py` — pure functions, unit tested)

### 5.1 Filing a flight plan
```
seeds  = search_symbols(prompt, top_k=policy.seed_top_k)          # 8
core   = {s.symbol_id for s in seeds}
halo   = {}
for s in seeds:
    for sym, hops in impact_set(s.symbol_id, depth=policy.lease_depth):   # 2
        if sym.symbol_id not in core:
            halo[sym.symbol_id] = min(hops, halo.get(sym.symbol_id, 99))
store lease rows: core (hops=0) + halo (hops=n), expires_at = now + policy.lease_ttl_minutes*60
```
Cap the lease at `policy.max_lease_symbols` (400) — keep the lowest-hop symbols. A lease that covers
the whole repo is useless and slow.

### 5.2 Checking an edit
```
target = symbol_at(file_path, line) or file_pseudo_symbol(file_path)

for each active lease L of another session (not expired, not released):
    if target in L.core:                  distance = 0
    elif target in L.halo:                distance = L.halo[target]
    else:                                 distance = len(path_between(target, nearest core, depth)) - 1
                                          (None if no path within policy.separation_depth)

structural = {0: 1.00, 1: 0.60, 2: 0.35}.get(distance, 0.0)
prior      = prior_cache lookup for (file(target), file(other core symbol)), default 0.0
score      = round(0.65*structural + 0.35*prior, 2)

decision   = "denied"  if score >= policy.deny_threshold   (0.55)
             "warned"  if score >= policy.warn_threshold   (0.30)
             "cleared" otherwise
```
Take the **highest-scoring conflict** if several leases match. Ties break by earliest lease.

> **Calibration note:** distance 0 with a co-change prior of 0.63 gives exactly **0.87** — the number
> in the pitch. Use `gorilla/mux` files that actually co-change so the demo number is real, not staged.

### 5.3 Exemptions (always clear, checked first)
- Target path matches any `policy.exempt_globs` (`**/*.md`, `**/*.lock`, `.entire/**`, `tests/**`, `tower/**`)
- The editing session **owns** the lease (same `session_id`)
- No other active lease exists
- Anything at all went wrong → `cleared` (fail-open), logged with `decision="cleared"` and a reason

---

## 6. Hook contracts (exact — do not improvise)

Hooks are **stdlib only** (`json`, `sys`, `urllib.request`), 1-second timeout, and they exit 0 on any
error. A broken venv must never wedge the user's agent.

### 6.1 `hooks/tower_separation.py` — PreToolUse on `Edit|Write|MultiEdit`
Reads stdin JSON: `session_id`, `cwd`, `hook_event_name`, `tool_name`, `tool_input` (`.file_path`).
POSTs to `http://127.0.0.1:8765/separation`.

**On deny — print exactly this and exit 0:**
```json
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",
"permissionDecisionReason":"<the handoff brief, plain text, <= 1500 chars>"}}
```
**On clear or warn — print nothing, exit 0.** (Do *not* return `"allow"`: that would bypass the
user's own permission prompts. A warn is logged and surfaced on the radar, not in the agent.)

### 6.2 `hooks/tower_flightplan.py` — UserPromptSubmit
POSTs the prompt to `/flightplan`, then prints:
```json
{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit",
"additionalContext":"TOWER: flight plan filed. You hold a lease on 23 symbols around Router.Match. 1 other session is active in this repo."}}
```
This is worth demo points: the agent visibly *knows* it is in controlled airspace.

### 6.3 `hooks/tower_session.py` — SessionEnd → POST `/release`. Print nothing.

### 6.4 `.claude/settings.json` registration
```json
{
  "hooks": {
    "UserPromptSubmit": [
      {"hooks": [{"type": "command", "command": "python3 $CLAUDE_PROJECT_DIR/hooks/tower_flightplan.py", "timeout": 10}]}
    ],
    "PreToolUse": [
      {"matcher": "Edit|Write|MultiEdit",
       "hooks": [{"type": "command", "command": "python3 $CLAUDE_PROJECT_DIR/hooks/tower_separation.py", "timeout": 10}]}
    ],
    "SessionEnd": [
      {"hooks": [{"type": "command", "command": "python3 $CLAUDE_PROJECT_DIR/hooks/tower_session.py", "timeout": 10}]}
    ]
  }
}
```
Entire installs its own hooks in the same file. **Merge, never overwrite** — multiple hook commands
can sit on one event, they all run, and the most restrictive permission decision wins.

---

## 7. Daemon API (`127.0.0.1:8765`)

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/flightplan` | `session_id, agent, repo, branch, prompt, cwd` | `{lease_id, core, halo_count, others_active}` |
| POST | `/separation` | `session_id, tool, file_path, line?` | `{decision, score, reason, evidence}` |
| POST | `/release` | `session_id` | `{released: n}` |
| GET | `/state` | – | full radar payload (flights, leases, last 50 squawks) |
| GET | `/health` | – | `{ok, entire: bool, databricks: bool, fixtures: bool}` |

Run with `uvicorn tower.daemon:app --port 8765`. Single process, no reload in demo mode.

---

## 8. `briefing.py` — the handoff brief

The deny message is the product. Build it in two stages so the demo never depends on a network call.

**Stage 1 (template, always available):**
```
⛔ DENIED — separation conflict

  symbol        {target.name}            {target.path}:{lines}
  leased by     {other_session_short}    session started {t}, {n} turns
  their intent  "{other_intent}"
  graph path    {a} ← {b} ← {c}          depth {d}
  prior         P(collide) = {score}     co-changed {k}/{n} commits · databricks

  handoff brief
  {two concrete options}

  cleared alternatives: {up to 3 unleased symbols from your own lease}
```

**Stage 2 (Foundation Model API, if it responds in < 2 s):** rewrite the `handoff brief` paragraph only.
System prompt: *"You are an air-traffic controller for coding agents. In at most 60 words, tell the
blocked agent what the other session is doing, why it conflicts, and give exactly two concrete options.
No preamble."* Cache per `(session, target)` so a retry is instant. Any failure → keep the template.

---

## 9. Databricks integration (`sink.py`, `prior.py`)

- **Write path:** hooks and daemon append rows to `outbox`. A daemon background thread drains it every
  5 s over `databricks-sql-connector` with batched `INSERT INTO ... VALUES`. Failures are logged and
  retried; **nothing on the request path ever waits for this.**
- **Read path:** `prior.refresh()` runs at daemon start and every 10 minutes: one query against
  `workspace.tower.cochange`, results written into `prior_cache`. The hook path only reads SQLite.
- **Bulk:** after `entire graph symbols/edges`, upload the NDJSON to a volume and `COPY INTO` the
  `graph_symbols` / `graph_edges` tables (see DATABRICKS_SETUP.md §B3).

---

## 10. Radar (`app/app.py`, Streamlit on Databricks Apps)

Single page, `st.autorefresh` every 5 s, four blocks top to bottom:

1. **Header strip** — active flights, symbols leased, squawks today, **collisions averted** (big number).
2. **Active flights** — table: session (short id), agent, branch, intent, lease size, age.
3. **Squawk stream** — reverse chronological, colour by decision, one row per squawk.
4. **Drill-down** — select a squawk → target symbol, the graph path as `a ← b ← c`, structural vs
   prior contribution to the score, the brief that was returned.

Data source: SQL warehouse. Add `TOWER_SOURCE=local` to read the SQLite file directly instead — that
is your demo insurance if the warehouse stalls.

---

## 11. `policy.yaml` (Curveball insurance — express rules here, not in code)

```yaml
separation_depth: 2          # max graph hops that still counts as a conflict
lease_depth: 2               # impact depth used when filing a flight plan
seed_top_k: 8
max_lease_symbols: 400
lease_ttl_minutes: 20
deny_threshold: 0.55
warn_threshold: 0.30
weights: {structural: 0.65, prior: 0.35}
exempt_globs: ["**/*.md", "**/*.lock", ".entire/**", "tests/**", "tower/**", "hooks/**"]
fail_open: true
adapter_timeout_s: 1.5
cache_ttl_s: 600
brief_model: databricks-claude-haiku-4-5      # confirm what your workspace actually lists
```

---

## 12. Acceptance criteria (this is the definition of done, phase by phase)

| Phase | Done when |
|---|---|
| **P1 Recon** | `fixtures/*.json` contain **real** output from 4 graph commands; `NOTES.md` lists the actual field names; `entire_adapter.py` parses them; `pytest tests/test_adapter_fixture.py` passes. |
| **P2a Daemon** | `curl -XPOST /separation` with a hand-written body returns a deny JSON. The hook script prints valid deny JSON when fed the same body on stdin. |
| **P2b Flight plan** | POSTing a prompt creates ≥ 20 lease rows in SQLite in < 3 s. |
| **P2c Separation** | Two real agent sessions in two worktrees: session B's `Edit` is **denied**, and the reason contains a real graph path and session A's real intent. **This is the product — do not proceed past it for any reason.** |
| **P3 Databricks** | `squawks` rows appear in `workspace.tower.squawks`; `prior_cache` is non-empty; the deny message shows a prior that came from Databricks. |
| **P4 Freeze** | `git tag pre-noon-stable` pushed; `STATE.md` + `HANDOFF.md` committed; tests green. |
| **P5 Curveball** | Impact assessment captured *before* editing; constraint implemented; `git diff pre-noon-stable..HEAD` is explainable in 30 s. |
| **P6 Radar** | App URL loads, shows the averted-collision count, drill-down renders a real graph path. GIF of the deny recorded. |
| **P7 Ship** | BUILDATHON.md complete, final SHA noted, mirror pushed with checkpoint links, two clean rehearsals. |

---

## 13. Explicit non-goals (say no to all of these)

- No auth, no multi-user, no accounts, no Docker, no CI, no migrations framework.
- No ORM. Raw `sqlite3` with parameterised SQL.
- No React. Streamlit only.
- No embeddings / vector search unless P1–P6 are all green and the clock says before 14:15.
- No git hooks (Entire owns those). No editing of files inside `.entire/`.
- No attempt to *auto-resolve* a conflict. TOWER denies and informs. That restraint is the product.

---

## 14. Failure modes to defend against (write the defence, don't just note it)

| Failure | Required behaviour |
|---|---|
| `entire` not installed / command differs | Adapter returns empty → every check clears → agents work normally. Log once. |
| Graph call slow | 1.5 s subprocess timeout, cached results, deny path never exceeds 800 ms. |
| Daemon down | Hook's `urlopen` times out at 1 s → prints nothing → agent proceeds. |
| Databricks down | `prior = 0.0`, structural score alone still denies at distance 0 (0.65 ≥ 0.55). Radar switches to `TOWER_SOURCE=local`. |
| Two sessions, same worktree | `session_id` differs → still works. Leases are per session, not per branch. |
| Lease never released | TTL expiry sweep every 60 s in the daemon. |

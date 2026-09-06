"""TOWER core: SQLite store + the only `entire` adapter surface + the separation algorithm.

One module, no server, no threads (see ARCHITECTURE.md, "Rejected options"). Every `entire` invocation
lives in this file and nowhere else. Every adapter function fails open: on any error it returns an
empty/None result rather than raising, so a hook built on top of this module can always `sys.exit(0)`.

Shapes parsed here are the REAL ones captured in NOTES.md, not the design sketch's pre-recon
guesses -- see NOTES.md if a field name here looks surprising.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "fixtures"
POLICY_PATH = REPO_ROOT / "policy.yaml"

# Path.home() handles the space in this machine's profile path correctly; never build this by
# string concatenation (see ARCHITECTURE.md "Verified on this machine"). TOWER_DB_PATH exists only
# so tests don't write into the real ~/.tower/tower.db.
DB_PATH = Path(os.environ.get("TOWER_DB_PATH", "")) if os.environ.get("TOWER_DB_PATH") else (Path.home() / ".tower" / "tower.db")

STRUCTURAL_BY_HOPS = {0: 1.00, 1: 0.60, 2: 0.35}

# --------------------------------------------------------------------------------------
# Evidence quality (noon Curveball, Track 2: "the graph is evidence, not an oracle")
#
# A static call graph cannot resolve dynamic dispatch, reflection or generated code. Entire
# reports this honestly -- `partial_failures[]`, `warnings[]`, `completeness`,
# `disambiguation_required` -- and TOWER used to discard all of it, presenting every hop as
# settled fact.
#
# The direction of the correction matters and is easy to get backwards: a MISSING graph edge is a
# collision TOWER cannot see. So when analysis is partial, "no path found" stops being evidence of
# safety. Partial evidence therefore makes TOWER MORE conservative, never less -- it can raise a
# `cleared` to `warned`, and it must never downgrade a `denied`.
# --------------------------------------------------------------------------------------
EVIDENCE_CONFIRMED = "confirmed"    # graph fully resolved for this file
EVIDENCE_PARTIAL = "partial"        # file implicated in partial_failures / degraded / ambiguous
EVIDENCE_UNVERIFIED = "unverified"  # no resolved symbol; needs source or test verification

EVIDENCE_RANK = {EVIDENCE_CONFIRMED: 0, EVIDENCE_PARTIAL: 1, EVIDENCE_UNVERIFIED: 2}
DECISION_RANK = {"cleared": 0, "warned": 1, "denied": 2}

DEFAULT_POLICY = {
    "separation_depth": 2,
    "lease_depth": 2,
    "seed_top_k": 3,
    "max_lease_symbols": 300,
    "lease_ttl_minutes": 20,
    "deny_threshold": 0.55,
    "warn_threshold": 0.30,
    "weights": {"structural": 0.65, "prior": 0.35},
    "exempt_globs": ["**/*.md", "**/*.lock", ".entire/**", "tests/**", "tower/**", "hooks/**"],
    "fail_open": True,
    "adapter_timeout_s": 20,
    "cache_ttl_s": 600,
    # Curveball: the weakest decision allowed when graph evidence for the target file is
    # partial or unverified. Detection lives in the adapter (code); this is the behaviour knob.
    "partial_evidence_floor": "warned",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS flights (
  session_id TEXT PRIMARY KEY,
  agent TEXT, repo TEXT, branch TEXT, worktree TEXT,
  prompt TEXT, intent TEXT,
  filed_at REAL, last_seen REAL,
  status TEXT
);
CREATE TABLE IF NOT EXISTS leases (
  lease_id TEXT PRIMARY KEY,
  session_id TEXT, symbol_id TEXT,
  kind TEXT, hops INTEGER,
  path TEXT, line_start INTEGER, line_end INTEGER, via TEXT,
  evidence TEXT DEFAULT 'confirmed', verify_command TEXT, evidence_note TEXT,
  created_at REAL, expires_at REAL, released_at REAL
);
CREATE INDEX IF NOT EXISTS idx_lease_symbol ON leases(symbol_id);
CREATE INDEX IF NOT EXISTS idx_lease_path ON leases(path);
CREATE INDEX IF NOT EXISTS idx_lease_session ON leases(session_id);
CREATE TABLE IF NOT EXISTS squawks (
  squawk_id TEXT PRIMARY KEY,
  ts REAL, session_id TEXT,
  tool TEXT, file_path TEXT, line INTEGER,
  target_symbol TEXT,
  decision TEXT,
  score REAL, structural REAL, prior REAL, distance INTEGER,
  other_session_id TEXT, other_intent TEXT,
  path_json TEXT,
  evidence TEXT DEFAULT 'confirmed',
  latency_ms INTEGER,
  rerouted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS prior_cache (
  file_a TEXT, file_b TEXT, prior REAL, refreshed_at REAL,
  PRIMARY KEY (file_a, file_b)
);
CREATE TABLE IF NOT EXISTS graph_cache (
  key TEXT PRIMARY KEY, value TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT, payload_json TEXT, created_at REAL
);
"""


@dataclass
class SymbolRef:
    symbol_id: str
    path: str
    name: str
    line_start: Optional[int]
    line_end: Optional[int]
    kind: str
    evidence: str = EVIDENCE_CONFIRMED
    verify_command: Optional[str] = None
    evidence_note: str = ""


@dataclass
class FlightResult:
    session_id: str
    core_count: int
    halo_count: int
    total: int
    capped: bool
    partial_count: int = 0


@dataclass
class Decision:
    decision: str  # cleared | warned | denied
    score: float
    structural: float
    prior: float
    hops: Optional[int]
    other_session: Optional[str]
    other_intent: Optional[str]
    via: Optional[str]
    target_path: str
    symbol_id: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    reason: str = ""
    evidence: str = EVIDENCE_CONFIRMED
    verify_command: Optional[str] = None
    evidence_note: str = ""


# --------------------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------------------

_CONNECTIONS: dict[str, sqlite3.Connection] = {}


def get_connection() -> sqlite3.Connection:
    key = str(DB_PATH)
    conn = _CONNECTIONS.get(key)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate_evidence_columns(conn)
        conn.commit()
        _CONNECTIONS[key] = conn
    return conn


def _migrate_evidence_columns(conn: sqlite3.Connection) -> None:
    # Curveball landed after a real ~/.tower/tower.db already existed on this machine; CREATE
    # TABLE IF NOT EXISTS does not add columns to a table that's already there, so a pre-Curveball
    # DB needs these added by hand. Each ALTER is caught independently -- idempotent either way.
    for stmt in (
        "ALTER TABLE leases ADD COLUMN evidence TEXT DEFAULT 'confirmed'",
        "ALTER TABLE leases ADD COLUMN verify_command TEXT",
        "ALTER TABLE leases ADD COLUMN evidence_note TEXT",
        "ALTER TABLE squawks ADD COLUMN evidence TEXT DEFAULT 'confirmed'",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists


def _load_policy() -> dict:
    # Re-read every call -- cheap, and it's Curveball insurance (CLAUDE.md).
    try:
        with open(POLICY_PATH, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
    except OSError:
        loaded = {}
    policy = dict(DEFAULT_POLICY)
    policy.update(loaded)
    return policy


def _fixtures_enabled() -> bool:
    return os.environ.get("TOWER_FIXTURES") == "1"


_FIXTURE_CACHE: dict[str, object] = {}


def _read_fixture(name: str):
    if name in _FIXTURE_CACHE:
        return _FIXTURE_CACHE[name]
    try:
        with open(FIXTURES_DIR / name, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = None
    _FIXTURE_CACHE[name] = data
    return data


def _cache_get(conn: sqlite3.Connection, key: str, ttl: float):
    row = conn.execute("SELECT value, created_at FROM graph_cache WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    if (time.time() - row["created_at"]) >= ttl:
        return None
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return None


def _cache_set(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO graph_cache(key, value, created_at) VALUES (?, ?, ?)",
        (key, json.dumps(value), time.time()),
    )
    conn.commit()


def _run_entire(args: list[str], repo: str, timeout: float):
    try:
        proc = subprocess.run(args, cwd=repo, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _run_entire_cached(args: list[str], repo: str, timeout: float):
    policy = _load_policy()
    ttl = float(policy.get("cache_ttl_s", 600))
    conn = get_connection()
    key = hashlib.sha1(" ".join(args + [repo]).encode("utf-8")).hexdigest()
    cached = _cache_get(conn, key, ttl)
    if cached is not None:
        return cached
    data = _run_entire(args, repo, timeout)
    if data is not None:
        _cache_set(conn, key, data)
    return data


def _glob_match(path: str, pattern: str) -> bool:
    # fnmatch has no path-awareness: "**" and "*" both translate to ".*", so a leading "**/"
    # (gitignore's "any depth, including the repo root") still requires a literal "/" in the
    # candidate and fails to match a bare top-level file like "README.md". Fall back to the
    # pattern with that prefix stripped so the root case matches too.
    if fnmatch.fnmatch(path, pattern):
        return True
    if pattern.startswith("**/"):
        return fnmatch.fnmatch(path, pattern[3:])
    return False


def _normalize_path(file_path: str, repo: str) -> str:
    p = Path(file_path)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(Path(repo).resolve())
        except ValueError:
            pass
    return str(p).replace("\\", "/")


# --------------------------------------------------------------------------------------
# adapter -- the only functions that shell out to `entire`
# --------------------------------------------------------------------------------------


def _evidence_context(data: dict) -> dict:
    """One evidence context per Entire response -- computed once, applied per file/symbol below.
    This is the only place `completeness`/`stats.completeness_level`/`partial_failures`/`warnings`/
    `disambiguation_required` are read; every caller below goes through `_evidence_for()`, never
    back at the raw response."""
    stats = data.get("stats") or {}
    completeness_level = stats.get("completeness_level")
    if completeness_level is None:
        completeness_level = (data.get("completeness_scope") or {}).get("level")
    partial_by_file = {
        pf.get("file_path"): pf.get("code")
        for pf in (data.get("partial_failures") or [])
        if pf.get("file_path")
    }
    return {
        "partial_by_file": partial_by_file,
        "disambiguation_required": bool(data.get("disambiguation_required")),
        "degraded": completeness_level not in (None, "ok"),
    }


def _evidence_for(file_path: str, symbol_id: Optional[str], ctx: dict) -> tuple[str, str]:
    """Per-file/symbol evidence tier -- see EVIDENCE_CONFIRMED/PARTIAL/UNVERIFIED above.

    Graph silence is not proof of safety: an unresolved symbol or an ambiguous query is always
    UNVERIFIED, never CONFIRMED, regardless of what else the response reports.
    """
    if not symbol_id or ctx["disambiguation_required"]:
        return EVIDENCE_UNVERIFIED, "no symbol resolved -- file-level match only"
    code = ctx["partial_by_file"].get(file_path)
    if code:
        return EVIDENCE_PARTIAL, f"graph reported {code} on {file_path}; this relationship may be incomplete"
    if ctx["degraded"]:
        return EVIDENCE_PARTIAL, "graph completeness for this query was degraded"
    return EVIDENCE_CONFIRMED, ""


def search_symbols(prompt: str, top_k: Optional[int] = None, repo: str = ".") -> list[SymbolRef]:
    """`entire graph search --repo <repo> --head --profile full --query <prompt> --top-k N --format json`"""
    try:
        policy = _load_policy()
        top_k = top_k or int(policy.get("seed_top_k", 3))
        if _fixtures_enabled():
            data = _read_fixture("search.json")
        else:
            args = [
                "entire", "graph", "search", "--repo", repo, "--head", "--profile", "full",
                "--query", prompt, "--top-k", str(top_k), "--format", "json",
            ]
            data = _run_entire_cached(args, repo, timeout=float(policy.get("adapter_timeout_s", 20)))
        if not data:
            return []
        ctx = _evidence_context(data)
        # Search's own verify_command is the narrowest test Entire has for this query -- never
        # invent one; impact responses carry none of their own (recon-verified, see NOTES.md), so
        # file_flight() inherits this onto halo symbols that came from the same seed.
        verify_command = ((data.get("verify_command") or {}).get("command")) or None
        out: list[SymbolRef] = []
        for r in data.get("results", []) or []:
            symbol_id = r.get("symbol_id")
            if not symbol_id:
                # related/covering-test/literal-cluster entries carry no symbol_id -- not
                # resolvable to a leaseable symbol, skip them.
                continue
            file_path = r.get("file_path", "")
            evidence, evidence_note = _evidence_for(file_path, symbol_id, ctx)
            out.append(SymbolRef(
                symbol_id=symbol_id,
                path=file_path,
                name=r.get("symbol_name") or r.get("qualified_name") or "",
                line_start=r.get("symbol_start_line", r.get("start_line")),
                line_end=r.get("symbol_end_line", r.get("end_line")),
                kind=r.get("kind", ""),
                evidence=evidence,
                verify_command=verify_command,
                evidence_note=evidence_note,
            ))
        return out[:top_k]
    except Exception:
        return []


def impact_set(symbol: SymbolRef, depth: Optional[int] = None, repo: str = ".") -> list[tuple[SymbolRef, int]]:
    """`entire graph impact --repo <repo> --head --profile full --symbol <path>:<line> --depth N --format json`

    `impact --symbol` only documents NAME|<file>:<line> selectors, not Entire's own compound `id`
    strings, so this resolves via the symbol's own file:line (exactly the form recon verified) rather
    than replaying `symbol.symbol_id` back at the CLI.
    """
    try:
        policy = _load_policy()
        depth = depth or int(policy.get("lease_depth", 2))
        if _fixtures_enabled():
            data = _read_fixture("impact.json")
        else:
            if not symbol.path or symbol.line_start is None:
                return []
            selector = f"{symbol.path}:{symbol.line_start}"
            args = [
                "entire", "graph", "impact", "--repo", repo, "--head", "--profile", "full",
                "--symbol", selector, "--depth", str(depth), "--format", "json",
            ]
            data = _run_entire_cached(args, repo, timeout=float(policy.get("adapter_timeout_s", 20)))
        if not data:
            return []
        ctx = _evidence_context(data)
        out: list[tuple[SymbolRef, int]] = []
        # co_changes is deliberately excluded: it is file-level co-edit history, not a graph hop --
        # folding it into the halo would fake a structural distance and double-count the prior term.
        for section in ("callers", "callees", "type_consumers"):
            entries = (data.get(section) or {}).get("entries", []) or []
            for entry in entries:
                endpoint = entry.get("endpoint") or {}
                symbol_id = endpoint.get("id")
                if not symbol_id:
                    continue
                file_path = endpoint.get("file_path", "")
                hops = entry.get("depth", 1)
                evidence, evidence_note = _evidence_for(file_path, symbol_id, ctx)
                out.append((SymbolRef(
                    symbol_id=symbol_id,
                    path=file_path,
                    name=endpoint.get("name") or endpoint.get("qualified_name") or "",
                    line_start=endpoint.get("start_line"),
                    line_end=endpoint.get("end_line"),
                    kind=endpoint.get("kind", ""),
                    evidence=evidence,
                    evidence_note=evidence_note,
                ), hops))
        return out
    except Exception:
        return []


def session_intent(session_id: str, repo: str = ".") -> Optional[str]:
    """`entire checkpoint list --json --session <session_id>`"""
    try:
        if _fixtures_enabled():
            data = _read_fixture("checkpoints.json")
        else:
            policy = _load_policy()
            args = ["entire", "checkpoint", "list", "--json", "--session", session_id]
            data = _run_entire_cached(args, repo, timeout=float(policy.get("adapter_timeout_s", 20)))
        if not data:
            return None
        entries = data if isinstance(data, list) else data.get("checkpoints", [])
        if not entries:
            return None
        matching = [e for e in entries if e.get("session_id") == session_id] or entries

        def _sort_key(entry):
            try:
                return datetime.fromisoformat(entry.get("date", ""))
            except ValueError:
                return datetime.min

        latest = max(matching, key=_sort_key)
        return latest.get("message")
    except Exception:
        return None


def symbol_at_file(
    path: str,
    line: Optional[int] = None,
    exclude_session_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[sqlite3.Row]:
    """Resolve a file path to the lowest-hop active lease covering it. Pure SQL against the leases
    table -- no subprocess, this is the hot path (CUT 3). `line` is accepted for a future tighter
    match but unused: PreToolUse payloads for Edit/Write don't reliably carry a line number, so
    matching stays at file granularity.
    """
    try:
        conn = conn or get_connection()
        now = time.time()
        query = "SELECT * FROM leases WHERE path = ? AND expires_at > ? AND released_at IS NULL"
        params: list = [path, now]
        if exclude_session_id is not None:
            query += " AND session_id != ?"
            params.append(exclude_session_id)
        query += " ORDER BY hops ASC, created_at ASC"
        rows = conn.execute(query, params).fetchall()
        return rows[0] if rows else None
    except Exception:
        return None


# --------------------------------------------------------------------------------------
# airspace -- lease building + separation scoring
# --------------------------------------------------------------------------------------


def file_flight(
    session_id: str,
    prompt: str,
    repo: str = ".",
    agent: Optional[str] = None,
    branch: Optional[str] = None,
    worktree: Optional[str] = None,
) -> FlightResult:
    policy = _load_policy()
    # Refresh the co-change prior from Databricks here -- the slow, once-per-prompt path. Never
    # from check(), which must stay pure SQL. Fails soft: unreachable Databricks leaves the cache
    # as-is and separation still works on the structural term alone.
    try:
        from . import prior as prior_mod
        prior_mod.refresh()
    except Exception:
        pass
    seed_top_k = int(policy.get("seed_top_k", 3))
    lease_depth = int(policy.get("lease_depth", 2))
    max_symbols = int(policy.get("max_lease_symbols", 300))
    ttl_minutes = float(policy.get("lease_ttl_minutes", 20))

    seeds = search_symbols(prompt, top_k=seed_top_k, repo=repo)
    core: dict[str, SymbolRef] = {s.symbol_id: s for s in seeds}

    halo: dict[str, tuple[int, str, SymbolRef]] = {}
    for seed in seeds:
        for sym, hops in impact_set(seed, depth=lease_depth, repo=repo):
            if sym.symbol_id in core:
                continue
            if sym.verify_command is None and seed.verify_command:
                # impact responses carry no verify_command of their own -- inherit the seed's,
                # already the narrowest test Entire has for that seed's file. Do not invent one.
                sym.verify_command = seed.verify_command
            existing = halo.get(sym.symbol_id)
            if existing is None or hops < existing[0]:
                halo[sym.symbol_id] = (hops, seed.symbol_id, sym)

    core_rows = [(sym, 0, None) for sym in core.values()]
    halo_rows = [(sym, hops, via) for hops, via, sym in sorted(halo.values(), key=lambda t: t[0])]

    all_rows = core_rows + halo_rows
    capped = len(all_rows) > max_symbols
    if capped:
        keep = max(max_symbols - len(core_rows), 0)
        all_rows = core_rows + halo_rows[:keep]

    partial_count = sum(1 for sym, _, _ in all_rows if sym.evidence != EVIDENCE_CONFIRMED)

    now = time.time()
    expires_at = now + ttl_minutes * 60
    conn = get_connection()
    conn.execute(
        "INSERT INTO flights(session_id, agent, repo, branch, worktree, prompt, intent, "
        "filed_at, last_seen, status) VALUES (?,?,?,?,?,?,?,?,?,'active') "
        "ON CONFLICT(session_id) DO UPDATE SET agent=excluded.agent, repo=excluded.repo, "
        "branch=excluded.branch, worktree=excluded.worktree, prompt=excluded.prompt, "
        "intent=excluded.intent, last_seen=excluded.last_seen, status='active'",
        (session_id, agent, repo, branch, worktree, prompt, prompt[:200], now, now),
    )
    conn.execute(
        "UPDATE leases SET released_at = ? WHERE session_id = ? AND released_at IS NULL",
        (now, session_id),
    )
    for sym, hops, via in all_rows:
        conn.execute(
            "INSERT INTO leases(lease_id, session_id, symbol_id, kind, hops, path, line_start, "
            "line_end, via, evidence, verify_command, evidence_note, created_at, expires_at, "
            "released_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (uuid.uuid4().hex, session_id, sym.symbol_id, "core" if hops == 0 else "halo", hops,
             sym.path, sym.line_start, sym.line_end, via, sym.evidence, sym.verify_command,
             sym.evidence_note, now, expires_at),
        )
    conn.commit()

    return FlightResult(
        session_id=session_id,
        core_count=len(core_rows),
        halo_count=len(all_rows) - len(core_rows),
        total=len(all_rows),
        capped=capped,
        partial_count=partial_count,
    )


def _other_core_file(conn: sqlite3.Connection, lease_row: sqlite3.Row) -> str:
    via = lease_row["via"]
    if not via:
        # hops == 0: the matched lease IS the other session's core symbol, so its own file is
        # "the other core symbol's file" (spec 5.2's degenerate same-file case).
        return lease_row["path"]
    row = conn.execute(
        "SELECT path FROM leases WHERE session_id = ? AND symbol_id = ? AND hops = 0 "
        "ORDER BY created_at DESC LIMIT 1",
        (lease_row["session_id"], via),
    ).fetchone()
    return row["path"] if row else lease_row["path"]


def _lookup_prior(conn: sqlite3.Connection, file_a: str, file_b: str) -> float:
    a, b = sorted((file_a, file_b))
    row = conn.execute(
        "SELECT prior FROM prior_cache WHERE file_a = ? AND file_b = ?", (a, b)
    ).fetchone()
    return float(row["prior"]) if row else 0.0


def _cleared(path: str, reason: str) -> Decision:
    return Decision(
        decision="cleared", score=0.0, structural=0.0, prior=0.0, hops=None,
        other_session=None, other_intent=None, via=None, target_path=path, reason=reason,
    )


def check(session_id: str, file_path: str, line: Optional[int] = None, repo: str = ".") -> Decision:
    norm_path = file_path
    try:
        policy = _load_policy()
        norm_path = _normalize_path(file_path, repo)

        for pattern in policy.get("exempt_globs", []):
            if _glob_match(norm_path, pattern):
                return _cleared(norm_path, reason=f"exempt:{pattern}")

        conn = get_connection()
        lease_row = symbol_at_file(norm_path, line=line, exclude_session_id=session_id, conn=conn)
        if lease_row is None:
            return _cleared(norm_path, reason="no-conflicting-lease")

        hops = lease_row["hops"]
        structural = STRUCTURAL_BY_HOPS.get(hops, 0.0)
        # spec §5.2 pairs the target's file with file(other core symbol) -- but at hops 0 those are
        # the SAME file, so the lookup degenerated to a self-pair that co-change over distinct pairs
        # can never contain, pinning the prior to 0.0 exactly where it matters most. best_prior()
        # scores the target against the other DISTINCT files the lease covers. Pure SQL (CUT 3).
        from . import prior as prior_mod
        prior, prior_pair_file = prior_mod.best_prior(conn, norm_path, lease_row["session_id"])
        if prior <= 0.0:  # fall back to the literal §5.2 pair so behaviour never regresses
            other_file = _other_core_file(conn, lease_row)
            prior, prior_pair_file = _lookup_prior(conn, norm_path, other_file), other_file

        weights = policy.get("weights", {"structural": 0.65, "prior": 0.35})
        score = round(weights.get("structural", 0.65) * structural + weights.get("prior", 0.35) * prior, 2)

        if score >= float(policy.get("deny_threshold", 0.55)):
            decision = "denied"
        elif score >= float(policy.get("warn_threshold", 0.30)):
            decision = "warned"
        else:
            decision = "cleared"

        # Curveball insurance: partial/unverified graph evidence, carried on the lease row from
        # flight-plan time (never recomputed here -- CUT 3), can only RAISE the decision to the
        # policy floor. It never lowers one that's already stronger -- a deny stays a deny. Graph
        # silence is not proof of safety.
        evidence = lease_row["evidence"] or EVIDENCE_CONFIRMED
        evidence_note = lease_row["evidence_note"] or ""
        if evidence != EVIDENCE_CONFIRMED:
            floor = str(policy.get("partial_evidence_floor", "warned"))
            if DECISION_RANK.get(floor, 1) > DECISION_RANK.get(decision, 0):
                decision = floor

        other_session_id = lease_row["session_id"]
        # NOT session_intent(): that shells out to `entire checkpoint list` and this path must
        # stay pure SQL (CUT 3, <200ms). flights.intent was already stored, subprocess-free, at
        # file_flight() time -- read it back instead.
        intent_row = conn.execute(
            "SELECT intent FROM flights WHERE session_id = ?", (other_session_id,)
        ).fetchone()
        other_intent = intent_row["intent"] if intent_row else None

        return Decision(
            decision=decision, score=score, structural=structural, prior=prior, hops=hops,
            other_session=other_session_id, other_intent=other_intent, via=lease_row["via"],
            target_path=norm_path, symbol_id=lease_row["symbol_id"],
            line_start=lease_row["line_start"], line_end=lease_row["line_end"], reason=decision,
            evidence=evidence, verify_command=lease_row["verify_command"], evidence_note=evidence_note,
        )
    except Exception as exc:
        return _cleared(norm_path, reason=f"error:{exc.__class__.__name__}")


# --------------------------------------------------------------------------------------
# briefing (template only, no LLM) + logging
# --------------------------------------------------------------------------------------


def render_deny(decision: Decision) -> str:
    """Plain-text handoff brief. Template only -- no model call on this path.

    Curveball: the tier line below is not decoration -- it is the difference between "this hop
    distance is settled fact" and "this hop distance came from a query the graph itself flagged as
    incomplete or ambiguous." A PARTIAL/UNVERIFIED result always says the distance may be wrong and
    names a way to check it, so the blocked agent isn't asked to just trust a number.
    """
    lines = f"{decision.line_start}-{decision.line_end}" if decision.line_start else "?"
    other_short = (decision.other_session or "?")[:8]
    intent = decision.other_intent or "(no recorded intent)"
    path_chain = f"{decision.target_path}"
    if decision.symbol_id:
        path_chain += f" <- {decision.symbol_id}"
    if decision.via:
        path_chain += f" <- {decision.via}"

    evidence = decision.evidence or EVIDENCE_CONFIRMED
    if evidence == EVIDENCE_CONFIRMED:
        evidence_line = "  evidence      CONFIRMED -- structural, graph fully resolved for this file\n"
    elif evidence == EVIDENCE_PARTIAL:
        note = decision.evidence_note or "graph analysis for this file was incomplete"
        how = (f"Verify with: {decision.verify_command}" if decision.verify_command
               else "Re-run entire graph impact on this file before trusting the distance above")
        evidence_line = f"  evidence      PARTIAL -- {note}; this relationship may be wrong. {how}.\n"
    else:  # EVIDENCE_UNVERIFIED
        note = decision.evidence_note or "no resolved symbol, file-level match only"
        verify = decision.verify_command or "inspect the source directly -- no verify command available"
        evidence_line = (
            f"  evidence      UNVERIFIED -- {note}; the hop distance above may be wrong. "
            f"Verify with: {verify}\n"
        )

    return (
        "⛔ DENIED -- separation conflict\n\n"
        f"  symbol        {decision.symbol_id}\t{decision.target_path}:{lines}\n"
        f"  leased by     session {other_short}\n"
        f"  their intent  \"{intent}\"\n"
        f"  graph path    {path_chain}\tdepth {decision.hops}\n"
        f"  prior         P(collide) = {decision.score}\t"
        f"(structural {decision.structural} x 0.65 + prior {decision.prior} x 0.35)\n"
        f"{evidence_line}\n"
        "  handoff brief\n"
        f"  Session {other_short} is already leasing this area (depth {decision.hops} from its core "
        "edit). Wait for its lease to expire, or ask the user which of you should continue; do not "
        "edit this symbol until the lease clears.\n"
    )


def record_squawk(
    session_id: str,
    tool: str,
    file_path: str,
    line: Optional[int],
    decision: Decision,
    latency_ms: Optional[int] = None,
) -> str:
    conn = get_connection()
    squawk_id = uuid.uuid4().hex[:8]
    now = time.time()
    path_json = json.dumps([p for p in (decision.target_path, decision.via, decision.symbol_id) if p])
    conn.execute(
        "INSERT INTO squawks(squawk_id, ts, session_id, tool, file_path, line, target_symbol, "
        "decision, score, structural, prior, distance, other_session_id, other_intent, path_json, "
        "evidence, latency_ms, rerouted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (squawk_id, now, session_id, tool, file_path, line, decision.symbol_id, decision.decision,
         decision.score, decision.structural, decision.prior, decision.hops, decision.other_session,
         decision.other_intent, path_json, decision.evidence, latency_ms),
    )
    payload = {
        "squawk_id": squawk_id, "ts": now, "session_id": session_id, "tool": tool,
        "file_path": file_path, "line": line, "target_symbol": decision.symbol_id,
        "decision": decision.decision, "score": decision.score, "structural": decision.structural,
        "prior": decision.prior, "distance": decision.hops, "other_session_id": decision.other_session,
        "other_intent": decision.other_intent, "path_json": path_json, "evidence": decision.evidence,
        "latency_ms": latency_ms,
    }
    conn.execute(
        "INSERT INTO outbox(table_name, payload_json, created_at) VALUES ('squawks', ?, ?)",
        (json.dumps(payload), now),
    )
    conn.commit()
    return squawk_id

"""TOWER — the collision prior: Databricks co-change, cached locally, read as pure SQL.

Two halves, deliberately separated so the hook path never touches the network:

  refresh()     pulls workspace.tower.cochange into the local prior_cache table. Called from
                file_flight() (the slow, once-per-prompt path) and from scripts, never from check().
  best_prior()  a pure-SQL lookup used by check(). No subprocess, no network (CUT 3).

WHY best_prior() EXISTS (this fixes a real bug, see BUILDATHON.md limitations):
spec §5.2 says the prior is `prior_cache[file(target), file(other core symbol)]`. At distance 0 the
target IS the leased symbol, so file(target) == file(other core symbol) and the lookup degenerates
to a self-pair — which co-change over *distinct* file pairs can never contain. The prior was
therefore always 0.0 exactly where the score matters most, and the calibrated 0.87 was unreachable
outside a hand-staged row.

The fix reads §5.2 the way it was meant: a lease spans several files, so score the target against
the other *distinct* files the lease covers and take the strongest coupling. That is the honest
question anyway — "how likely is editing this file to collide with the work this lease represents?"
Taking the max is the conservative choice, consistent with how TOWER treats partial evidence.
"""

from __future__ import annotations

import pathlib
import sqlite3
import time
from typing import Optional

CREDENTIALS = pathlib.Path.home() / ".tower" / "databricks.env"
COCHANGE_TABLE = "workspace.tower.cochange"


def _load_credentials() -> dict[str, str]:
    """Credentials live outside the repo on purpose: they can never be committed."""
    env: dict[str, str] = {}
    if not CREDENTIALS.exists():
        return env
    for line in CREDENTIALS.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def refresh(conn: Optional[sqlite3.Connection] = None, max_rows: int = 5000) -> int:
    """Pull cochange from Databricks into prior_cache. Returns rows written, 0 on any failure.

    Fail-soft by design: if Databricks is unreachable the prior simply stays at whatever is already
    cached (0.0 on a cold cache) and separation still works on the structural term alone — a
    distance-0 conflict scores 0.65, which is above the 0.55 deny threshold.
    """
    from . import core  # local import: prior.py must be importable without a live DB

    env = _load_credentials()
    if not all(env.get(k) for k in ("DATABRICKS_HOST", "DATABRICKS_HTTP_PATH", "DATABRICKS_TOKEN")):
        return 0
    try:
        from databricks import sql
    except ImportError:
        return 0

    conn = conn or core.get_connection()
    try:
        with sql.connect(
            server_hostname=env["DATABRICKS_HOST"].replace("https://", ""),
            http_path=env["DATABRICKS_HTTP_PATH"],
            access_token=env["DATABRICKS_TOKEN"],
        ) as dbx:
            with dbx.cursor() as cur:
                cur.execute(
                    f"SELECT file_a, file_b, prior FROM {COCHANGE_TABLE} "
                    f"ORDER BY prior DESC LIMIT {int(max_rows)}"
                )
                rows = cur.fetchall()
    except Exception:
        return 0

    now = time.time()
    written = 0
    for file_a, file_b, prior in rows:
        a, b = sorted((str(file_a), str(file_b)))
        conn.execute(
            "INSERT INTO prior_cache(file_a, file_b, prior, refreshed_at) VALUES (?,?,?,?) "
            "ON CONFLICT(file_a, file_b) DO UPDATE SET prior=excluded.prior, "
            "refreshed_at=excluded.refreshed_at",
            (a, b, float(prior), now),
        )
        written += 1
    conn.commit()
    return written


def lookup(conn: sqlite3.Connection, file_a: str, file_b: str) -> float:
    """Pure-SQL prior for one unordered file pair. 0.0 when absent or when the pair is a self-pair."""
    if not file_a or not file_b or file_a == file_b:
        return 0.0
    a, b = sorted((file_a, file_b))
    row = conn.execute(
        "SELECT prior FROM prior_cache WHERE file_a = ? AND file_b = ?", (a, b)
    ).fetchone()
    return float(row["prior"]) if row else 0.0


def best_prior(conn: sqlite3.Connection, target_file: str, other_session_id: str) -> tuple[float, Optional[str]]:
    """Strongest co-change between `target_file` and any OTHER distinct file the lease covers.

    Returns (prior, the file it paired with). Pure SQL — safe on the hook path.
    Self-pairs are excluded, which is exactly the degenerate case this replaces.
    """
    if not target_file or not other_session_id:
        return 0.0, None
    rows = conn.execute(
        "SELECT DISTINCT path FROM leases "
        "WHERE session_id = ? AND released_at IS NULL AND expires_at > ? AND path != ? AND path != ''",
        (other_session_id, time.time(), target_file),
    ).fetchall()
    best, best_file = 0.0, None
    for r in rows:
        p = lookup(conn, target_file, r["path"])
        if p > best:
            best, best_file = p, r["path"]
    return best, best_file


if __name__ == "__main__":  # python -m tower.prior
    n = refresh()
    print(f"prior_cache refreshed: {n} rows" if n else
          "prior_cache NOT refreshed (no credentials, or Databricks unreachable) -- prior stays 0.0")

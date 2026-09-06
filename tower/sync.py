"""TOWER — drain the local outbox into Delta. Never on the request path.

Hooks append a row to SQLite `outbox` and return immediately; this process moves them to
`workspace.tower.*` out of band. That separation is the point: a hook must never wait on a
network call, so Databricks being slow or down can delay analytics but can never delay — or
block — a developer's edit.

    python -m tower.sync              # drain the outbox once
    python -m tower.sync --backfill   # also push current flights/leases (one-shot, idempotent)
    python -m tower.sync --loop       # drain every 10s

Credentials come from ~/.tower/databricks.env, deliberately outside the repo.
"""

from __future__ import annotations

import json
import sys
import time

from . import core, prior

BATCH = 200
TABLES = {
    "squawks": ["squawk_id", "ts", "session_id", "tool", "file_path", "target_symbol", "decision",
                "score", "structural", "prior", "distance", "other_session_id", "other_intent",
                "evidence", "latency_ms"],
    "flights": ["session_id", "agent", "repo", "branch", "prompt", "intent", "filed_at", "status"],
    "leases": ["lease_id", "session_id", "symbol_id", "path", "kind", "hops", "via",
               "created_at", "expires_at"],
}


def _lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def _connect():
    env = prior._load_credentials()
    if not all(env.get(k) for k in ("DATABRICKS_HOST", "DATABRICKS_HTTP_PATH", "DATABRICKS_TOKEN")):
        return None
    try:
        from databricks import sql
    except ImportError:
        return None
    return sql.connect(
        server_hostname=env["DATABRICKS_HOST"].replace("https://", ""),
        http_path=env["DATABRICKS_HTTP_PATH"],
        access_token=env["DATABRICKS_TOKEN"],
    )


def _insert(cur, table: str, rows: list[dict]) -> int:
    cols = TABLES[table]
    written = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        values = ",".join("(" + ",".join(_lit(r.get(c)) for c in cols) + ")" for r in chunk)
        cur.execute(f"INSERT INTO workspace.tower.{table} ({','.join(cols)}) VALUES {values}")
        written += len(chunk)
    return written


def drain(backfill: bool = False) -> dict[str, int]:
    """Move queued rows to Delta. Returns {table: rows_written}. Failure leaves the outbox intact."""
    conn = core.get_connection()
    queued = conn.execute(
        "SELECT id, table_name, payload_json FROM outbox ORDER BY id"
    ).fetchall()

    batches: dict[str, list[dict]] = {}
    drained_ids: list[int] = []
    for row in queued:
        table = row["table_name"]
        if table not in TABLES:
            drained_ids.append(row["id"])  # unknown table: drop rather than block the queue forever
            continue
        try:
            batches.setdefault(table, []).append(json.loads(row["payload_json"]))
            drained_ids.append(row["id"])
        except json.JSONDecodeError:
            drained_ids.append(row["id"])

    if backfill:
        for table, cols in (("flights", TABLES["flights"]), ("leases", TABLES["leases"])):
            rows = conn.execute(f"SELECT {','.join(cols)} FROM {table}").fetchall()
            batches.setdefault(table, []).extend(dict(r) for r in rows)

    if not batches:
        return {}

    dbx = _connect()
    if dbx is None:
        print("no credentials / connector -- outbox left intact", file=sys.stderr)
        return {}

    written: dict[str, int] = {}
    try:
        with dbx:
            with dbx.cursor() as cur:
                for table, rows in batches.items():
                    written[table] = _insert(cur, table, rows)
    except Exception as exc:
        # Leave the outbox untouched so the next run retries. Analytics lag; nothing is lost.
        print(f"delta write failed ({type(exc).__name__}: {exc}) -- outbox retained", file=sys.stderr)
        return {}

    if drained_ids:
        conn.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in drained_ids])
        conn.commit()
    return written


def main() -> None:
    backfill = "--backfill" in sys.argv
    if "--loop" in sys.argv:
        while True:
            w = drain(backfill=backfill)
            if w:
                print(f"{time.strftime('%H:%M:%S')} drained {w}")
            backfill = False  # only ever once
            time.sleep(10)
    else:
        w = drain(backfill=backfill)
        print(f"drained: {w}" if w else "nothing to drain")


if __name__ == "__main__":
    main()

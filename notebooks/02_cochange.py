"""TOWER — build the file-pair co-change prior from this repo's git history.

    prior = pair_count / min(a_count, b_count), clipped to [0, 1]

Kept: pairs co-changed in at least MIN_PAIR commits. Two files that always move
together score near 1.0; two that merely both change often score near 0.

This is the `prior` term in TOWER's separation score:

    score = 0.65 * structural + 0.35 * prior

DATA PROVENANCE: this repository's own public git history, nothing else. No
third-party or personal data. Stated as an approximation in BUILDATHON.md,
because separation is measured at *symbol* level while co-change is measured at
*file* level -- two files changing together is weaker evidence than two symbols
changing together, which is why it is only weighted 0.35 and never used alone.

Run locally (writes to Databricks over databricks-sql-connector):
    python notebooks/02_cochange.py
Credentials are read from ~/.tower/databricks.env, deliberately outside the repo.
"""

from __future__ import annotations

import itertools
import pathlib
import subprocess
import sys
from collections import Counter

COMMIT_WINDOW = 300      # most recent N commits
MIN_PAIR = 2             # ignore pairs that co-changed only once
MAX_FILES_PER_COMMIT = 40  # skip sweeping refactors; they co-change everything with everything
TABLE = "workspace.tower.cochange"

# Noise that co-changes with everything and tells us nothing about coupling.
SKIP_SUFFIXES = (".md", ".lock", ".sum", ".txt", ".json", ".yaml", ".yml")
SKIP_PREFIXES = (".entire/", ".github/", ".claude/", ".codex/", "evidence/", "fixtures/")


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = pathlib.Path.home() / ".tower" / "databricks.env"
    if not p.exists():
        sys.exit(f"missing {p} -- see BUILDATHON.md setup section")
    for line in p.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def interesting(path: str) -> bool:
    if path.endswith(SKIP_SUFFIXES):
        return False
    return not path.startswith(SKIP_PREFIXES)


def commits(repo: str = ".") -> list[list[str]]:
    """Return each commit as its list of touched files."""
    out = subprocess.run(
        ["git", "log", f"-{COMMIT_WINDOW}", "--name-only", "--pretty=format:%H"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    groups, current = [], []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
            if current:
                groups.append(current)
            current = []
        elif interesting(line):
            current.append(line)
    if current:
        groups.append(current)
    return groups


def build(groups: list[list[str]]) -> list[tuple]:
    file_counts: Counter[str] = Counter()
    pair_counts: Counter[tuple[str, str]] = Counter()
    for files in groups:
        files = sorted(set(files))
        if not files or len(files) > MAX_FILES_PER_COMMIT:
            continue
        file_counts.update(files)
        pair_counts.update(itertools.combinations(files, 2))  # already sorted -> (a, b) canonical

    rows = []
    for (a, b), n in pair_counts.items():
        if n < MIN_PAIR:
            continue
        denom = min(file_counts[a], file_counts[b])
        if denom <= 0:
            continue
        rows.append((a, b, n, file_counts[a], file_counts[b], min(1.0, n / denom)))
    rows.sort(key=lambda r: (-r[5], -r[2]))
    return rows


def main() -> None:
    env = load_env()
    repo = str(pathlib.Path(__file__).resolve().parents[1])

    groups = commits(repo)
    rows = build(groups)
    print(f"{len(groups)} commits -> {len(rows)} file pairs with count >= {MIN_PAIR}")
    if not rows:
        sys.exit("no pairs found -- is this a shallow clone?")

    print(f"\ntop 10 by prior:\n{'prior':>6}  {'pairs':>5}  files")
    for a, b, n, _ac, _bc, pr in rows[:10]:
        print(f"{pr:6.2f}  {n:5}  {a}  <->  {b}")

    from databricks import sql  # imported late so --dry-run needs no connector

    with sql.connect(
        server_hostname=env["DATABRICKS_HOST"].replace("https://", ""),
        http_path=env["DATABRICKS_HTTP_PATH"],
        access_token=env["DATABRICKS_TOKEN"],
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {TABLE}")
            for i in range(0, len(rows), 200):   # batched; one statement per chunk
                chunk = rows[i:i + 200]
                values = ",".join(
                    "('{}','{}',{},{},{},{})".format(
                        a.replace("'", "''"), b.replace("'", "''"), n, ac, bc, round(pr, 6)
                    )
                    for a, b, n, ac, bc, pr in chunk
                )
                cur.execute(f"INSERT INTO {TABLE} VALUES {values}")
                print(f"  inserted {min(i + 200, len(rows))}/{len(rows)}")
            cur.execute(f"SELECT count(*) FROM {TABLE}")
            print(f"\n{TABLE} now holds {cur.fetchone()[0]} rows")


if __name__ == "__main__":
    main()

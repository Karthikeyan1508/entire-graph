"""UserPromptSubmit hook. Files a flight plan for the prompt and tells the agent it is in
controlled airspace. Reads stdin JSON, wraps EVERYTHING in try/except, always exits 0 -- a
wedged agent is worse than no TOWER (CLAUDE.md "Fail-open everywhere")."""

import json
import sys
import time
from pathlib import Path


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
        session_id = payload.get("session_id") or ""
        cwd = payload.get("cwd") or "."
        prompt = payload.get("prompt") or ""
        if not session_id or not prompt:
            sys.exit(0)

        # The venv lives outside the repo (D:\tower\.venv), so `tower` is only importable once
        # the repo root is on sys.path.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from tower import core

        result = core.file_flight(session_id, prompt, repo=cwd)

        conn = core.get_connection()
        row = conn.execute(
            "SELECT COUNT(DISTINCT session_id) AS n FROM leases "
            "WHERE session_id != ? AND expires_at > ? AND released_at IS NULL",
            (session_id, time.time()),
        ).fetchone()
        others_active = row["n"] if row else 0

        if result.total == 0:
            # Fail-open stays -- the prompt still proceeds -- but failing open SILENTLY is exactly
            # what put the earlier cold-cache run at risk: 0 leases looks identical to "this prompt
            # matched nothing" unless we say so. Make it loud.
            message = (
                "TOWER: 0 symbols leased -- graph cache is cold. Run scripts/prewarm.sh. "
                "You are NOT protected."
            )
        else:
            message = (
                f"TOWER: flight plan filed. You hold a lease on {result.total} symbols "
                f"({result.core_count} core, {result.halo_count} halo)"
                f"{' (capped)' if result.capped else ''}. "
                f"{others_active} other session(s) active in this repo."
            )
            if result.partial_count:
                # Curveball: graph is evidence, not an oracle -- say so as loudly as the existing
                # 0-symbols warning, not just in the deny path. A separation check against these
                # symbols floors at "warned" even if the score alone would clear.
                message += (
                    f" {result.partial_count} of those symbols carry partial or unverified graph "
                    "evidence (incomplete analysis, or a query the graph itself flagged as "
                    "ambiguous) -- TOWER treats those as at least a warn, never a silent clear."
                )
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": message,
            }
        }))
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()

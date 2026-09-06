"""SessionEnd hook. Releases this session's leases. Reads stdin JSON, wraps EVERYTHING in
try/except, always exits 0, prints nothing (spec §6.3)."""

import json
import sys
import time
from pathlib import Path


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
        session_id = payload.get("session_id") or ""
        if not session_id:
            sys.exit(0)

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from tower import core

        conn = core.get_connection()
        now = time.time()
        conn.execute(
            "UPDATE leases SET released_at = ? WHERE session_id = ? AND released_at IS NULL",
            (now, session_id),
        )
        conn.execute(
            "UPDATE flights SET status = 'released', last_seen = ? WHERE session_id = ?",
            (now, session_id),
        )
        conn.commit()
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()

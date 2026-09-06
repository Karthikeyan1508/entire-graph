"""PreToolUse hook on Edit|Write|MultiEdit. Denies at the hook, or prints nothing. Reads stdin
JSON, wraps EVERYTHING in try/except, always exits 0 -- see CLAUDE.md "Fail-open everywhere".
Pure SQL against the leases table (CUT 3): no subprocess on this path, so it stays under 200ms."""

import json
import sys
from pathlib import Path


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
        session_id = payload.get("session_id") or ""
        cwd = payload.get("cwd") or "."
        tool_name = payload.get("tool_name") or ""
        file_path = (payload.get("tool_input") or {}).get("file_path")
        if not session_id or not file_path:
            sys.exit(0)

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from tower import core

        decision = core.check(session_id, file_path, repo=cwd)

        if decision.decision != "cleared":
            # Only warned/denied are logged -- a squawk per ordinary cleared edit would flood the
            # table on every single Edit/Write call.
            core.record_squawk(session_id, tool_name, file_path, None, decision)

        if decision.decision == "denied":
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": core.render_deny(decision)[:1500],
                }
            }))
        # cleared or warned -> print nothing (spec §6.1: a warn is logged and surfaced on the
        # radar, not in the agent; only a deny bypasses the user's own permission prompt).
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()

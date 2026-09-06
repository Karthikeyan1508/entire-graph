# Counterfactual: the same collision with TOWER not running

Captured 2026-09-06 ~11:15, during the first live two-session test.

Session A was asked to change how SearchRepository ranks results. It edited
internal/sem/search.go and succeeded. TOWER did not stop it, because the
PreToolUse hook never executed:

    PreToolUse:Edit hook error
    /usr/bin/bash: line 1: D:tower.venvScriptspython.exe: command not found
    Added 7 lines, removed 2 lines

Cause: Claude Code runs hook commands through bash. The Windows path was
written with backslashes, and bash consumed them as escape characters:
  D:\tower\.venv\Scripts\python.exe  ->  D:tower.venvScriptspython.exe
Fixed by using forward slashes, which bash passes through and Windows accepts.

This is worth keeping. It is exactly the failure TOWER exists to prevent, and
git reported nothing wrong: no conflict, no warning, a clean working tree diff.
The accompanying .diff is that edit.

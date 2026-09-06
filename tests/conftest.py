"""Fixture-mode + isolated DB for the whole test session -- must be set before `tower.core` is
imported by any test module, so this lives in conftest.py and runs at collection time."""

import os
import tempfile
from pathlib import Path

_tmp_dir = tempfile.mkdtemp(prefix="tower-test-")
os.environ["TOWER_FIXTURES"] = "1"
os.environ["TOWER_DB_PATH"] = str(Path(_tmp_dir) / "tower-test.db")

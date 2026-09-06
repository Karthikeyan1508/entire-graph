"""TOWER_FIXTURES=1, isolated SQLite (see conftest.py) -- no Entire, no Databricks, no server.

Proves the scoring table pinned in CLAUDE.md / ARCHITECTURE.md:
  hops 0 + prior 0.63 -> 0.87 -> denied      hops 1 + prior 0 -> 0.39 -> warned
  hops 2 + prior 0 -> 0.23 -> cleared        own lease -> cleared      exempt glob -> cleared
"""

import time

import pytest

from tower import core


@pytest.fixture(autouse=True)
def _clean_db():
    conn = core.get_connection()
    for table in ("leases", "flights", "prior_cache", "squawks", "outbox"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    yield


def _seed_lease(session_id, path, hops, symbol_id, via=None, prior=None, prior_other_file=None):
    conn = core.get_connection()
    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO flights(session_id, agent, repo, branch, worktree, prompt, "
        "intent, filed_at, last_seen, status) VALUES (?,?,?,?,?,?,?,?,?,'active')",
        (session_id, "claude", ".", "main", ".", "test prompt", "test", now, now),
    )
    conn.execute(
        "INSERT INTO leases(lease_id, session_id, symbol_id, kind, hops, path, line_start, "
        "line_end, via, created_at, expires_at, released_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
        (f"lease-{session_id}-{path}-{hops}", session_id, symbol_id,
         "core" if hops == 0 else "halo", hops, path, 1, 10, via, now, now + 1200),
    )
    conn.commit()
    if prior is not None:
        a, b = sorted((path, prior_other_file))
        conn.execute(
            "INSERT OR REPLACE INTO prior_cache(file_a, file_b, prior, refreshed_at) "
            "VALUES (?,?,?,?)",
            (a, b, prior, now),
        )
        conn.commit()


def test_hops0_with_calibrated_prior_denies_at_0_87():
    # spec's calibration note: distance 0 with a co-change prior of 0.63 must give exactly 0.87.
    _seed_lease("A", "fileX.go", hops=0, symbol_id="sym::fileX::Core",
                prior=0.63, prior_other_file="fileX.go")
    decision = core.check("B", "fileX.go")
    assert decision.hops == 0
    assert decision.score == 0.87
    assert decision.decision == "denied"


def test_hops1_warns_at_0_39():
    _seed_lease("A", "fileX.go", hops=0, symbol_id="sym::fileX::Core")
    _seed_lease("A", "fileY.go", hops=1, symbol_id="sym::fileY::Caller", via="sym::fileX::Core")
    decision = core.check("B", "fileY.go")
    assert decision.hops == 1
    assert decision.score == 0.39
    assert decision.decision == "warned"


def test_hops2_clears_at_0_23():
    _seed_lease("A", "fileX.go", hops=0, symbol_id="sym::fileX::Core")
    _seed_lease("A", "fileZ.go", hops=2, symbol_id="sym::fileZ::Distant", via="sym::fileX::Core")
    decision = core.check("B", "fileZ.go")
    assert decision.hops == 2
    assert decision.score == 0.23
    assert decision.decision == "cleared"


def test_own_lease_clears():
    _seed_lease("A", "fileX.go", hops=0, symbol_id="sym::fileX::Core")
    decision = core.check("A", "fileX.go")
    assert decision.decision == "cleared"
    assert decision.reason == "no-conflicting-lease"


def test_exempt_glob_clears():
    _seed_lease("A", "README.md", hops=0, symbol_id="sym::readme")
    decision = core.check("B", "README.md")
    assert decision.decision == "cleared"
    assert decision.reason.startswith("exempt:")


def test_no_other_active_lease_clears():
    decision = core.check("B", "untouched.go")
    assert decision.decision == "cleared"
    assert decision.reason == "no-conflicting-lease"


def test_search_symbols_parses_real_fixture_shape():
    seeds = core.search_symbols("semantic search over a repository", top_k=3)
    assert seeds, "fixtures/search.json should yield at least one leaseable symbol"
    first = seeds[0]
    assert first.symbol_id
    assert first.path
    assert first.line_start is not None and first.line_end is not None


def test_impact_set_reports_hops_from_depth_field():
    seed = core.SymbolRef(
        symbol_id="local/entire-graph:Go:internal/sem/search.go:function:SearchRepository",
        path="internal/sem/search.go", name="SearchRepository",
        line_start=683, line_end=690, kind="function",
    )
    halo = core.impact_set(seed, depth=2)
    assert halo, "fixtures/impact.json should yield caller/callee entries"
    assert all(hops in (1, 2) for _, hops in halo)


def test_render_deny_is_plain_text_no_network():
    _seed_lease("A", "fileX.go", hops=0, symbol_id="sym::fileX::Core",
                prior=0.63, prior_other_file="fileX.go")
    decision = core.check("B", "fileX.go")
    brief = core.render_deny(decision)
    assert "DENIED" in brief
    assert "sym::fileX::Core" in brief
    assert str(decision.score) in brief

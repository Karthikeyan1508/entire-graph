"""TOWER_FIXTURES=1, isolated SQLite (see conftest.py) -- no Entire, no Databricks, no server.

Proves the scoring table pinned in CLAUDE.md / ARCHITECTURE.md:
  hops 0 + prior 0.63 -> 0.87 -> denied      hops 1 + prior 0 -> 0.39 -> warned
  hops 2 + prior 0 -> 0.23 -> cleared        own lease -> cleared      exempt glob -> cleared
"""

import json
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


def _seed_lease(session_id, path, hops, symbol_id, via=None, prior=None, prior_other_file=None,
                 evidence="confirmed", verify_command=None):
    conn = core.get_connection()
    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO flights(session_id, agent, repo, branch, worktree, prompt, "
        "intent, filed_at, last_seen, status) VALUES (?,?,?,?,?,?,?,?,?,'active')",
        (session_id, "claude", ".", "main", ".", "test prompt", "test", now, now),
    )
    conn.execute(
        "INSERT INTO leases(lease_id, session_id, symbol_id, kind, hops, path, line_start, "
        "line_end, via, evidence, verify_command, created_at, expires_at, released_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
        (f"lease-{session_id}-{path}-{hops}", session_id, symbol_id,
         "core" if hops == 0 else "halo", hops, path, 1, 10, via, evidence, verify_command,
         now, now + 1200),
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


# --------------------------------------------------------------------------------------
# Curveball, Track 2: "the graph is evidence, not an oracle" -- partial/unverified evidence must
# make TOWER MORE conservative (raise cleared -> warned), never less (never downgrade a denied),
# and CONFIRMED-evidence behaviour above this line must stay byte-identical.
# --------------------------------------------------------------------------------------


def test_partial_evidence_on_clear_floors_to_warned():
    # hops=2 alone scores 0.23 (cleared, see test_hops2_clears_at_0_23) -- but partial graph
    # evidence for this lease must raise it to the policy floor, never leave it silently cleared.
    _seed_lease("A", "fileX.go", hops=0, symbol_id="sym::fileX::Core")
    _seed_lease("A", "fileZ.go", hops=2, symbol_id="sym::fileZ::Distant", via="sym::fileX::Core",
                evidence="partial")
    decision = core.check("B", "fileZ.go")
    assert decision.hops == 2
    assert decision.score == 0.23
    assert decision.decision == "warned"
    assert decision.evidence == "partial"


def test_partial_evidence_never_downgrades_a_deny():
    # hops=0 + prior 0.63 scores 0.87 (denied, see test_hops0_with_calibrated_prior_denies_at_0_87)
    # regardless of evidence tier -- partial evidence only ever raises a decision, never lowers one
    # that's already stronger. The tier still has to show up so the blocked agent sees it.
    _seed_lease("A", "fileX.go", hops=0, symbol_id="sym::fileX::Core",
                prior=0.63, prior_other_file="fileX.go", evidence="partial")
    decision = core.check("B", "fileX.go")
    assert decision.hops == 0
    assert decision.score == 0.87
    assert decision.decision == "denied"
    assert decision.evidence == "partial"
    brief = core.render_deny(decision)
    assert "DENIED" in brief
    assert "PARTIAL" in brief


def test_unverified_evidence_carries_verify_command():
    # An ambiguous/unresolved graph match (ex: entire graph impact --symbol check hit
    # disambiguation_required, see fixtures/impact_ambiguous.json) must say so and hand the agent a
    # way to check for itself, not silently present a settled-looking hop distance.
    _seed_lease("A", "fileX.go", hops=0, symbol_id="sym::fileX::Core")
    _seed_lease("A", "fileZ.go", hops=2, symbol_id="sym::fileZ::Distant", via="sym::fileX::Core",
                evidence="unverified", verify_command="go test ./internal/sem -run TestFoo")
    decision = core.check("B", "fileZ.go")
    assert decision.hops == 2
    assert decision.score == 0.23
    assert decision.decision == "warned"
    assert decision.evidence == "unverified"
    assert decision.verify_command == "go test ./internal/sem -run TestFoo"
    brief = core.render_deny(decision)
    assert "UNVERIFIED" in brief
    assert "go test ./internal/sem -run TestFoo" in brief


def test_evidence_context_flags_the_real_ambiguous_response_as_unverified():
    # fixtures/impact_ambiguous.json is the REAL response captured for
    # `entire graph impact --symbol check --depth 2` (evidence/curveball-impact.txt) --
    # disambiguation_required: true, no focus resolved.
    data = json.loads((core.FIXTURES_DIR / "impact_ambiguous.json").read_text(encoding="utf-8"))
    ctx = core._evidence_context(data)
    assert ctx["disambiguation_required"] is True
    tier, _note = core._evidence_for("tower/core.py", None, ctx)
    assert tier == "unverified"


def test_evidence_context_flags_partial_failure_file_as_partial():
    # fixtures/impact_partial.json is fixtures/impact.json with one real caller endpoint file
    # (internal/cli/search.go) added to partial_failures.
    data = json.loads((core.FIXTURES_DIR / "impact_partial.json").read_text(encoding="utf-8"))
    ctx = core._evidence_context(data)
    tier, note = core._evidence_for("internal/cli/search.go", "some-symbol-id", ctx)
    assert tier == "partial"
    assert "internal/cli/search.go" in note
    # a file NOT named in partial_failures, with a resolved symbol, on a non-degraded response
    # stays confirmed -- partial is per-file, not a blanket downgrade of the whole response.
    other_tier, _ = core._evidence_for("internal/sem/search.go", "some-other-id", ctx)
    assert other_tier == "confirmed"


def test_impact_set_handles_the_real_ambiguous_response_without_crashing(monkeypatch):
    data = json.loads((core.FIXTURES_DIR / "impact_ambiguous.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(core, "_read_fixture", lambda name: data)
    seed = core.SymbolRef(
        symbol_id="local/entire-graph:Python:tower/core.py:function:check",
        path="tower/core.py", name="check", line_start=498, line_end=544, kind="function",
    )
    halo = core.impact_set(seed, depth=2)
    assert halo == []  # ambiguous response resolves no relations -- fail-open, not fail-crash


def test_impact_set_marks_only_the_degraded_endpoint_as_partial(monkeypatch):
    data = json.loads((core.FIXTURES_DIR / "impact_partial.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(core, "_read_fixture", lambda name: data)
    seed = core.SymbolRef(
        symbol_id="local/entire-graph:Go:internal/sem/search.go:function:SearchRepository",
        path="internal/sem/search.go", name="SearchRepository",
        line_start=683, line_end=690, kind="function",
    )
    halo = core.impact_set(seed, depth=2)
    assert halo, "impact_partial.json should still yield caller/callee entries"
    tiers = {sym.path: sym.evidence for sym, _ in halo}
    assert tiers.get("internal/cli/search.go") == "partial"
    assert any(t == "confirmed" for t in tiers.values())

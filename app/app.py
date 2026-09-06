"""TOWER radar — what the tower can see right now.

Reads Delta by default; TOWER_SOURCE=local reads the SQLite file directly. That fallback is demo
insurance: if the warehouse stalls, the radar still shows live local state.

    streamlit run app/app.py                    # Delta
    TOWER_SOURCE=local streamlit run app/app.py # SQLite
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import time

import pandas as pd
import streamlit as st

LOCAL = os.environ.get("TOWER_SOURCE") == "local"
_db_env = os.environ.get("TOWER_DB_PATH") or ""
# NB: Path("") is Path("."), which is truthy — so this must test the string, not the Path.
DB = pathlib.Path(_db_env) if _db_env else (pathlib.Path.home() / ".tower" / "tower.db")

st.set_page_config(page_title="TOWER radar", page_icon="🛫", layout="wide")


def _creds() -> dict:
    p = pathlib.Path.home() / ".tower" / "databricks.env"
    out = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


@st.cache_data(ttl=10)
def load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, str]:
    if not LOCAL:
        try:
            from databricks import sql
            e = _creds()
            with sql.connect(
                server_hostname=e["DATABRICKS_HOST"].replace("https://", ""),
                http_path=e["DATABRICKS_HTTP_PATH"], access_token=e["DATABRICKS_TOKEN"],
            ) as c:
                q = lambda s: pd.read_sql(s, c)  # noqa: E731
                return (q("SELECT * FROM workspace.tower.squawks ORDER BY ts DESC LIMIT 50"),
                        q("SELECT * FROM workspace.tower.flights ORDER BY filed_at DESC LIMIT 20"),
                        q("SELECT * FROM workspace.tower.cochange ORDER BY prior DESC LIMIT 10"),
                        "databricks · workspace.tower")
        except Exception as exc:
            st.warning(f"Delta unavailable ({type(exc).__name__}) — falling back to local SQLite.")

    conn = sqlite3.connect(str(DB))
    now = time.time()
    return (pd.read_sql("SELECT * FROM squawks ORDER BY ts DESC LIMIT 50", conn),
            pd.read_sql("SELECT * FROM flights ORDER BY filed_at DESC LIMIT 20", conn),
            pd.read_sql("SELECT file_a, file_b, prior FROM prior_cache ORDER BY prior DESC LIMIT 10", conn),
            f"local sqlite · {DB}")


squawks, flights, cochange, source = load()
denied = int((squawks["decision"] == "denied").sum()) if not squawks.empty else 0

st.title("🛫 TOWER — separation radar")
st.caption(f"source: {source}")

a, b, c, d = st.columns(4)
a.metric("Collisions averted", denied)
b.metric("Flights filed", len(flights))
c.metric("Squawks", len(squawks))
d.metric("Co-change pairs", len(cochange))

st.subheader("Active flights")
if flights.empty:
    st.info("No flight plans filed yet.")
else:
    st.dataframe(flights[[col for col in ("session_id", "branch", "intent", "status") if col in flights]],
                 use_container_width=True, hide_index=True)

st.subheader("Squawk stream")
if squawks.empty:
    st.info("No separation checks recorded yet.")
else:
    view = squawks[[c for c in ("decision", "score", "structural", "prior", "distance",
                                "file_path", "target_symbol", "other_intent") if c in squawks]].copy()
    st.dataframe(
        view.style.map(
            lambda v: {"denied": "background-color:#7f1d1d;color:white",
                       "warned": "background-color:#78350f;color:white",
                       "cleared": "background-color:#14532d;color:white"}.get(v, ""),
            subset=["decision"],
        ),
        use_container_width=True, hide_index=True,
    )

st.subheader("Airspace — the lease as a graph")
st.caption(
    "Core seeds (hops 0) are what the prompt resolved to. Halo symbols are what the call graph "
    "reaches within 2 hops — that is the airspace. Red = an edit that was denied inside it. "
    "**Solid border = confirmed structural evidence. Dashed = partial, the graph reported the "
    "analysis may be incomplete. Dotted = unverified, needs source or test verification.** "
    "A dashed node is not a weaker warning — TOWER treats partial evidence as MORE dangerous, "
    "because a missing edge is a collision it cannot see."
)


def _short(symbol_id: str) -> str:
    """local/entire-graph:Go:internal/sem/search.go:function:SearchRepository -> SearchRepository"""
    return symbol_id.rsplit(":", 1)[-1] if symbol_id else "?"


def lease_dot(session_id: str, denied_symbols: set[str], limit: int = 26) -> str:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        # Only symbols that actually live in this repo. External endpoints (errors.New, fmt.Errorf)
        # are real graph edges but they are noise in a picture about airspace ownership.
        "SELECT symbol_id, path, hops, via, COALESCE(evidence,'confirmed') evidence FROM leases WHERE session_id = ? "
        "AND path IS NOT NULL AND path != '' AND path NOT LIKE '%_test.go' "
        "ORDER BY hops, symbol_id LIMIT ?", (session_id, limit),
    ).fetchall()
    conn.close()
    if not rows:
        return ""

    fill = {0: "#1d4ed8", 1: "#0e7490", 2: "#3f3f46"}
    lines = [
        'digraph lease {', '  rankdir=LR; bgcolor="transparent"; pad=0.3;',
        '  node [shape=box style="rounded,filled" fontname="Helvetica" fontsize=10 '
        'fontcolor="#f4f4f5" color="#52525b"];',
        '  edge [color="#52525b" arrowsize=0.6];',
    ]
    present = {r["symbol_id"] for r in rows}
    # Curveball, Track 2: this picture presents graph RELATIONSHIPS, so it must not draw an
    # incomplete one as certain. The border encodes the tier the adapter recorded on the lease:
    #   solid  = confirmed structural evidence
    #   dashed = partial -- the graph itself reported the analysis may be incomplete
    #   dotted = unverified -- no resolved symbol; needs source or test verification
    border = {"confirmed": "solid", "partial": "dashed", "unverified": "dotted"}
    mark = {"confirmed": "", "partial": r"\n⚠ partial evidence", "unverified": r"\n? unverified"}
    for r in rows:
        sid, hops = r["symbol_id"], r["hops"] or 0
        ev = r["evidence"] or "confirmed"
        denied = sid in denied_symbols
        colour = "#b91c1c" if denied else fill.get(hops, "#3f3f46")
        label = _short(sid).replace('"', "") + mark.get(ev, "")
        style = f'"rounded,filled,{border.get(ev, "solid")}"'
        if denied:
            pen = ' penwidth=2.5 color="#fca5a5"'
        elif ev != "confirmed":
            pen = ' penwidth=2 color="#fbbf24"'
        else:
            pen = ""
        lines.append(
            f'  "{sid}" [label="{label}\\nhops {hops}" fillcolor="{colour}" style={style}{pen}];'
        )
    for r in rows:
        if r["via"] and r["via"] in present:
            lines.append(f'  "{r["via"]}" -> "{r["symbol_id"]}";')
    lines.append("}")
    return "\n".join(lines)


_denied_syms = set(squawks.loc[squawks["decision"] == "denied", "target_symbol"].dropna()) \
    if not squawks.empty and "target_symbol" in squawks else set()

_lease_sessions = []
if not flights.empty and "session_id" in flights:
    conn_l = sqlite3.connect(str(DB))
    for sid in flights["session_id"]:
        n = conn_l.execute(
            "SELECT count(*), sum(released_at IS NULL) FROM leases WHERE session_id=?", (sid,)
        ).fetchone()
        if n and n[0]:
            _lease_sessions.append((sid, n[0], bool(n[1])))
    conn_l.close()

if not _lease_sessions:
    st.info("No lease recorded yet. File a flight plan and the airspace appears here.")
else:
    pick = st.selectbox(
        "Lease", _lease_sessions,
        format_func=lambda t: f"{t[0][:8]}…  ({t[1]} symbols, {'active' if t[2] else 'released'})",
    )
    dot = lease_dot(pick[0], _denied_syms)
    if dot:
        st.graphviz_chart(dot, use_container_width=True)
        st.caption(f"Showing up to 26 of {pick[1]} leased symbols, lowest hops first.")

st.subheader("Collision prior — top co-change pairs")
st.caption("Computed in Databricks from this repo's own git history. Supplies the 0.35-weighted term.")
st.dataframe(cochange, use_container_width=True, hide_index=True)

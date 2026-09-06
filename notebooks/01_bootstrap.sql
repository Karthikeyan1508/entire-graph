-- TOWER — Databricks bootstrap
-- Creates the schema and the four tables TOWER writes to or reads from.
-- Safe to re-run: everything is IF NOT EXISTS.
--
-- Run either in a Databricks SQL editor, or via scripts/databricks_bootstrap.py
-- which executes these statements over databricks-sql-connector.

CREATE SCHEMA IF NOT EXISTS workspace.tower
  COMMENT 'TOWER — air traffic control for coding agents. BTW Buildathon 2026, Track 02.';

-- Every separation decision TOWER has made. Drained from the local `outbox`
-- table out of band; nothing on the hook path ever waits for this.
CREATE TABLE IF NOT EXISTS workspace.tower.squawks (
  squawk_id        STRING,
  ts               DOUBLE,
  session_id       STRING,
  tool             STRING,
  file_path        STRING,
  target_symbol    STRING,
  decision         STRING,   -- cleared | warned | denied
  score            DOUBLE,
  structural       DOUBLE,
  prior            DOUBLE,
  distance         INT,      -- graph hops; 0 = same symbol
  other_session_id STRING,
  other_intent     STRING,
  evidence         STRING,   -- confirmed | partial | unverified (Curveball, Track 2)
  latency_ms       INT
) COMMENT 'One row per PreToolUse separation check.';

-- Flight plans: one row per prompt that filed a lease.
CREATE TABLE IF NOT EXISTS workspace.tower.flights (
  session_id STRING,
  agent      STRING,
  repo       STRING,
  branch     STRING,
  prompt     STRING,
  intent     STRING,
  filed_at   DOUBLE,
  status     STRING
) COMMENT 'One row per filed flight plan.';

-- The symbols each flight plan reserved, with graph distance.
CREATE TABLE IF NOT EXISTS workspace.tower.leases (
  lease_id   STRING,
  session_id STRING,
  symbol_id  STRING,
  path       STRING,
  kind       STRING,   -- core | halo
  hops       INT,
  via        STRING,
  created_at DOUBLE,
  expires_at DOUBLE
) COMMENT 'Leased symbols. hops 0 = core seed, 1..n = halo.';

-- The analytics brain: file-pair co-change learned from this repo's git history.
-- Read back into the local prior_cache; supplies the 0.35-weighted `prior` term.
CREATE TABLE IF NOT EXISTS workspace.tower.cochange (
  file_a     STRING,
  file_b     STRING,
  pair_count INT,      -- commits touching both
  a_count    INT,      -- commits touching file_a
  b_count    INT,      -- commits touching file_b
  prior      DOUBLE    -- pair_count / min(a_count, b_count), clipped to [0,1]
) COMMENT 'File-level co-change prior. Approximation: separation is measured at symbol level, co-change at file level.';

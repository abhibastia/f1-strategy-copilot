-- Correctness checks. Run these before trusting the dashboard.
--
-- Check 1 is the important one: it reconciles two INDEPENDENT Jolpica endpoints
-- against each other (results + sprint versus driverStandings). Agreement is
-- real evidence the flattening, deduplication, and joins are correct — not a
-- comparison against a number someone typed in from memory.
--
-- Verified against the landed 2024 data before the pipeline was built:
--   race points only     → 13 of 24 drivers mismatch
--   race + sprint points →  0 of 24 drivers mismatch

-- ─────────────────────────────────────────────────────────────────────────
-- 1. Championship reconciliation. MUST return zero rows.
-- ─────────────────────────────────────────────────────────────────────────
WITH earned AS (
  SELECT season, driver_id, SUM(total_points) AS points_from_results
  FROM f1.gold.driver_performance
  GROUP BY season, driver_id
),
final_round AS (
  SELECT season, MAX(round) AS last_round
  FROM f1.gold.championship_progression
  GROUP BY season
),
official AS (
  SELECT c.season, c.driver_id, c.cumulative_points AS points_from_standings
  FROM f1.gold.championship_progression c
  JOIN final_round f ON f.season = c.season AND f.last_round = c.round
)
SELECT
  o.season,
  o.driver_id,
  e.points_from_results,
  o.points_from_standings,
  e.points_from_results - o.points_from_standings AS delta
FROM official o
JOIN earned e ON e.season = o.season AND e.driver_id = o.driver_id
WHERE ABS(e.points_from_results - o.points_from_standings) > 1e-9
ORDER BY ABS(delta) DESC;


-- ─────────────────────────────────────────────────────────────────────────
-- 2. SCD-2 history exists. MUST return rows.
--    An empty result means dim_driver has no versioning and MVP item 3 is
--    NOT met, regardless of the pattern being implemented.
-- ─────────────────────────────────────────────────────────────────────────
SELECT
  driver_id,
  driver_name,
  constructor_id,
  constructor_name,
  __START_AT,
  __END_AT
FROM f1.silver.dim_driver
WHERE __END_AT IS NOT NULL
ORDER BY driver_id, __START_AT;


-- ─────────────────────────────────────────────────────────────────────────
-- 3. Every driver has exactly one current row in the SCD-2 dimension.
--    MUST return zero rows.
-- ─────────────────────────────────────────────────────────────────────────
SELECT driver_id, COUNT(*) AS current_rows
FROM f1.silver.dim_driver
WHERE __END_AT IS NULL
GROUP BY driver_id
HAVING COUNT(*) <> 1;


-- ─────────────────────────────────────────────────────────────────────────
-- 4. Deduplication worked: no duplicate natural keys anywhere in Silver.
--    MUST return zero rows. A non-zero count here means the open round landed
--    multiple snapshots and the dedupe window failed.
-- ─────────────────────────────────────────────────────────────────────────
SELECT 'fact_result' AS table_name, season, round, driver_id, COUNT(*) AS n
FROM f1.silver.fact_result GROUP BY 1,2,3,4 HAVING COUNT(*) > 1
UNION ALL
SELECT 'fact_qualifying', season, round, driver_id, COUNT(*)
FROM f1.silver.fact_qualifying GROUP BY 1,2,3,4 HAVING COUNT(*) > 1
UNION ALL
SELECT 'fact_driver_standing', season, round, driver_id, COUNT(*)
FROM f1.silver.fact_driver_standing GROUP BY 1,2,3,4 HAVING COUNT(*) > 1
UNION ALL
SELECT 'fact_sprint_result', season, round, driver_id, COUNT(*)
FROM f1.silver.fact_sprint_result GROUP BY 1,2,3,4 HAVING COUNT(*) > 1;


-- ─────────────────────────────────────────────────────────────────────────
-- 5. Gold row counts and season coverage — a quick sanity read.
-- ─────────────────────────────────────────────────────────────────────────
SELECT
  season,
  COUNT(DISTINCT round)      AS rounds,
  COUNT(DISTINCT driver_id)  AS drivers,
  COUNT(*)                   AS rows,
  SUM(total_points)          AS points_awarded,
  SUM(CASE WHEN dnf_flag THEN 1 ELSE 0 END) AS dnfs
FROM f1.gold.driver_performance
GROUP BY season
ORDER BY season;


-- ─────────────────────────────────────────────────────────────────────────
-- 6. Quarantine census. Expected to be empty on clean Jolpica data — corrupt
--    a landed file and re-run to prove the pattern actually catches things.
-- ─────────────────────────────────────────────────────────────────────────
SELECT 'race'                 AS source, COUNT(*) AS quarantined FROM f1.silver.quarantine_race
UNION ALL SELECT 'result',                COUNT(*) FROM f1.silver.quarantine_result
UNION ALL SELECT 'qualifying',            COUNT(*) FROM f1.silver.quarantine_qualifying
UNION ALL SELECT 'sprint_result',         COUNT(*) FROM f1.silver.quarantine_sprint_result
UNION ALL SELECT 'driver_standing',       COUNT(*) FROM f1.silver.quarantine_driver_standing
UNION ALL SELECT 'constructor_standing',  COUNT(*) FROM f1.silver.quarantine_constructor_standing;


-- ─────────────────────────────────────────────────────────────────────────
-- 7. The as-of join is doing real work: drivers whose Gold constructor differs
--    across the season. Should list mid-season team changes.
-- ─────────────────────────────────────────────────────────────────────────
SELECT season, driver_id, driver_name,
       COUNT(DISTINCT constructor_id_as_of_race) AS teams_in_season,
       COLLECT_SET(constructor_name_as_of_race)  AS teams
FROM f1.gold.driver_performance
GROUP BY season, driver_id, driver_name
HAVING COUNT(DISTINCT constructor_id_as_of_race) > 1
ORDER BY season, driver_id;

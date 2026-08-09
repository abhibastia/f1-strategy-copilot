"""Gold — business-ready marts.

Both marts join facts to the SCD-2 driver dimension **as of the race date**, so
a 2024 result for a driver who changed teams mid-season is attributed to the
team they actually drove for that weekend, not to their current one. That
as-of join is the reason the SCD-2 dimension exists; a current-row-only join
would silently rewrite history.

Dimensions are kept wide on purpose: every filter the dashboard offers has to
exist as a column here, and it is far easier to aggregate further in a query
than to recover a dimension that was aggregated away.
"""

from pyspark import pipelines as dp

# Catalog comes from the pipeline configuration so the bundle's `catalog`
# variable actually controls where datasets land. Falls back to `f1` when the
# pipeline is created outside the bundle.
CATALOG = spark.conf.get("f1.catalog", "f1")
BRONZE = f"{CATALOG}.bronze"
SILVER = f"{CATALOG}.silver"
GOLD = f"{CATALOG}.gold"

# Attach each fact row to the driver version that was current on race day.
# f-string: this fragment is interpolated into the queries below, and a nested
# {SILVER} in a plain string would be injected as literal text, not substituted.
_AS_OF_DRIVER = f"""
    LEFT JOIN {SILVER}.dim_driver d
           ON d.driver_id = f.driver_id
          AND f.race_date >= d.__START_AT
          AND (d.__END_AT IS NULL OR f.race_date < d.__END_AT)
"""


@dp.materialized_view(
    name=f"{GOLD}.driver_performance",
    comment=(
        "Driver performance per race: grid, finish, positions gained, points, "
        "reliability and fastest-lap flags. Grain: driver x race."
    ),
    table_properties={"quality": "gold"},
    cluster_by=["season", "round"],
)
def driver_performance():
    return spark.sql(f"""
        SELECT
            f.season,
            f.round,
            f.race_date,
            f.race_name,
            r.circuit_id,
            r.circuit_name,
            r.circuit_country,
            f.driver_id,
            f.driver_name,
            f.driver_code,
            f.driver_nationality,
            d.constructor_id            AS constructor_id_as_of_race,
            d.constructor_name          AS constructor_name_as_of_race,
            f.constructor_name          AS constructor_name_reported,
            f.grid_position,
            f.position                  AS finish_position,
            f.position_text,
            f.positions_gained,
            f.points                    AS race_points,
            -- Sprint points count toward the championship. Summing race points
            -- alone leaves 13 of 24 drivers short of their official 2024 total.
            COALESCE(sp.sprint_points, 0)               AS sprint_points,
            f.points + COALESCE(sp.sprint_points, 0)    AS total_points,
            sp.sprint_position,
            sp.sprint_grid_position,
            sp.sprint_position IS NOT NULL              AS is_sprint_weekend,
            f.laps_completed,
            f.status,
            f.dnf_flag,
            f.is_fastest_lap,
            f.fastest_lap_rank,
            f.fastest_lap_time,
            q.quali_position,
            q.quali_stage_reached,
            q.q3_millis,
            CASE WHEN q.quali_position IS NOT NULL AND f.position IS NOT NULL
                 THEN q.quali_position - f.position END AS quali_to_finish_delta,
            f.position = 1              AS is_win,
            f.position <= 3             AS is_podium,
            f.points > 0                AS is_points_finish
        FROM {SILVER}.fact_result f
        {_AS_OF_DRIVER}
        LEFT JOIN {SILVER}.dim_race r
               ON r.season = f.season AND r.round = f.round
        LEFT JOIN {SILVER}.fact_qualifying q
               ON q.season = f.season AND q.round = f.round AND q.driver_id = f.driver_id
        LEFT JOIN {SILVER}.fact_sprint_result sp
               ON sp.season = f.season AND sp.round = f.round AND sp.driver_id = f.driver_id
    """)


@dp.materialized_view(
    name=f"{GOLD}.championship_progression",
    comment=(
        "Driver championship state after every round: cumulative points, position, "
        "gap to leader, and round-over-round movement. Grain: driver x round."
    ),
    table_properties={"quality": "gold"},
    cluster_by=["season"],
)
def championship_progression():
    return spark.sql(f"""
        WITH standings AS (
            SELECT
                s.season,
                s.round,
                r.race_date,
                r.race_name,
                r.circuit_country,
                s.driver_id,
                s.driver_name,
                s.championship_position,
                s.cumulative_points,
                s.cumulative_wins,
                MAX(s.cumulative_points) OVER (PARTITION BY s.season, s.round)
                    AS leader_points,
                LAG(s.cumulative_points) OVER (
                    PARTITION BY s.season, s.driver_id ORDER BY s.round
                ) AS prev_cumulative_points,
                LAG(s.championship_position) OVER (
                    PARTITION BY s.season, s.driver_id ORDER BY s.round
                ) AS prev_championship_position
            FROM {SILVER}.fact_driver_standing s
            LEFT JOIN {SILVER}.dim_race r
                   ON r.season = s.season AND r.round = s.round
        )
        SELECT
            f.season,
            f.round,
            f.race_date,
            f.race_name,
            f.circuit_country,
            f.driver_id,
            f.driver_name,
            d.constructor_id            AS constructor_id_as_of_race,
            d.constructor_name          AS constructor_name_as_of_race,
            f.championship_position,
            f.cumulative_points,
            f.cumulative_wins,
            f.leader_points - f.cumulative_points        AS gap_to_leader,
            f.cumulative_points - COALESCE(f.prev_cumulative_points, 0)
                                                         AS points_gained_in_round,
            f.prev_championship_position - f.championship_position
                                                         AS position_change_vs_prev_round,
            f.championship_position = 1                  AS is_championship_leader
        FROM standings f
        {_AS_OF_DRIVER}
    """)

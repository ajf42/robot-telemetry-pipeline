-- "Which robots are struggling?" -- the question the whole pipeline exists to answer.
--
-- Compiled, not run: dbt's default analysis-paths is analyses/, so `dbt build`
-- never executes this. Render it with `dbt compile --profiles-dir .` and read
-- target/compiled/robot_telemetry/analyses/fleet_health_demo.sql, or paste the
-- body into the BigQuery console.
--
-- error_rate is errors per 100 completed cycles, so a busy robot is not
-- penalised for being busy. fleet_rank 1 is the worst unit and the top of the
-- maintenance queue. Read days_observed alongside avg_uptime_pct: a robot that
-- was dark for whole days can still average high uptime over the days it did
-- report.

select
    fleet_rank,
    robot_id,
    site_id,
    days_observed,
    total_cycles,
    total_errors,
    error_rate,
    avg_uptime_pct,
    window_start_date,
    window_end_date
from {{ ref('fct_fleet_health') }}
order by fleet_rank, robot_id

-- Which robots are struggling: a trailing 7-day rollup, one row per robot,
-- ranked worst-first by error rate.
--
-- The window ends on the last day present in int_robot_daily rather than on
-- current_date, so the table stays populated over a fixed historical load
-- instead of emptying once the data ages past a week.

{% set window_days = var('fleet_health_window_days') %}

with bounds as (

    select
        max(activity_date) as window_end_date,
        date_sub(max(activity_date), interval {{ window_days - 1 }} day) as window_start_date
    from {{ ref('int_robot_daily') }}

),

windowed as (

    select daily.*
    from {{ ref('int_robot_daily') }} as daily
    cross join bounds
    where daily.activity_date between bounds.window_start_date and bounds.window_end_date

),

rolled_up as (

    select
        robot_id,
        any_value(site_id) as site_id,
        (select window_start_date from bounds) as window_start_date,
        (select window_end_date from bounds) as window_end_date,

        count(*) as days_observed,
        sum(cycles_completed) as total_cycles,
        sum(error_count) as total_errors,

        -- Errors per 100 completed cycles. safe_divide yields null rather than
        -- an error for a robot that completed no cycles in the window.
        round(safe_divide(sum(error_count) * 100, sum(cycles_completed)), 2) as error_rate,

        round(avg(uptime_pct), 2) as avg_uptime_pct

    from windowed
    group by robot_id

)

select
    *,
    -- Worst error rate ranks 1. BigQuery sorts nulls last under `desc`, so a
    -- robot with no completed cycles (null error_rate) falls to the bottom
    -- rather than masquerading as the healthiest unit.
    rank() over (order by error_rate desc) as fleet_rank
from rolled_up

-- One row per robot per operating day.
--
-- The daily grain is the LOCAL date, not the UTC date. All three sites run
-- 10:00-22:00 US/Eastern, which spans two UTC dates, so grouping on
-- DATE(event_ts) in UTC would split every operating day into two partial rows
-- and halve the uptime figure.

{% set open_hour = var('operating_open_hour') -%}
{%- set close_hour = var('operating_close_hour') -%}
{%- set heartbeat_interval_s = var('heartbeat_interval_s') -%}

{#- Heartbeats a fully healthy robot should emit across one operating day. -#}
{%- set expected_heartbeats = (
        ((close_hour - open_hour) * 3600 / heartbeat_interval_s) | round | int
) %}

with events as (

    select
        robot_id,
        site_id,
        event_type,
        temp_c,
        cycle_duration_s,
        error_code,
        date(event_ts, '{{ var("operating_timezone") }}') as activity_date
    from {{ ref('stg_telemetry') }}

)

select
    robot_id,
    activity_date,

    -- Robots do not move between sites, so any_value preserves the one-row-per
    -- robot-per-day grain that grouping on site_id would risk breaking.
    any_value(site_id) as site_id,

    -- A cycle counts as completed when its end event lands. cycle_duration_s is
    -- populated on cook_cycle_end only, so avg() over it needs no filter.
    countif(event_type = 'cook_cycle_end') as cycles_completed,
    round(avg(cycle_duration_s), 1) as avg_cycle_duration_s,

    -- temp_c is null except on sensor_reading and cook_cycle_end; avg() skips nulls.
    round(avg(temp_c), 2) as avg_temp_c,

    countif(event_type = 'error') as error_count,

    -- The error codes are a closed set of five, so conditional aggregation in a
    -- single pass beats an ARRAY_AGG subquery. Emitted as a JSON string for a
    -- stable, easily-read column shape.
    to_json_string(struct(
        countif(error_code = 'E01_TEMP_SENSOR') as E01_TEMP_SENSOR,
        countif(error_code = 'E02_MOTOR_STALL') as E02_MOTOR_STALL,
        countif(error_code = 'E03_VISION_FAULT') as E03_VISION_FAULT,
        countif(error_code = 'E04_COMMS_TIMEOUT') as E04_COMMS_TIMEOUT,
        countif(error_code = 'E05_DOOR_JAM') as E05_DOOR_JAM
    )) as errors_by_code,

    countif(event_type = 'heartbeat') as heartbeat_count,

    -- Share of the {{ expected_heartbeats }} heartbeats expected across a
    -- {{ close_hour - open_hour }}-hour operating day at one every
    -- {{ heartbeat_interval_s }}s. Capped at 100: jitter around the interval can
    -- fit an extra beat into the window, which is not >100% availability.
    least(
        round(100.0 * countif(event_type = 'heartbeat') / {{ expected_heartbeats }}, 2),
        100.0
    ) as uptime_pct

from events
group by robot_id, activity_date

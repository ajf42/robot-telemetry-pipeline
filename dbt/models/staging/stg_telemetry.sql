-- One clean, unique row per telemetry event.
--
-- Column names and types already match the contract as loaded by
-- ingest/load_bigquery.py, so nothing is cast or renamed here. This model does
-- exactly two things: drops rows with no robot_id, and collapses duplicate
-- event_ids to the earliest-observed copy.

with source as (

    select * from {{ source('robot_telemetry', 'telemetry_raw') }}

),

deduplicated as (

    select
        event_id,
        robot_id,
        site_id,
        event_type,
        event_ts,
        temp_c,
        cycle_id,
        cycle_duration_s,
        error_code,
        firmware_version
    from source
    -- A null robot_id cannot be attributed to a unit, so it cannot roll up.
    where robot_id is not null
    -- Keep the earliest copy of a duplicated event_id. to_json_string breaks
    -- ties on identical timestamps so the choice is deterministic across runs
    -- rather than dependent on scan order.
    qualify row_number() over (
        partition by event_id
        order by event_ts asc, to_json_string(source) asc
    ) = 1

)

select * from deduplicated

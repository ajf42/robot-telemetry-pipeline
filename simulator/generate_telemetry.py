#!/usr/bin/env python3
"""Synthetic telemetry generator for a fleet of cooking robots.

Emits one JSON object per line (JSONL), sorted by ``event_ts``, matching the
flat event contract consumed by ``ingest/`` and the dbt staging layer.

The stream is deliberately imperfect: a configurable slice of rows carries one
of five injected data-quality defects, with nothing in the payload marking them
as such. Downstream validation is expected to catch them cold.

Determinism: a given ``--seed`` plus a given calendar date produce byte-identical
output. The generated window is the ``--days`` complete local days ending
yesterday, so the run date is the only moving part.

    python simulator/generate_telemetry.py --robots 15 --days 21 --seed 42
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# --- contract constants -----------------------------------------------------

SITES: tuple[str, ...] = ("PIT-01", "PIT-02", "CLE-01")
ERROR_CODES: tuple[str, ...] = (
    "E01_TEMP_SENSOR",
    "E02_MOTOR_STALL",
    "E03_VISION_FAULT",
    "E04_COMMS_TIMEOUT",
    "E05_DOOR_JAM",
)
FIRMWARE_VERSIONS: tuple[str, ...] = ("2.3.7", "2.4.0", "2.4.1", "2.5.0")
UNKNOWN_EVENT_TYPE = "diagnostic_ping"

TEMP_MIN_C, TEMP_MAX_C = 15.0, 260.0
CYCLE_MIN_S, CYCLE_MAX_S = 30, 1800

# --- simulation tuning ------------------------------------------------------

LOCAL_TZ = ZoneInfo("America/New_York")  # all three sites are US/Eastern
OPEN_HOUR, CLOSE_HOUR = 10, 22

HEARTBEAT_INTERVAL_S = 300
HEARTBEAT_JITTER_S = 40
SENSOR_INTERVAL_S = 90
SENSOR_JITTER_S = 15

CYCLES_PER_DAY = (16, 30)
RUSH_SHARE = 0.70  # fraction of cycles that start inside a rush window
LUNCH_RUSH = (11.5, 13.5)
DINNER_RUSH = (17.0, 19.5)

ERROR_RATE = 0.005  # errors as a share of all emitted events
PROBLEM_ROBOT_COUNT = 3
PROBLEM_ROBOT_WEIGHT = 6.0  # relative error likelihood vs. a healthy robot
CLUSTERED_CODE_WEIGHTS = (1.0, 5.0, 1.0, 1.0, 5.0)  # problem robots: E02/E05 heavy
BASELINE_CODE_WEIGHTS = (3.0, 0.5, 3.0, 3.0, 0.5)  # everyone else: E02/E05 rare

SAMPLE_LINES = 200
SAMPLE_FILENAME = "sample_telemetry.jsonl"


@dataclass(slots=True)
class TelemetryEvent:
    """One flat telemetry row. Field order matches the contract."""

    event_id: str
    robot_id: str | None
    site_id: str
    event_type: str
    event_ts: datetime
    temp_c: float | None
    cycle_id: str | None
    cycle_duration_s: int | None
    error_code: str | None
    firmware_version: str


@dataclass(slots=True, frozen=True)
class Robot:
    """Static per-robot profile, fixed for the whole run."""

    robot_id: str
    site_id: str
    firmware_version: str
    setpoint_c: float  # target cook temperature this unit holds
    activity: float  # multiplier on cycles/day; some units are just busier
    is_problem: bool


def new_id(rng: random.Random) -> str:
    """A uuid4-shaped id drawn from the seeded RNG (uuid.uuid4 is not seedable)."""
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def to_utc(day: date, hour_float: float) -> datetime:
    """Convert a local wall-clock hour on ``day`` to an aware UTC timestamp."""
    hour_float = min(max(hour_float, 0.0), 23.9997)
    total_s = int(round(hour_float * 3600))
    local = datetime.combine(
        day,
        time(total_s // 3600, (total_s % 3600) // 60, total_s % 60),
        tzinfo=LOCAL_TZ,
    )
    return local.astimezone(UTC)


def build_fleet(rng: random.Random, count: int) -> list[Robot]:
    """Assign sites, firmware, and personality to the fleet."""
    # Uneven site split (roughly 40/40/20) that still generalises to any count.
    site_cycle = ("PIT-01", "PIT-01", "PIT-02", "PIT-02", "CLE-01")
    ids = [f"ROBOT-{i:03d}" for i in range(1, count + 1)]
    problem_ids = set(rng.sample(ids, k=min(PROBLEM_ROBOT_COUNT, count)))

    fleet: list[Robot] = []
    for index, robot_id in enumerate(ids):
        fleet.append(
            Robot(
                robot_id=robot_id,
                site_id=site_cycle[index % len(site_cycle)],
                firmware_version=rng.choices(
                    FIRMWARE_VERSIONS, weights=(1.0, 2.0, 5.0, 1.5), k=1
                )[0],
                setpoint_c=round(rng.uniform(188.0, 206.0), 1),
                activity=rng.uniform(0.8, 1.2),
                is_problem=robot_id in problem_ids,
            )
        )
    return fleet


def cycle_start_hour(rng: random.Random) -> float:
    """Pick a local start hour, clustered on the lunch and dinner rushes."""
    if rng.random() < RUSH_SHARE:
        low, high = LUNCH_RUSH if rng.random() < 0.45 else DINNER_RUSH
        center = (low + high) / 2
        hour = rng.gauss(center, (high - low) / 4)
        return min(max(hour, low), high)
    return rng.uniform(OPEN_HOUR, CLOSE_HOUR - 0.5)


def cycle_duration(rng: random.Random) -> int:
    """Most cycles run a few minutes; a small tail runs long."""
    if rng.random() < 0.02:
        seconds = rng.uniform(900, CYCLE_MAX_S)
    else:
        seconds = rng.gauss(330, 140)
    return int(min(max(round(seconds), CYCLE_MIN_S), CYCLE_MAX_S))


def cook_temp(rng: random.Random, robot: Robot, progress: float) -> float:
    """Temperature ramps from preheat toward the setpoint over the cycle."""
    temp = robot.setpoint_c - 25.0 + 33.0 * progress + rng.gauss(0.0, 1.8)
    return round(min(max(temp, TEMP_MIN_C), TEMP_MAX_C), 1)


def generate_heartbeats(
    rng: random.Random, robot: Robot, day: date, events: list[TelemetryEvent]
) -> None:
    """One heartbeat roughly every five minutes across operating hours."""
    open_ts = to_utc(day, OPEN_HOUR)
    close_ts = to_utc(day, CLOSE_HOUR)
    ts = open_ts + timedelta(seconds=rng.randrange(HEARTBEAT_INTERVAL_S))
    while ts < close_ts:
        events.append(
            TelemetryEvent(
                event_id=new_id(rng),
                robot_id=robot.robot_id,
                site_id=robot.site_id,
                event_type="heartbeat",
                event_ts=ts,
                temp_c=None,
                cycle_id=None,
                cycle_duration_s=None,
                error_code=None,
                firmware_version=robot.firmware_version,
            )
        )
        step = HEARTBEAT_INTERVAL_S + rng.randint(-HEARTBEAT_JITTER_S, HEARTBEAT_JITTER_S)
        ts += timedelta(seconds=step)


def generate_cycles(
    rng: random.Random, robot: Robot, day: date, events: list[TelemetryEvent]
) -> None:
    """Cook cycles plus the sensor readings that ride alongside them."""
    low, high = CYCLES_PER_DAY
    count = max(1, int(round(rng.randint(low, high) * robot.activity)))
    close_ts = to_utc(day, CLOSE_HOUR)

    for _ in range(count):
        duration_s = cycle_duration(rng)
        start_ts = to_utc(day, cycle_start_hour(rng))
        end_ts = start_ts + timedelta(seconds=duration_s)
        if end_ts > close_ts:  # never let a cycle run past close
            continue
        cycle_id = new_id(rng)

        events.append(
            TelemetryEvent(
                event_id=new_id(rng),
                robot_id=robot.robot_id,
                site_id=robot.site_id,
                event_type="cook_cycle_start",
                event_ts=start_ts,
                temp_c=None,
                cycle_id=cycle_id,
                cycle_duration_s=None,
                error_code=None,
                firmware_version=robot.firmware_version,
            )
        )

        offset = SENSOR_INTERVAL_S + rng.randint(-SENSOR_JITTER_S, SENSOR_JITTER_S)
        while offset < duration_s:
            events.append(
                TelemetryEvent(
                    event_id=new_id(rng),
                    robot_id=robot.robot_id,
                    site_id=robot.site_id,
                    event_type="sensor_reading",
                    event_ts=start_ts + timedelta(seconds=offset),
                    temp_c=cook_temp(rng, robot, offset / duration_s),
                    cycle_id=cycle_id,
                    cycle_duration_s=None,
                    error_code=None,
                    firmware_version=robot.firmware_version,
                )
            )
            offset += SENSOR_INTERVAL_S + rng.randint(-SENSOR_JITTER_S, SENSOR_JITTER_S)

        events.append(
            TelemetryEvent(
                event_id=new_id(rng),
                robot_id=robot.robot_id,
                site_id=robot.site_id,
                event_type="cook_cycle_end",
                event_ts=end_ts,
                temp_c=cook_temp(rng, robot, 1.0),
                cycle_id=cycle_id,
                cycle_duration_s=duration_s,
                error_code=None,
                firmware_version=robot.firmware_version,
            )
        )


def generate_errors(
    rng: random.Random, fleet: list[Robot], days: list[date], base_count: int
) -> list[TelemetryEvent]:
    """Rare error events, weighted so E02/E05 pile up on a few problem robots."""
    target = round(base_count * ERROR_RATE / (1.0 - ERROR_RATE))
    robot_weights = [PROBLEM_ROBOT_WEIGHT if r.is_problem else 1.0 for r in fleet]

    errors: list[TelemetryEvent] = []
    for _ in range(target):
        robot = rng.choices(fleet, weights=robot_weights, k=1)[0]
        weights = CLUSTERED_CODE_WEIGHTS if robot.is_problem else BASELINE_CODE_WEIGHTS
        errors.append(
            TelemetryEvent(
                event_id=new_id(rng),
                robot_id=robot.robot_id,
                site_id=robot.site_id,
                event_type="error",
                event_ts=to_utc(rng.choice(days), rng.uniform(OPEN_HOUR, CLOSE_HOUR)),
                temp_c=None,
                cycle_id=None,
                cycle_duration_s=None,
                error_code=rng.choices(ERROR_CODES, weights=weights, k=1)[0],
                firmware_version=robot.firmware_version,
            )
        )
    return errors


def inject_defects(
    rng: random.Random,
    events: list[TelemetryEvent],
    defect_rate: float,
    future_anchor: datetime,
) -> int:
    """Corrupt a slice of rows in place, spreading the five defect types evenly.

    Nothing in the row records that it was touched -- that is the point.
    ``future_anchor`` is tonight's local midnight, so future-dated rows land in
    the future without making the output depend on the wall clock.
    """
    total = len(events)
    count = int(total * defect_rate)
    if count == 0:
        return 0

    # Out-of-range temperatures go on rows that already report one, so a
    # corrupted row carries exactly one defect rather than two overlapping ones.
    temp_pool = [i for i, e in enumerate(events) if e.temp_c is not None]
    temp_targets = rng.sample(temp_pool, k=min(count // 5, len(temp_pool)))
    for index in temp_targets:
        if rng.random() < 0.5:
            events[index].temp_c = round(rng.uniform(-60.0, -1.0), 1)
        else:
            events[index].temp_c = round(rng.uniform(401.0, 999.0), 1)

    taken = set(temp_targets)
    remaining = [i for i in range(total) if i not in taken]
    others = rng.sample(remaining, k=min(count - len(temp_targets), len(remaining)))
    for position, index in enumerate(others):
        event = events[index]
        match position % 4:
            case 0:  # duplicate event_id, borrowed from another row
                donor = rng.randrange(total)
                while donor == index:
                    donor = rng.randrange(total)
                event.event_id = events[donor].event_id
            case 1:  # null robot_id
                event.robot_id = None
            case 2:  # event_ts in the future
                event.event_ts = future_anchor + timedelta(
                    days=rng.randint(0, 29), seconds=rng.randrange(86400)
                )
            case _:  # unknown event_type
                event.event_type = UNKNOWN_EVENT_TYPE
    return len(temp_targets) + len(others)


def serialize(event: TelemetryEvent) -> str:
    row = asdict(event)
    row["event_ts"] = event.event_ts.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return json.dumps(row, separators=(",", ":"))


def generate(
    robots: int, days: int, defect_rate: float, seed: int
) -> tuple[list[TelemetryEvent], list[Robot], int]:
    rng = random.Random(seed)
    fleet = build_fleet(rng, robots)

    today_local = datetime.now(UTC).astimezone(LOCAL_TZ).date()
    window = [today_local - timedelta(days=offset) for offset in range(days, 0, -1)]
    future_anchor = to_utc(today_local + timedelta(days=1), 0.0)

    events: list[TelemetryEvent] = []
    for robot in fleet:
        for day in window:
            generate_heartbeats(rng, robot, day, events)
            generate_cycles(rng, robot, day, events)

    events.extend(generate_errors(rng, fleet, window, len(events)))
    events.sort(key=lambda e: (e.event_ts, e.event_id))

    defects = inject_defects(rng, events, defect_rate, future_anchor)
    events.sort(key=lambda e: (e.event_ts, e.event_id))
    return events, fleet, defects


def write_jsonl(path: Path, events: list[TelemetryEvent], limit: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = events if limit is None else events[:limit]
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for event in rows:
            handle.write(serialize(event) + "\n")


def summarize(events: list[TelemetryEvent], fleet: list[Robot], defects: int) -> str:
    counts: dict[str, int] = {}
    for event in events:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
    lines = [f"{len(events):,} events across {len(fleet)} robots"]
    for event_type, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {event_type:<18} {count:>7,}")
    lines.append(f"  {'(defective rows)':<18} {defects:>7,}")
    problems = ", ".join(r.robot_id for r in fleet if r.is_problem)
    lines.append(f"problem robots: {problems}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate synthetic robot fleet telemetry as JSONL.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--robots", type=int, default=15, help="fleet size")
    parser.add_argument("--days", type=int, default=21, help="days of history to generate")
    parser.add_argument(
        "--defect-rate", type=float, default=0.03, help="share of rows given a defect"
    )
    parser.add_argument("--seed", type=int, default=42, help="RNG seed")
    parser.add_argument(
        "--out", type=Path, default=Path("data/telemetry.jsonl"), help="output JSONL path"
    )
    args = parser.parse_args(argv)

    if args.robots < 1:
        parser.error("--robots must be at least 1")
    if args.days < 1:
        parser.error("--days must be at least 1")
    if not 0.0 <= args.defect_rate <= 1.0:
        parser.error("--defect-rate must be between 0.0 and 1.0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    events, fleet, defects = generate(args.robots, args.days, args.defect_rate, args.seed)

    write_jsonl(args.out, events)
    print(f"wrote {args.out}")

    sample_path = args.out.parent / SAMPLE_FILENAME
    if sample_path.resolve() != args.out.resolve():
        write_jsonl(sample_path, events, limit=SAMPLE_LINES)
        print(f"wrote {sample_path} (first {min(SAMPLE_LINES, len(events))} lines)")

    print(summarize(events, fleet, defects))
    return 0


if __name__ == "__main__":
    sys.exit(main())

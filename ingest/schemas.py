"""Pydantic v2 models expressing the telemetry contract.

Single source of truth for what a well-formed telemetry event looks like.
``ingest/validate.py`` uses it to split a raw JSONL stream into clean and
rejected rows; ``ingest/load_bigquery.py`` builds its table schema from the
same field definitions.

Four of the five defects the simulator injects are caught here, each by a
different rule:

    null robot_id           -> robot_id is a required, non-null string
    temp_c out of range     -> TEMP_MIN_C / TEMP_MAX_C bounds
    event_ts in the future  -> _event_ts_is_utc_and_past
    unknown event_type      -> the EventType enum

The fifth, duplicate event_id, is a cross-row property and cannot be seen
from a single event; ingest/validate.py tracks it across the file.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

# --- contract constants -----------------------------------------------------

FLEET_SIZE = 15
ROBOT_ID_PATTERN = re.compile(r"ROBOT-(\d{3})")
SEMVER_PATTERN = re.compile(r"\d+\.\d+\.\d+")

TEMP_MIN_C, TEMP_MAX_C = 15.0, 260.0
CYCLE_MIN_S, CYCLE_MAX_S = 30, 1800

#: Contract field order. Drives JSON output and the BigQuery schema.
FIELD_ORDER: tuple[str, ...] = (
    "event_id",
    "robot_id",
    "site_id",
    "event_type",
    "event_ts",
    "temp_c",
    "cycle_id",
    "cycle_duration_s",
    "error_code",
    "firmware_version",
)


class SiteId(StrEnum):
    PIT_01 = "PIT-01"
    PIT_02 = "PIT-02"
    CLE_01 = "CLE-01"


class EventType(StrEnum):
    COOK_CYCLE_START = "cook_cycle_start"
    COOK_CYCLE_END = "cook_cycle_end"
    SENSOR_READING = "sensor_reading"
    ERROR = "error"
    HEARTBEAT = "heartbeat"


class ErrorCode(StrEnum):
    E01_TEMP_SENSOR = "E01_TEMP_SENSOR"
    E02_MOTOR_STALL = "E02_MOTOR_STALL"
    E03_VISION_FAULT = "E03_VISION_FAULT"
    E04_COMMS_TIMEOUT = "E04_COMMS_TIMEOUT"
    E05_DOOR_JAM = "E05_DOOR_JAM"


# --- which nullable fields the contract populates on which event types ------

TEMP_C_ON = frozenset({EventType.SENSOR_READING, EventType.COOK_CYCLE_END})
CYCLE_DURATION_ON = frozenset({EventType.COOK_CYCLE_END})
ERROR_CODE_ON = frozenset({EventType.ERROR})
#: A cycle_id is mandatory on the start/end pair it identifies, and permitted
#: on sensor readings taken during that cycle. Nowhere else.
CYCLE_ID_REQUIRED_ON = frozenset({EventType.COOK_CYCLE_START, EventType.COOK_CYCLE_END})
CYCLE_ID_ALLOWED_ON = CYCLE_ID_REQUIRED_ON | {EventType.SENSOR_READING}

TempC = Annotated[float, Field(ge=TEMP_MIN_C, le=TEMP_MAX_C)]
CycleDurationS = Annotated[int, Field(ge=CYCLE_MIN_S, le=CYCLE_MAX_S)]


class TelemetryEvent(BaseModel):
    """One flat telemetry row, validated against the contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    robot_id: str
    site_id: SiteId
    event_type: EventType
    event_ts: datetime
    temp_c: TempC | None = None
    cycle_id: str | None = None
    cycle_duration_s: CycleDurationS | None = None
    error_code: ErrorCode | None = None
    firmware_version: str

    @field_validator("event_id", "cycle_id")
    @classmethod
    def _uuid4(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            parsed = UUID(value)
        except ValueError:
            raise ValueError("must be a uuid4 string") from None
        if parsed.version != 4:
            raise ValueError(f"must be a uuid4 string (got a uuid{parsed.version})")
        return value

    @field_validator("robot_id")
    @classmethod
    def _robot_in_fleet(cls, value: str) -> str:
        match = ROBOT_ID_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError('must match "ROBOT-NNN"')
        if not 1 <= int(match.group(1)) <= FLEET_SIZE:
            raise ValueError(f"must be in the fleet ROBOT-001..ROBOT-{FLEET_SIZE:03d}")
        return value

    @field_validator("firmware_version")
    @classmethod
    def _semver(cls, value: str) -> str:
        if SEMVER_PATTERN.fullmatch(value) is None:
            raise ValueError("must be semver MAJOR.MINOR.PATCH")
        return value

    @field_validator("event_ts")
    @classmethod
    def _event_ts_is_utc_and_past(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("must carry a UTC offset")
        value = value.astimezone(UTC)
        if value > datetime.now(UTC):
            raise ValueError("must not be in the future")
        return value

    @model_validator(mode="after")
    def _fields_match_event_type(self) -> Self:
        """Enforce which nullable fields belong on which event type."""
        problems: list[str] = []

        def check(name: str, value: object, populated_on: frozenset[EventType]) -> None:
            if self.event_type in populated_on and value is None:
                problems.append(f"{name} is required on {self.event_type}")
            elif self.event_type not in populated_on and value is not None:
                problems.append(f"{name} must be null on {self.event_type}")

        check("temp_c", self.temp_c, TEMP_C_ON)
        check("cycle_duration_s", self.cycle_duration_s, CYCLE_DURATION_ON)
        check("error_code", self.error_code, ERROR_CODE_ON)

        if self.event_type in CYCLE_ID_REQUIRED_ON and self.cycle_id is None:
            problems.append(f"cycle_id is required on {self.event_type}")
        elif self.event_type not in CYCLE_ID_ALLOWED_ON and self.cycle_id is not None:
            problems.append(f"cycle_id must be null on {self.event_type}")

        if problems:
            raise ValueError("; ".join(problems))
        return self

    @field_serializer("event_ts")
    def _serialize_event_ts(self, value: datetime) -> str:
        """Match the simulator's wire format: UTC, trailing Z."""
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def to_row(self) -> dict[str, object]:
        """The event as a contract-shaped, JSON-ready dict."""
        return self.model_dump(mode="json")

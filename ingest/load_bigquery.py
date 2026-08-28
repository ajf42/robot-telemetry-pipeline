#!/usr/bin/env python3
"""Load validated telemetry into robot_telemetry.telemetry_raw.

    pip install -e .        # puts the ingest package on the import path
    export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
    python ingest/load_bigquery.py --source data/clean.jsonl

Creates the dataset and table if they are absent. The table is partitioned by
DATE(event_ts) and clustered by robot_id, so the dbt layers can scan a day or a
robot without touching the whole history.

Default write disposition is append; --replace truncates the table in the same
job, so the table is never observably empty.

Authentication is service-account only, via GOOGLE_APPLICATION_CREDENTIALS.
No other auth path is attempted -- if that variable is unset, this exits.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from ingest.schemas import FIELD_ORDER

DEFAULT_DATASET = "robot_telemetry"
DEFAULT_TABLE = "telemetry_raw"
DEFAULT_LOCATION = "US"
DEFAULT_SOURCE = Path("data/clean.jsonl")

CREDENTIALS_ENV = "GOOGLE_APPLICATION_CREDENTIALS"
PARTITION_FIELD = "event_ts"
CLUSTERING_FIELDS = ["robot_id"]

#: The contract, as BigQuery types. Fields the contract calls nullable are
#: NULLABLE; the rest are REQUIRED, which is safe because ingest/validate.py
#: has already rejected every row that left a required field empty.
BQ_SCHEMA: list[bigquery.SchemaField] = [
    bigquery.SchemaField("event_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("robot_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("site_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("event_type", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("event_ts", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("temp_c", "FLOAT", mode="NULLABLE"),
    bigquery.SchemaField("cycle_id", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("cycle_duration_s", "INTEGER", mode="NULLABLE"),
    bigquery.SchemaField("error_code", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("firmware_version", "STRING", mode="REQUIRED"),
]

log = logging.getLogger("load_bigquery")

# The BigQuery schema and the pydantic contract must not drift apart.
assert tuple(field.name for field in BQ_SCHEMA) == FIELD_ORDER, (
    "BQ_SCHEMA does not match ingest.schemas.FIELD_ORDER"
)


def build_client(project: str | None) -> bigquery.Client:
    """Construct a client from the service-account file named by the env var."""
    credentials = os.environ.get(CREDENTIALS_ENV)
    if not credentials:
        raise SystemExit(
            f"{CREDENTIALS_ENV} is not set. Point it at a service-account JSON key:\n"
            f"    export {CREDENTIALS_ENV}=/path/to/service-account.json"
        )
    if not Path(credentials).is_file():
        raise SystemExit(f"{CREDENTIALS_ENV} points at {credentials}, which is not a file")
    return bigquery.Client(project=project)


def ensure_dataset(client: bigquery.Client, dataset: str, location: str) -> str:
    dataset_id = f"{client.project}.{dataset}"
    try:
        client.get_dataset(dataset_id)
        log.info("dataset %s exists", dataset_id)
    except NotFound:
        reference = bigquery.Dataset(dataset_id)
        reference.location = location
        client.create_dataset(reference)
        log.info("created dataset %s in %s", dataset_id, location)
    return dataset_id


def ensure_table(client: bigquery.Client, dataset_id: str, table: str) -> str:
    table_id = f"{dataset_id}.{table}"
    try:
        client.get_table(table_id)
        log.info("table %s exists", table_id)
    except NotFound:
        reference = bigquery.Table(table_id, schema=BQ_SCHEMA)
        reference.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY, field=PARTITION_FIELD
        )
        reference.clustering_fields = CLUSTERING_FIELDS
        client.create_table(reference)
        log.info(
            "created table %s partitioned by DATE(%s), clustered by %s",
            table_id,
            PARTITION_FIELD,
            ", ".join(CLUSTERING_FIELDS),
        )
    return table_id


def load(client: bigquery.Client, table_id: str, source: Path, replace: bool) -> int:
    """Run the load job and return the number of rows written."""
    disposition = (
        bigquery.WriteDisposition.WRITE_TRUNCATE
        if replace
        else bigquery.WriteDisposition.WRITE_APPEND
    )
    job_config = bigquery.LoadJobConfig(
        schema=BQ_SCHEMA,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=disposition,
        ignore_unknown_values=False,
        max_bad_records=0,
    )

    log.info("loading %s into %s (%s)", source, table_id, disposition)
    with source.open("rb") as handle:
        job = client.load_table_from_file(handle, table_id, job_config=job_config)
    job.result()  # raises on failure, surfacing BigQuery's own error detail

    return job.output_rows or 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load validated telemetry JSONL into BigQuery.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="clean JSONL to load")
    parser.add_argument("--project", default=None, help="GCP project (default: from credentials)")
    parser.add_argument("--dataset", default=DEFAULT_DATASET, help="BigQuery dataset")
    parser.add_argument("--table", default=DEFAULT_TABLE, help="BigQuery table")
    parser.add_argument("--location", default=DEFAULT_LOCATION, help="dataset location")
    parser.add_argument(
        "--replace", action="store_true", help="truncate the table before loading"
    )
    args = parser.parse_args(argv)

    if not args.source.is_file():
        parser.error(f"no such file: {args.source}")
    if args.source.stat().st_size == 0:
        parser.error(f"{args.source} is empty; nothing to load")
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    client = build_client(args.project)
    dataset_id = ensure_dataset(client, args.dataset, args.location)
    table_id = ensure_table(client, dataset_id, args.table)
    written = load(client, table_id, args.source, args.replace)

    total = client.get_table(table_id).num_rows
    log.info("loaded %s rows; %s now holds %s rows", f"{written:,}", table_id, f"{total:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

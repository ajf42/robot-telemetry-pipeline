# Architecture

One level below the [README](../README.md): the contract the whole pipeline is
built around, how the dbt layers divide the work, how the Argo DAG handles
failure and credentials, and an honest account of what this demo leaves out.

---

## The contract

Every stage agrees on one flat event shape — one row per telemetry event, no
nesting. `ingest/schemas.py` holds it as a pydantic v2 model and is the single
source of truth: `ingest/validate.py` validates against it, and
`ingest/load_bigquery.py` builds its BigQuery schema from the same field order,
with an `assert` at import time that the two have not drifted apart.

| Field | Type | Null? | Rule |
| --- | --- | --- | --- |
| `event_id` | STRING | no | uuid4, unique across the feed |
| `robot_id` | STRING | no | `ROBOT-NNN`, within the 15-robot fleet `ROBOT-001`–`ROBOT-015` |
| `site_id` | STRING | no | one of `PIT-01`, `PIT-02`, `CLE-01` |
| `event_type` | STRING | no | one of `cook_cycle_start`, `cook_cycle_end`, `sensor_reading`, `error`, `heartbeat` |
| `event_ts` | TIMESTAMP | no | UTC, must carry an offset, must not be in the future |
| `temp_c` | FLOAT | yes | 15.0–260.0; required on `sensor_reading` and `cook_cycle_end`, null elsewhere |
| `cycle_id` | STRING | yes | uuid4 shared by a start/end pair; required on both, allowed on `sensor_reading`, null elsewhere |
| `cycle_duration_s` | INTEGER | yes | 30–1800; required on `cook_cycle_end`, null elsewhere |
| `error_code` | STRING | yes | one of five `E0N_*` codes; required on `error`, null elsewhere |
| `firmware_version` | STRING | no | semver `MAJOR.MINOR.PATCH` |

Two things worth noting. The model is `extra="forbid"` and `frozen=True`, so an
unexpected field is a rejection rather than a silent pass-through. And the
per-event-type nullability rules are enforced by a model-level validator, not
just by column types: a `heartbeat` carrying a `temp_c` is as invalid as a
`cook_cycle_end` missing one. Types alone would accept both.

### The five injected defects

`simulator/generate_telemetry.py` corrupts roughly 3% of rows in place, spread
evenly across five failure modes, with nothing in the payload marking them.
Four are single-row properties and are caught by the schema. The fifth is not:

| Defect | Caught by |
| --- | --- |
| null `robot_id` | required non-null string |
| `temp_c` out of range (negative, or > 400) | `ge=15.0, le=260.0` bounds |
| `event_ts` in the future | `_event_ts_is_utc_and_past` validator |
| unknown `event_type` (`diagnostic_ping`) | `EventType` enum |
| duplicate `event_id` | **not the schema** — `validate.py` tracks ids across the file |

That last row is the interesting one. No single-row model can see a duplicate,
so `ingest/validate.py` carries a `dict` of `event_id → first line number` and
rejects the second occurrence. Only rows that already passed the schema register
an id, so a malformed row cannot evict a good one from the map.

### Measured behaviour

From the run used to write these docs (`--robots 15 --days 21 --seed 42`):

```
rows           84,740
clean          82,207  97.01%
rejected        2,533   2.99%

rejected by reason (a row may trip more than one):
  robot_id: Input should be a valid string                     509
  event_type: Input should be 'cook_cycle_start', ...          508
  event_ts: Value error, must not be in the future             508
  duplicate_event_id                                           500
  temp_c: Input should be less than or equal to 260            260
  temp_c: Input should be greater than or equal to 15          248
```

All five defect types are represented. The simulator reported 2,542 corrupted
rows against 2,533 rejections — a handful of injected defects land on rows where
they happen not to produce an invalid event.

---

## dbt layer strategy

Three layers, each with one job, materialised according to how often it is read
versus rebuilt.

### `stg_telemetry` — view

Does exactly two things: drops rows with a null `robot_id`, and collapses
duplicate `event_id`s to the earliest-observed copy via a `qualify row_number()`
window. Nothing is cast or renamed, because `ingest/load_bigquery.py` already
landed the columns in contract shape.

It is a **view** so it always reflects the current contents of `telemetry_raw`
without a rebuild. Its tests assert that the ingest contract held — unique and
non-null `event_id`, accepted values for `site_id`, `event_type` and
`error_code`, `accepted_range` on `temp_c` and `cycle_duration_s`. A failure
there does not mean the model needs a wider filter; it means bad rows reached
BigQuery by some path other than `ingest/validate.py`.

### `int_robot_daily` — table

One row per robot per operating day: cycles completed, average cycle duration,
average temperature, error count, a JSON breakdown by error code, heartbeat
count, and uptime.

**The grain is the local date, not the UTC date.** All three sites run
10:00–22:00 US/Eastern, a window that crosses midnight UTC, so grouping on
`DATE(event_ts)` in UTC would split every operating day into two partial rows
and halve the uptime figure. The model groups on
`date(event_ts, 'America/New_York')`, with the timezone as a dbt var.

`uptime_pct` is heartbeats received against the 144 expected across a 12-hour
day at one every 300 seconds, capped at 100 — jitter around the interval can fit
an extra beat into the window, which is not more than 100% availability. The
expected count is computed in Jinja from the same vars, so changing the
operating hours or the heartbeat cadence updates the denominator.

A robot that emits nothing on a given day produces **no row**. Absence is not
zero, which is why the mart reports `days_observed` alongside uptime.

### `fct_fleet_health` — table

The trailing 7-day rollup, one row per robot, ranked worst-first by `error_rate`
— errors per 100 completed cycles. Normalising by cycles is what stops a busy
robot from looking worse than an idle one at equal fault counts, and
`safe_divide` yields null rather than an error for a robot that completed no
cycles.

The window ends on `max(activity_date)` in `int_robot_daily` rather than on
`current_date`; the README's design-decisions section covers why. Ranking uses
`rank() over (order by error_rate desc)`, and because BigQuery sorts nulls last
under `desc`, a robot with no completed cycles falls to the bottom instead of
masquerading as the healthiest unit.

Both aggregate layers are **tables** rather than views: they are read far more
often than they are rebuilt, and the dashboard hits them on every panel refresh.

---

## The Argo DAG

`orchestration/workflow-template.yaml` defines a `WorkflowTemplate` named
`telemetry-pipeline` with four steps in a straight line:

```
generate → validate → load → dbt-build
```

Linear because each step's output file is the next step's input; there is
genuinely nothing to parallelise. It is a DAG rather than a shell script for the
three things shell scripts are bad at: knowing which step failed, retrying only
that step, and showing someone what ran without making them read the script.

### Data movement

A `volumeClaimTemplate` creates one PersistentVolumeClaim per run, mounted at
`/data` in all four pods, and garbage-collected with the workflow. The
alternative — an artifact repository, with every step uploading and
re-downloading — costs a storage bucket, an IAM policy, and controller config
before the first run does anything.

The tradeoff is real: a single `ReadWriteOnce` claim means all four pods must
land on the same node. That is fine on k3d and on a single-node runner, and the
point at which it stops being true is the point at which an artifact repository
is worth its setup cost anyway.

### Retry model

`retryStrategy: limit 2`, 30-second backoff doubling, on **`load` and
`dbt-build` only**. Those are the two steps that leave the cluster, so they are
the two that can fail for reasons unrelated to the data — a dropped connection,
a throttled API, a transient BigQuery error.

`generate` and `validate` deliberately have none. They are deterministic and
local: given the same inputs they produce the same outputs, so a retry can only
waste time. More pointedly, `validate` failing means the rejection rate crossed
its threshold — a real signal about the feed — and retrying it would delay the
alert while changing nothing.

`load` runs with `--replace`, which truncates and loads in the same BigQuery
job. That is what makes the retry safe: a second attempt after a half-finished
load cannot double-count rows, and the table is never observably empty in
between.

### Credential model

The service-account JSON lives in a Kubernetes secret named `gcp-sa-key`,
mounted read-only at `/secrets/gcp/key.json` with mode `0400` — into `load` and
`dbt-build` only. `generate` and `validate` get no credentials, because they
have no reason to touch GCP.

The GCP project id is read out of the mounted keyfile at run time unless the
`gcp-project` workflow parameter overrides it, so the usual submit needs no
parameters. dbt reaches the same keyfile through `env_var()` calls in
`orchestration/dbt-profiles.yml`, which is baked into the image at
`/root/.dbt/profiles.yml`. The image therefore contains no project id and no
secret in any layer.

Workflow pods run as the `telemetry-pipeline` ServiceAccount
(`orchestration/rbac.yaml`), whose Role grants two things: `create` and `patch`
on `argoproj.io/workflowtaskresults` — which Argo 3.4+ requires for a step to
report its own outcome — and `get` on `pods/log`. Nothing else.

### One image, four steps

All four steps run `robot-telemetry-pipeline:local`, built from the root
`Dockerfile`. Sharing one image is what stops the steps drifting onto different
versions of the contract: the pydantic model that validates a row and the schema
that loads it are the same file in the same layer. `dbt_utils` is vendored at
build time via `dbt deps`, so the dbt step needs no network beyond BigQuery and
a package-hub outage cannot fail a run.

---

## What I'd change for production

None of the following is here. Each is omitted deliberately, and each would
matter.

**Streaming ingest instead of batch file handoff.** Real robots emit
continuously; this pipeline generates a static historical window and moves it as
a file. Production would put events on Pub/Sub — or Kafka — with the robots
publishing directly, and either a subscriber writing to BigQuery or the Storage
Write API for streaming inserts. The contract in `ingest/schemas.py` survives
that change essentially unaltered, which is the point of keeping it in one
place; what changes is the transport, the need for idempotency on replay, and
handling events that arrive late or out of order. The current design has no
concept of late arrival at all — it assumes the whole window is present before
`validate` runs.

**Incremental dbt models.** `int_robot_daily` and `fct_fleet_health` are both
full rebuilds. At 85,000 rows that is free; at a year of a real fleet it is
neither fast nor cheap. Production would make `int_robot_daily` an incremental
model keyed on `activity_date`, reprocessing only recent partitions and
tolerating a lookback window for late data. That brings its own problems — a
backfill after a logic change becomes a deliberate operation rather than the
default — which is exactly why it is not worth doing at this size.

**Data contracts enforced in CI.** Right now `ingest/schemas.py` and
`BQ_SCHEMA` are kept in step by a runtime `assert`, and the dbt tests only run
when the pipeline runs — after data has already landed. Production would enforce
the contract before merge: dbt model contracts pinning column names and types,
schema tests in CI against a scratch dataset, and a check that fails the build
when the pydantic model and the warehouse schema diverge. A contract that is
only checked at runtime is a contract you discover has broken from a dashboard.

**Alerting on the signals that already exist.** The pipeline generates two good
alerts and acts on neither: the rejection rate that `validate` computes, and the
`error_rate` ranking in `fct_fleet_health`. Production would page on a rejection
rate crossing its threshold, on the workflow failing after its retries, and on a
robot entering the top of the fleet ranking — that last one being the actual
business signal, a unit that needs a technician. The Grafana dashboard shows all
of this and notifies nobody.

**Beyond those four.** Secrets should come from Secret Manager via Workload
Identity rather than a mounted JSON key, which is the single biggest security
gap here. Scheduling needs a `CronWorkflow`. The image needs a registry, a
non-root user, and a digest pin rather than `:local` with
`imagePullPolicy: Never`. Grafana needs provisioning-as-code so the datasource
and dashboard are reproducible rather than clicked together. And the Python has
no unit tests — the validator's behaviour is asserted only by the demo data
happening to exercise it.

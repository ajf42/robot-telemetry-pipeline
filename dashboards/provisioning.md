# Grafana provisioning — fleet health dashboard

Manual import path: run Grafana in Docker, add the BigQuery data source by hand,
import [fleet-health.json](fleet-health.json). No provisioning directory, no
alert rules — this is the walkthrough you can do live in front of someone.

Pinned versions:

| Component | Version |
| --------- | ------- |
| Grafana | `11.1.0` (OSS image) |
| BigQuery plugin | `grafana-bigquery-datasource` (latest) |
| Dashboard schema | `schemaVersion: 39` |

> **Shell note.** Every command is a single line so it pastes cleanly into
> PowerShell, bash, or zsh.

---

## 0. Prerequisites

- Docker running.
- The service-account JSON keyfile from
  [orchestration/SETUP.md](../orchestration/SETUP.md) step 4 — the same file.
- The pipeline has run at least once, so all three models exist in BigQuery.

Confirm the tables are there before touching Grafana:

```bash
bq ls robot_telemetry
```

You want to see `telemetry_raw`, `stg_telemetry`, `int_robot_daily`, and
`fct_fleet_health`. If `fct_fleet_health` is missing, the dbt step has not run
and every panel will import fine but render empty.

**IAM.** The service account needs `roles/bigquery.dataViewer` and
`roles/bigquery.jobUser` on the project. The pipeline's key already has
`dataEditor` (which contains `dataViewer`) and `jobUser`, so it works as-is.
`roles/browser` is optional — it only populates the project *dropdown* in the
data source form, and this setup types the project id in directly.

---

## 1. Start Grafana with the BigQuery plugin

```bash
docker run -d --name grafana -p 3000:3000 -v grafana-storage:/var/lib/grafana -e GF_INSTALL_PLUGINS=grafana-bigquery-datasource grafana/grafana:11.1.0
```

`GF_INSTALL_PLUGINS` downloads the plugin on first boot, so this needs outbound
network the first time. The named volume keeps the data source and the imported
dashboard across `docker restart`.

Wait for startup, then confirm the plugin actually landed:

```bash
docker exec grafana ls /var/lib/grafana/plugins
```

That must list `grafana-bigquery-datasource`. If the directory is empty, see
troubleshooting below.

Open <http://localhost:3000> and log in with `admin` / `admin`. Grafana will ask
you to set a new password; skip is fine for local.

---

## 2. Connect the data source

In the UI:

1. **Connections → Data sources → Add new data source**.
2. Search for **BigQuery** and choose **Google BigQuery**.
3. Under **Authentication**, set the type to **Google JWT File**.
4. **Upload** the service-account JSON keyfile, or paste its contents into the
   field. Grafana splits it into the client email, token URI, and private key;
   the private key is stored encrypted and never shown again.
5. Set **Default project** to the GCP project holding the `robot_telemetry`
   dataset.
6. Set **Processing location** to **US** — this must match the dataset location
   that `ingest/load_bigquery.py` created (`DEFAULT_LOCATION = "US"`). A
   mismatch here is the single most common cause of "dataset not found".
7. Click **Save & test**. You want *"Data source is working"*.

---

## 3. Import the dashboard

1. **Dashboards → New → Import**.
2. **Upload dashboard JSON file** and pick `dashboards/fleet-health.json`.
3. Click **Load**, then **Import**.

The dashboard binds to your data source through a variable rather than a
hard-coded UID, so there is nothing to search-and-replace in the JSON. Two
dropdowns sit at the top of the dashboard once it opens:

- **Data source** — pick the BigQuery data source you just created. It
  auto-selects the first BigQuery data source, so usually this is already right.
- **GCP project** — a text box, shipped with the placeholder
  `your-gcp-project-id`.

## 4. Point it at your project

Replace `your-gcp-project-id` in the **GCP project** box with your real project
id and press **Enter**. Every panel query is fully qualified as
`` `${project}.robot_telemetry.<table>` ``, so nothing renders until this is set.

Then **save the dashboard** (the disk icon, or `Ctrl+S`) so the value persists.
Without saving, the box resets to the placeholder on reload.

---

## 5. Verify all four panels

| Panel | What "working" looks like |
| --- | --- |
| **Fleet at a glance** (4 stats) | Fleet size reads `15`. Cycles lands around 2,400 for a default 21-day run. Error rate is a single-digit number with a `/100 cyc` suffix. Avg uptime sits just under 100%. |
| **Robot health ranking** | 15 rows, `Rank` ascending. |
| **Daily errors by robot** | Up to 15 lines, legend on the right listing robot ids. |
| **Fleet cook temperature** | One dark blue average line inside a pale blue band. |

**The problem robots.** The simulator deliberately concentrates `E02_MOTOR_STALL`
and `E05_DOOR_JAM` failures on three units, weighting them six-to-one against the
rest of the fleet. On a default 21-day run those three sit at ranks 1–3 with an
`Error rate` near **18** — solidly **red** — while the other twelve land around
**3**, which colours amber. So the read is a red block of three at the top over
an amber tail, not red-over-green. If nothing is red, check you loaded a full
21-day run rather than a one-day smoke test.

**On the time picker.** The four stat panels and the table read
`fct_fleet_health`, which is already a fixed trailing 7-day rollup ending on the
last day *in the data*, so they deliberately ignore the dashboard time range.
Only the two time-series panels use `$__timeFilter`. The default range is
`now-30d → now` so a 21-day generated history falls inside it — this is not a
broken time picker, it is the mart doing its own windowing.

**On the timezone.** The dashboard is pinned to UTC. `int_robot_daily` keys on
the *local* (America/New_York) operating date, cast to midnight UTC; rendering
in browser-local time would shift every point back a day for anyone west of
Eastern.

---

## Dependencies

Nothing was added to `pyproject.toml` for this layer. The dashboard is a JSON
document and a Grafana plugin — neither is a Python runtime dependency, and the
project's package list stays limited to what the CLIs actually import. The only
new dependency is the `grafana-bigquery-datasource` plugin, installed by the
`GF_INSTALL_PLUGINS` variable in step 1.

---

## Tear down

```bash
docker rm -f grafana
```

```bash
docker volume rm grafana-storage
```

The second command discards the data source and the imported dashboard; skip it
to keep them for next time.

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Plugin directory empty after step 1 | The container had no network on first boot. Run `docker exec grafana grafana-cli plugins install grafana-bigquery-datasource` then `docker restart grafana`. |
| `Not found: Table your-gcp-project-id:robot_telemetry...` | The **GCP project** variable is still the placeholder. Redo step 4. |
| `Not found: Dataset ... was not found in location EU` | **Processing location** on the data source is not `US`. Fix it in step 2.6. |
| All panels empty, no error | The dbt models exist but hold no rows. Re-run the pipeline; check `bq query "SELECT COUNT(*) FROM robot_telemetry.fct_fleet_health"`. |
| Time-series panels empty, stats fine | Dashboard time range excludes the generated window. The data ends yesterday — set the range to **Last 30 days**. |
| `403 Access Denied` | Service account is missing `bigquery.jobUser`, or the key belongs to a different project than the one in the variable. |
| Legend on **Daily errors by robot** shows blank names | The driver did not attach `robot_id` as a label. Edit the panel and clear the **Standard options → Display name** field (`${__field.labels.robot_id}`); series fall back to their full frame names. |
| No band on the temperature panel, just three lines | The `Fill below to` override lost its target. Re-set it on **Fleet max** to **Fleet min**. |
| Table renders but no red cells | Either the run was too short to accumulate errors, or the `Error rate` column was renamed. The colouring override matches the column by the exact name `Error rate`. |

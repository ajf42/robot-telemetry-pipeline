# Running the pipeline on a local k3d cluster

Everything below is copy-pasteable, in order, from the repo root. Start from a
machine with Docker running and `kubectl` on the path; steps 0–1 install the
other two tools.

Pinned versions, changed in one place each:

| Component      | Version   |
| -------------- | --------- |
| Argo Workflows | `v3.6.5`  |
| k3d cluster    | `telemetry` |
| Image tag      | `robot-telemetry-pipeline:local` |

> **Shell note.** Commands are written for bash/zsh and are single-line wherever
> possible so they paste cleanly into PowerShell too. The two places that differ
> are called out inline.

---

## 0. Install k3d and the Argo CLI

**macOS / Linux**

```bash
curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
```

```bash
curl -sLO https://github.com/argoproj/argo-workflows/releases/download/v3.6.5/argo-darwin-amd64.gz && gunzip -f argo-darwin-amd64.gz && chmod +x argo-darwin-amd64 && sudo mv argo-darwin-amd64 /usr/local/bin/argo
```

On Linux swap `argo-darwin-amd64` for `argo-linux-amd64`; on Apple Silicon use
`argo-darwin-arm64`.

**Windows (PowerShell)** — via [Scoop](https://scoop.sh):

```powershell
scoop install k3d argo
```

Without Scoop, k3d ships a Windows binary on its releases page, and the Argo CLI
asset is gzipped, which PowerShell can unpack in one line:

```powershell
Invoke-WebRequest https://github.com/argoproj/argo-workflows/releases/download/v3.6.5/argo-windows-amd64.gz -OutFile argo.gz
```

```powershell
$in=[IO.File]::OpenRead("$PWD\argo.gz"); $out=[IO.File]::Create("$PWD\argo.exe"); $gz=New-Object IO.Compression.GzipStream($in,[IO.Compression.CompressionMode]::Decompress); $gz.CopyTo($out); $gz.Dispose(); $out.Dispose(); $in.Dispose()
```

Then move `argo.exe` somewhere on your `PATH`.

Confirm both are live:

```bash
k3d version
```

```bash
argo version --short
```

---

## 1. Create the cluster

```bash
k3d cluster create telemetry --agents 1 --wait
```

```bash
kubectl cluster-info
```

```bash
kubectl get nodes
```

`k3d cluster create` also points your kubeconfig at the new cluster, so every
`kubectl` below lands in the right place. The cluster ships with the
`local-path` storage class, which is what backs the pipeline's shared volume.

---

## 2. Install Argo Workflows (pinned)

```bash
kubectl create namespace argo
```

```bash
kubectl apply -n argo -f https://github.com/argoproj/argo-workflows/releases/download/v3.6.5/quick-start-minimal.yaml
```

```bash
kubectl -n argo rollout status deploy/workflow-controller --timeout=180s
```

```bash
kubectl -n argo rollout status deploy/argo-server --timeout=180s
```

`quick-start-minimal` is the variant with **no** bundled MinIO and no artifact
repository — this pipeline passes data on a volume instead, so there is nothing
further to configure. To move to a newer Argo, change the version in both the
URL above and the table at the top; releases are listed at
<https://github.com/argoproj/argo-workflows/releases>.

---

## 3. Build the image and import it into k3d

```bash
docker build -t robot-telemetry-pipeline:local .
```

```bash
k3d image import robot-telemetry-pipeline:local -c telemetry
```

The import step is not optional. k3d nodes run their own containerd, separate
from your Docker daemon, and the workflow sets `imagePullPolicy: Never` so a
missing import fails loudly with `ErrImageNeverPull` instead of silently
reaching out to Docker Hub for an image that does not exist there.

Re-run **both** commands after any change to the Python CLIs, the dbt models, or
the Dockerfile.

Quick sanity check that the image can see its own code:

```bash
docker run --rm robot-telemetry-pipeline:local python simulator/generate_telemetry.py --help
```

---

## 4. Create the GCP credentials secret

You need a service-account JSON keyfile whose account can create datasets,
tables, and jobs in the target project (`roles/bigquery.dataEditor` +
`roles/bigquery.jobUser` is enough).

```bash
kubectl -n argo create secret generic gcp-sa-key --from-file=key.json=/absolute/path/to/service-account.json
```

**Windows (PowerShell)** — same command, Windows path:

```powershell
kubectl -n argo create secret generic gcp-sa-key --from-file=key.json=C:\path\to\service-account.json
```

The key name `key.json` matters: the workflow mounts the secret at
`/secrets/gcp` and points `GOOGLE_APPLICATION_CREDENTIALS` at
`/secrets/gcp/key.json`. Verify it landed:

```bash
kubectl -n argo describe secret gcp-sa-key
```

The output must list a key named `key.json`; if it lists anything else, delete
the secret and redo the command above.

The GCP project id is read out of that keyfile at run time, so there is nothing
else to configure. To target a different project than the key's own, pass
`-p gcp-project=other-project-id` at submit time.

---

## 5. Apply RBAC and the workflow template

```bash
kubectl apply -n argo -f orchestration/rbac.yaml
```

```bash
argo lint orchestration/workflow-template.yaml
```

```bash
kubectl apply -n argo -f orchestration/workflow-template.yaml
```

```bash
argo template list -n argo
```

`rbac.yaml` creates the `telemetry-pipeline` ServiceAccount that every step pod
runs as, with permission to do exactly two things: report its own step result
and read its own logs.

---

## 6. Submit and watch

```bash
argo submit -n argo --from workflowtemplate/telemetry-pipeline --watch
```

With non-default parameters:

```bash
argo submit -n argo --from workflowtemplate/telemetry-pipeline -p days=7 -p defect-rate=0.05 --watch
```

Follow the logs of the run in flight, or of a finished one:

```bash
argo logs -n argo @latest --follow
```

```bash
argo get -n argo @latest
```

```bash
argo list -n argo
```

For the web UI:

```bash
kubectl -n argo port-forward svc/argo-server 2746:2746
```

Then open <https://localhost:2746> and accept the self-signed certificate. The
quick-start install runs the server in `server` auth mode, so no token is needed
locally.

Confirm the data landed, if you have the `bq` CLI configured:

```bash
bq query --use_legacy_sql=false "SELECT COUNT(*) AS rows, COUNT(DISTINCT robot_id) AS robots FROM robot_telemetry.telemetry_raw"
```

---

## 7. Tear down

```bash
k3d cluster delete telemetry
```

That removes the cluster, the Argo install, the secret, and every run's volume
in one shot. Nothing outside the cluster is touched — the BigQuery dataset
survives, because the pipeline built it.

---

## What each step does

A walkthrough of the DAG in `workflow-template.yaml`, in the order it runs.

**The shape.** Four steps, strictly sequential: `generate → validate → load →
dbt-build`. There is no fan-out here, and the linear shape is the honest one —
each step's output file is literally the next step's input, so there is nothing
to parallelize. It is written as a DAG rather than a shell script because the
three things a shell script is bad at are the three things that matter in
operations: knowing which step failed, retrying only that step, and showing
someone else what ran without making them read the script.

**How data moves.** Argo's usual answer is an artifact repository — S3 or GCS,
with every step uploading and re-downloading its files. This pipeline uses a
`volumeClaimTemplate` instead: one PersistentVolumeClaim per run, mounted at
`/data` in all four pods. It costs one line of YAML instead of a storage bucket,
an IAM policy, and a controller config, and it means a fresh clone runs on a
laptop with no cloud storage at all. The tradeoff is that all four steps must
land on the same node — fine for k3d and for a single-node runner, and the point
at which that stops being true is the point at which you want an artifact
repository anyway.

**1. `generate`** runs `simulator/generate_telemetry.py` and writes
`/data/telemetry.jsonl` — 21 days of history for a 15-robot fleet across three
sites, on a fixed seed so the same day produces the same bytes. Roughly 3% of
rows carry one of five deliberately injected defects: a duplicate `event_id`, a
null `robot_id`, a `temp_c` outside 15–260, an `event_ts` in the future, or an
`event_type` of `diagnostic_ping` that is not in the contract. Nothing in the
payload flags a bad row. That is the whole point — the next step has to catch
them cold.

**2. `validate`** runs every row through the pydantic model in
`ingest/schemas.py` and splits the file in two: `/data/clean.jsonl` and
`/data/rejected.jsonl`, where each rejection keeps the original row alongside
the reasons it failed. Duplicate `event_id`s are caught here too, which no
single-row schema can do — it takes a pass over the whole file. The step exits
non-zero if more than 10% of rows reject, so a feed that has genuinely degraded
stops here rather than quietly poisoning the warehouse. Note that this step has
**no** retry: a file bad enough to fail this check will fail it again, and
retrying only delays the alert.

**3. `load`** pushes `clean.jsonl` into `robot_telemetry.telemetry_raw`,
creating the dataset and table if they are absent, partitioned by
`DATE(event_ts)` and clustered by `robot_id` so the dbt layers can scan one day
or one robot without touching the full history. This is the first step that
leaves the cluster, so it is the first that can fail for reasons that have
nothing to do with the data — a dropped connection, a throttled API. Hence
`retryStrategy: limit 2` with a 30-second backoff that doubles. It runs with
`--replace`, which truncates and loads in the same job, so the step is
idempotent: a retry after a half-finished load cannot double-count rows, and the
table is never observably empty in between.

**4. `dbt-build`** runs `dbt build` over the three layers in dependency order —
`stg_telemetry` (a view, so it always reflects the raw table) →
`int_robot_daily` → `fct_fleet_health` — and runs every schema test in the same
pass. `build` rather than `run` is deliberate: a test failure stops the models
downstream of it instead of letting a broken number reach the dashboard. Same
retry policy as `load`, for the same reason. `dbt_utils` is vendored into the
image at build time, so this step needs no network beyond BigQuery itself and a
package-hub outage cannot fail a run.

**Credentials.** Only steps 3 and 4 talk to GCP, and only those two mount the
`gcp-sa-key` secret — read-only, mode `0400`, at `/secrets/gcp/key.json`. The
generate and validate pods have no credentials at all, because they have no
reason to. dbt reads the same keyfile through `env_var()` calls in
`orchestration/dbt-profiles.yml`, so the image holds no project id and no
secrets in any layer.

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `ErrImageNeverPull` on the first pod | The image was not imported. Re-run step 3 — both commands. |
| Pod stuck `Pending`, PVC unbound | The cluster lost its storage class. `kubectl get sc` should show `local-path` as default. |
| `validate` exits 1, `rejection rate ... exceeds` | Expected if you raised `-p defect-rate` above `0.10`. The gate is doing its job; lower the defect rate. |
| `load` fails with `DefaultCredentialsError` | The secret is missing or its key is not named `key.json`. Redo step 4. |
| `load` or `dbt-build` fails `403 Access Denied` | The service account lacks `bigquery.dataEditor` / `bigquery.jobUser` on the project. |
| `dbt-build` fails `Env var required but not provided: DBT_GCP_PROJECT` | The keyfile has no `project_id`. Pass `-p gcp-project=<id>` at submit. |
| Steps run but the workflow reports `Error` | `rbac.yaml` was not applied, so pods cannot write their `workflowtaskresults`. |
| Logs for a finished run are gone | Pods are garbage-collected. Use `argo logs -n argo <workflow-name>` while it exists, or the UI. |

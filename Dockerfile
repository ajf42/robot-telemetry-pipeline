# Runtime image for every stage of the telemetry pipeline.
#
# One image, four entrypoints: the Argo DAG in orchestration/workflow-template.yaml
# runs simulator/, ingest/, and dbt out of this same layer stack, so the four
# steps can never drift onto different versions of the contract.
#
#   docker build -t robot-telemetry-pipeline:local .
#
FROM python:3.11-slim

# PYTHONPATH is load-bearing: ingest/ and simulator/ are implicit namespace
# packages with no __init__.py, so `from ingest.schemas import ...` resolves
# from /app rather than depending on how the wheel laid them out.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DBT_PROFILES_DIR=/root/.dbt

WORKDIR /app

# dbt first and alone: it is by far the heaviest install and it changes only
# when this pin changes, so it stays cached across ordinary source edits.
RUN pip install --no-cache-dir "dbt-core>=1.9,<1.10" "dbt-bigquery>=1.9,<1.10"

# Project source. Copied explicitly rather than `COPY . .` so build context
# noise (data/, .venv/, dbt/target/) cannot leak into the image.
COPY pyproject.toml ./
COPY simulator/ simulator/
COPY ingest/ ingest/
COPY dbt/ dbt/

# Installs pydantic and the BigQuery client from pyproject.toml, which stays the
# single source of truth for the project's runtime dependencies.
RUN pip install --no-cache-dir .

# Vendor dbt_utils at build time so the dbt step needs no network beyond
# BigQuery itself, and so a package-hub outage cannot fail a pipeline run.
# Deliberately ahead of the profile copy: the profile resolves env_var() calls
# that only exist at run time, so nothing at build time should try to render it.
WORKDIR /app/dbt
RUN dbt deps
WORKDIR /app

# dbt's profile is rendered from the environment at run time; see the env_var
# calls in orchestration/dbt-profiles.yml.
COPY orchestration/dbt-profiles.yml /root/.dbt/profiles.yml

# /data is where the shared volume claim gets mounted; create it so the image
# is also runnable standalone with `docker run -v $PWD/data:/data`.
RUN mkdir -p /data

CMD ["python", "-c", "print('robot-telemetry-pipeline: run a stage, e.g. python simulator/generate_telemetry.py --help')"]

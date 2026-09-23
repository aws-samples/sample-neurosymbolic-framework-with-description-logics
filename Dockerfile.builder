# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Dockerfile.builder — the on-demand build job container (ECS Fargate RunTask).
#
# Runs `ontology-qa-build <name>` which evolves a query_program.py over up to
# 20 iterations. It shells out to `kiro-cli` (ontology_qa_cli/fixer.py) to fix
# the program each round, and calls Bedrock (judge). Long-running, spawns
# parallel subprocesses — a Fargate task launched on demand, NOT Lambda.
# See docs/deployment-cdk.md.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY ontology_qa_cli/ ./ontology_qa_cli/
COPY ontology_config.yaml ./ontology_config.yaml
RUN uv sync --frozen

# ===========================================================================
# !!! UNRESOLVED ASSUMPTION — see Task 0 in docs/deployment-cdk.md !!!
#
# The builder invokes `kiro-cli` as a subprocess (ontology_qa_cli/fixer.py).
# It MUST be present on PATH inside this image for a build to succeed.
#
# Whether kiro-cli can be installed into a container in this environment is
# NOT YET CONFIRMED and is the single biggest risk to the split architecture.
# Fill in the real install below once confirmed, e.g.:
#
#   RUN curl -fsSL <kiro-cli-install-url> | sh
#   # or COPY a vendored binary:
#   # COPY vendor/kiro-cli /usr/local/bin/kiro-cli
#   # RUN chmod +x /usr/local/bin/kiro-cli
#
# Until this is real, the builder task will fail at the fix step. If kiro-cli
# cannot be containerized, the build half needs a rethink (run builds outside
# AWS and serve only pre-built programs).
# ===========================================================================
# RUN <install kiro-cli here>

# Runtime config (overridable by the ECS task definition):
#   ONTOLOGY_QA_CACHE_DIR  - EFS mount, shared with the web service
#   BEDROCK_REGION / MODEL - see Task 2
#
# The ontology name + build args are passed as a container command override by
# the web service's ecs.run_task call (Task 6), e.g.:
#   ["ontology_qa_cli.builder", "FMA", "--plain",
#    "--ontology-config", "ontology_config.yaml", "--output-dir", "runs/"]
ENTRYPOINT ["uv", "run", "python", "-m"]
CMD ["ontology_qa_cli.builder", "--help"]

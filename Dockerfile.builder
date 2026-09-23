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
# kiro-cli — the builder invokes it as a subprocess (ontology_qa_cli/fixer.py)
# to fix the query program each iteration. It must be on PATH.
#
# CONFIRMED containerizable: kiro-cli ships a Linux build and, since 2.x, a
# headless mode that authenticates from an API key in KIRO_API_KEY (no browser,
# no SSO session to expire) — the natural fit for a Fargate task. See
# https://kiro.dev/docs/cli/headless/ and the auth notes in docs/deployment-cdk.md.
#
# curl|bash is used because that is the officially documented install path.
# Pin/verify it for a locked-down build (mirror the installer, or vendor the
# release binary and COPY it in) rather than fetching latest at build time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://cli.kiro.dev/install | bash \
    && ( command -v kiro-cli >/dev/null 2>&1 \
         || ln -s "$(find / -name kiro-cli -type f 2>/dev/null | head -1)" /usr/local/bin/kiro-cli ) \
    && kiro-cli --version

# Auth is a RUNTIME secret, never baked into the image. The ECS task definition
# injects KIRO_API_KEY from Secrets Manager (headless mode reads it directly);
# a browser/device-flow login is not possible in a Fargate task.
ENV KIRO_API_KEY=""

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

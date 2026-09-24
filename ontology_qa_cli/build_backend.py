"""Pluggable build-job backends for the web chatbot.

The web app kicks off a (long, costly) ontology build and then polls its status
and streams its log. *How* that build runs depends on the deployment:

* **subprocess** (default): run ``ontology-qa-build`` as a local child process,
  tailing its on-disk log file. This is the original single-process behavior —
  right for local dev and a single-container deploy.
* **ecs**: launch the builder as a separate Fargate task via ``ecs.run_task``
  (the "split" architecture, see docs/deployment-cdk.md). Status comes from
  ``ecs.describe_tasks`` and the log is tailed from CloudWatch Logs. The builder
  task shares the web service's EFS mount, so the built ``query_program.py`` is
  visible to the web service exactly as with the subprocess backend.

Both backends expose the same small surface (:class:`BuildBackend`) so the web
app is agnostic to which one is in use. Select with the ``BUILD_BACKEND``
environment variable (``subprocess`` | ``ecs``); ``create_app`` wires it.

A running build is represented by an opaque :class:`BuildJob` whose fields are
owned by the backend that created it. The web app only ever calls
``poll`` / ``log_tail`` / ``stream_lines`` on the backend, never the internals.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable, Optional, Protocol


class BuildCapacityError(RuntimeError):
    """Raised when a build is refused because the configured maximum number of
    concurrent builder tasks is already running. Distinct from a launch failure
    so the web app can surface a "try again shortly" 429 rather than a 500.
    """


# --------------------------------------------------------------------------- #
# Job handle
# --------------------------------------------------------------------------- #
@dataclass
class BuildJob:
    """Opaque handle to a running build. Backend-specific fields are optional
    and only set by the backend that owns the job.
    """
    job_id: str
    ontology: str
    status: str = "running"  # running | finished | failed
    lock: threading.Lock = field(default_factory=threading.Lock)

    # subprocess backend
    proc: Optional[subprocess.Popen] = None
    log_path: Optional[Path] = None

    # ecs backend
    task_arn: Optional[str] = None


class BuildBackend(Protocol):
    """Strategy for launching, polling and streaming an ontology build."""

    def start(self, ontology: str) -> BuildJob:
        """Launch a build for ``ontology`` and return its job handle."""
        ...

    def poll(self, job: BuildJob) -> str:
        """Return the terminal-or-running status: running | finished | failed."""
        ...

    def log_tail(self, job: BuildJob, max_lines: int = 40) -> str:
        """Return the last ``max_lines`` of the build's log (best effort)."""
        ...

    async def stream_lines(self, job: BuildJob) -> AsyncIterator[str]:
        """Yield log lines as they appear, ending when the job is terminal.

        The web app wraps each yielded line in an SSE ``data:`` frame and emits
        its own terminal ``event: done`` frame after this iterator is exhausted,
        so implementations only need to yield raw log lines.
        """
        ...


# --------------------------------------------------------------------------- #
# Subprocess backend (default; original behavior)
# --------------------------------------------------------------------------- #
class SubprocessBuildBackend:
    """Run the builder as a local child process, tailing its on-disk log.

    Behavior-identical to the original inline implementation in webapp.py, so
    existing tests (which monkeypatch ``subprocess.Popen``) keep working.
    """

    def __init__(self, config_path: Path, runs_dir: Path) -> None:
        self._config_path = Path(config_path)
        self._runs_dir = Path(runs_dir)

    def start(self, ontology: str) -> BuildJob:
        job_id = uuid.uuid4().hex[:12]
        log_path = self._runs_dir / f"_web_build_{ontology}_{job_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 - handed to Popen
        child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        proc = subprocess.Popen(
            [
                sys.executable, "-u", "-m", "ontology_qa_cli.builder",
                ontology, "--plain",
                "--ontology-config", str(self._config_path),
                "--output-dir", str(self._runs_dir),
            ],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=child_env,
        )
        return BuildJob(job_id=job_id, ontology=ontology, proc=proc, log_path=log_path)

    def poll(self, job: BuildJob) -> str:
        with job.lock:
            if job.status != "running":
                return job.status
            assert job.proc is not None
            code = job.proc.poll()
            if code is None:
                return "running"
            job.status = "finished" if code == 0 else "failed"
            return job.status

    def log_tail(self, job: BuildJob, max_lines: int = 40) -> str:
        if job.log_path is None:
            return ""
        try:
            text = job.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-max_lines:])

    async def stream_lines(self, job: BuildJob) -> AsyncIterator[str]:
        assert job.log_path is not None
        pos = 0
        while True:
            try:
                with open(job.log_path, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
            except OSError:
                chunk = ""
            for line in chunk.splitlines():
                yield line
            if self.poll(job) != "running":
                # Drain any final bytes written after the last read.
                try:
                    with open(job.log_path, "r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(pos)
                        tail = fh.read()
                    for line in tail.splitlines():
                        yield line
                except OSError:
                    pass
                return
            await asyncio.sleep(0.5)


# --------------------------------------------------------------------------- #
# ECS RunTask backend (split architecture)
# --------------------------------------------------------------------------- #
class EcsBuildBackend:
    """Launch the builder as an on-demand Fargate task.

    Status comes from ``ecs.describe_tasks``; the log is tailed from CloudWatch
    Logs (simple ``get_log_events`` polling — no extra IAM beyond
    ``logs:GetLogEvents``, works in every region, unlike StartLiveTail).

    Configured entirely from the environment so the web app stays declarative:

        BUILD_ECS_CLUSTER            ECS cluster name/ARN
        BUILD_ECS_TASK_DEFINITION    builder task definition family:revision or ARN
        BUILD_ECS_CONTAINER_NAME     container to override (default "builder")
        BUILD_ECS_SUBNETS            comma-separated private subnet ids
        BUILD_ECS_SECURITY_GROUPS    comma-separated security group ids
        BUILD_ECS_LOG_GROUP          CloudWatch log group the task logs to
        BUILD_ECS_LOG_STREAM_PREFIX  awslogs stream prefix (default "builder")
        ONTOLOGY_CONFIG              config path passed to the builder (default ontology_config.yaml)
        ONTOLOGY_RUNS_DIR            output dir on the shared EFS mount (default /mnt/runs)
        BUILD_ECS_MAX_CONCURRENT     max in-flight builder tasks; 0 = unlimited (default)
    """

    def __init__(self, *, boto_session: object = None) -> None:
        self._session = boto_session
        self._cluster = os.environ["BUILD_ECS_CLUSTER"]
        self._task_def = os.environ["BUILD_ECS_TASK_DEFINITION"]
        self._container = os.environ.get("BUILD_ECS_CONTAINER_NAME", "builder")
        self._subnets = _csv_env("BUILD_ECS_SUBNETS")
        self._security_groups = _csv_env("BUILD_ECS_SECURITY_GROUPS")
        self._log_group = os.environ.get("BUILD_ECS_LOG_GROUP", "")
        self._log_prefix = os.environ.get("BUILD_ECS_LOG_STREAM_PREFIX", "builder")
        self._config_path = os.environ.get("ONTOLOGY_CONFIG", "ontology_config.yaml")
        self._runs_dir = os.environ.get("ONTOLOGY_RUNS_DIR", "/mnt/runs")
        # Concurrency cap on in-flight builder tasks. 0 (default) = unlimited,
        # preserving the prior behavior. Builds are long and Bedrock-costly, so
        # a small positive cap protects both cost and Fargate task limits.
        self._max_concurrent = _int_env("BUILD_ECS_MAX_CONCURRENT", 0)

    # -- lazily-created AWS clients (so import/tests don't need boto3/creds) --
    def _client(self, service: str):
        if self._session is not None:
            return self._session.client(service)  # type: ignore[attr-defined]
        import boto3
        return boto3.client(service)

    def start(self, ontology: str) -> BuildJob:
        job_id = uuid.uuid4().hex[:12]
        ecs = self._client("ecs")
        # Enforce the concurrency cap before launching. A cap of 0 disables the
        # check (unlimited). We count RUNNING + PENDING builder tasks in the
        # cluster; the small race window (two callers passing the check at once)
        # is acceptable for a cost/limit guardrail, not a hard quota.
        if self._max_concurrent > 0:
            running = self._count_active_builder_tasks(ecs)
            if running >= self._max_concurrent:
                raise BuildCapacityError(
                    f"build refused: {running} builder task(s) already running "
                    f"(limit {self._max_concurrent}); try again shortly"
                )
        resp = ecs.run_task(
            cluster=self._cluster,
            taskDefinition=self._task_def,
            launchType="FARGATE",
            count=1,
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": self._subnets,
                    "securityGroups": self._security_groups,
                    "assignPublicIp": "DISABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": self._container,
                        "command": [
                            "ontology_qa_cli.builder",
                            ontology,
                            "--plain",
                            "--ontology-config", self._config_path,
                            "--output-dir", self._runs_dir,
                        ],
                    }
                ]
            },
        )
        failures = resp.get("failures") or []
        if failures or not resp.get("tasks"):
            raise RuntimeError(f"ecs.run_task failed to launch builder: {failures}")
        task_arn = resp["tasks"][0]["taskArn"]
        return BuildJob(job_id=job_id, ontology=ontology, task_arn=task_arn)

    def _task_family(self) -> str:
        """The task-def family used to filter list_tasks. Accepts either a
        ``family:revision`` string or a full task-definition ARN
        (``.../builder:3``); strip the ``:revision`` suffix in both cases.
        """
        tail = self._task_def.rsplit("/", 1)[-1]  # ARN -> "builder:3"; plain -> unchanged
        return tail.split(":", 1)[0]

    def _count_active_builder_tasks(self, ecs) -> int:
        """Count builder tasks in the cluster that are RUNNING or PENDING."""
        family = self._task_family()
        total = 0
        for status in ("RUNNING", "PENDING"):
            token = None
            while True:
                kwargs = {"cluster": self._cluster, "family": family, "desiredStatus": status}
                if token:
                    kwargs["nextToken"] = token
                resp = ecs.list_tasks(**kwargs)
                total += len(resp.get("taskArns") or [])
                token = resp.get("nextToken")
                if not token:
                    break
        return total

    def poll(self, job: BuildJob) -> str:
        with job.lock:
            if job.status != "running":
                return job.status
            assert job.task_arn is not None
            ecs = self._client("ecs")
            desc = ecs.describe_tasks(cluster=self._cluster, tasks=[job.task_arn])
            tasks = desc.get("tasks") or []
            if not tasks:
                return "running"  # not visible yet; keep polling
            task = tasks[0]
            if task.get("lastStatus") != "STOPPED":
                return "running"
            # Terminal: derive success from the container exit code.
            containers = task.get("containers") or []
            code = next(
                (c.get("exitCode") for c in containers if c.get("name") == self._container),
                None,
            )
            job.status = "finished" if code == 0 else "failed"
            return job.status

    def _log_stream_name(self, job: BuildJob) -> str:
        # awslogs stream: "<prefix>/<container>/<task-id>"
        task_id = (job.task_arn or "").rsplit("/", 1)[-1]
        return f"{self._log_prefix}/{self._container}/{task_id}"

    def log_tail(self, job: BuildJob, max_lines: int = 40) -> str:
        if not self._log_group:
            return ""
        try:
            logs = self._client("logs")
            resp = logs.get_log_events(
                logGroupName=self._log_group,
                logStreamName=self._log_stream_name(job),
                limit=max_lines,
                startFromHead=False,
            )
            return "\n".join(e["message"] for e in resp.get("events", []))
        except Exception:  # noqa: BLE001 - log tail is best-effort
            return ""

    async def stream_lines(self, job: BuildJob) -> AsyncIterator[str]:
        if not self._log_group:
            # No log group configured: still wait for terminal status.
            while self.poll(job) == "running":
                await asyncio.sleep(1.0)
            return
        logs = self._client("logs")
        next_token: Optional[str] = None
        stream = self._log_stream_name(job)
        while True:
            try:
                kwargs = {
                    "logGroupName": self._log_group,
                    "logStreamName": stream,
                    "startFromHead": True,
                }
                if next_token:
                    kwargs["nextToken"] = next_token
                resp = logs.get_log_events(**kwargs)
                for event in resp.get("events", []):
                    yield event["message"]
                token = resp.get("nextForwardToken")
                # get_log_events returns the same token at the end of the stream.
                if token == next_token:
                    if self.poll(job) != "running":
                        return
                    await asyncio.sleep(1.0)
                next_token = token
            except Exception as exc:  # noqa: BLE001 - surface then stop on terminal
                if self.poll(job) != "running":
                    yield f"[log stream error] {exc}"
                    return
                await asyncio.sleep(1.0)


def _csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _int_env(name: str, default: int) -> int:
    """Parse an int env var; fall back to ``default`` when unset or malformed."""
    raw = os.environ.get(name, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def make_build_backend(config_path: Path, runs_dir: Path) -> BuildBackend:
    """Return the backend named by ``BUILD_BACKEND`` (default ``subprocess``)."""
    kind = os.environ.get("BUILD_BACKEND", "subprocess").strip().lower()
    if kind == "ecs":
        return EcsBuildBackend()
    if kind == "subprocess":
        return SubprocessBuildBackend(config_path, runs_dir)
    raise ValueError(
        f"Unknown BUILD_BACKEND={kind!r}; expected 'subprocess' or 'ecs'."
    )

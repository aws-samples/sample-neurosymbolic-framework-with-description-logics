"""Tests for the pluggable build backends (ontology_qa_cli.build_backend).

The subprocess backend is exercised end-to-end via test_webapp.py; here we
unit-test the ECS backend against a fake boto3 session (no AWS, no creds) and
the BUILD_BACKEND selection logic.
"""
from __future__ import annotations

import pytest

from ontology_qa_cli import build_backend as bb


# --------------------------------------------------------------------------- #
# Fake boto3 session/clients
# --------------------------------------------------------------------------- #
class _FakeEcs:
    def __init__(self, *, task_arn="arn:aws:ecs:us-east-1:1:task/c/abc123", run_failures=None,
                 active_running=0, active_pending=0):
        self.task_arn = task_arn
        self.run_failures = run_failures or []
        self.run_task_calls = []
        self.list_tasks_calls = []
        self._active_running = active_running
        self._active_pending = active_pending
        self._last_status = "RUNNING"
        self._exit_code = None

    def set_stopped(self, exit_code):
        self._last_status = "STOPPED"
        self._exit_code = exit_code

    def list_tasks(self, **kwargs):
        self.list_tasks_calls.append(kwargs)
        n = self._active_running if kwargs.get("desiredStatus") == "RUNNING" else self._active_pending
        return {"taskArns": [f"arn/task-{i}" for i in range(n)]}

    def run_task(self, **kwargs):
        self.run_task_calls.append(kwargs)
        if self.run_failures:
            return {"tasks": [], "failures": self.run_failures}
        return {"tasks": [{"taskArn": self.task_arn}], "failures": []}

    def describe_tasks(self, cluster, tasks):
        return {
            "tasks": [
                {
                    "lastStatus": self._last_status,
                    "containers": [{"name": "builder", "exitCode": self._exit_code}],
                }
            ]
        }


class _FakeLogs:
    def __init__(self, events):
        # events: list of message strings
        self._events = events

    def get_log_events(self, **kwargs):
        return {
            "events": [{"message": m} for m in self._events],
            "nextForwardToken": "f/END",
        }


class _FakeSession:
    def __init__(self, ecs=None, logs=None):
        self._ecs = ecs
        self._logs = logs

    def client(self, service):
        if service == "ecs":
            return self._ecs
        if service == "logs":
            return self._logs
        raise ValueError(service)


@pytest.fixture
def ecs_env(monkeypatch):
    monkeypatch.setenv("BUILD_ECS_CLUSTER", "test-cluster")
    monkeypatch.setenv("BUILD_ECS_TASK_DEFINITION", "builder:1")
    monkeypatch.setenv("BUILD_ECS_SUBNETS", "subnet-a,subnet-b")
    monkeypatch.setenv("BUILD_ECS_SECURITY_GROUPS", "sg-1")
    monkeypatch.setenv("BUILD_ECS_LOG_GROUP", "/ecs/builder")
    monkeypatch.setenv("ONTOLOGY_RUNS_DIR", "/mnt/runs")


def _backend(ecs=None, logs=None):
    return bb.EcsBuildBackend(boto_session=_FakeSession(ecs=ecs, logs=logs))


class TestEcsBackend:
    def test_start_runs_task_with_overrides(self, ecs_env):
        ecs = _FakeEcs()
        job = _backend(ecs).start("FMA")
        assert job.task_arn == ecs.task_arn
        assert job.ontology == "FMA"
        call = ecs.run_task_calls[0]
        assert call["cluster"] == "test-cluster"
        assert call["taskDefinition"] == "builder:1"
        assert call["launchType"] == "FARGATE"
        net = call["networkConfiguration"]["awsvpcConfiguration"]
        assert net["subnets"] == ["subnet-a", "subnet-b"]
        assert net["securityGroups"] == ["sg-1"]
        # The ontology name + build args are passed as a container command override.
        cmd = call["overrides"]["containerOverrides"][0]["command"]
        assert cmd[:3] == ["ontology_qa_cli.builder", "FMA", "--plain"]
        assert "--output-dir" in cmd and "/mnt/runs" in cmd

    def test_start_raises_on_run_task_failure(self, ecs_env):
        ecs = _FakeEcs(run_failures=[{"reason": "CAPACITY"}])
        with pytest.raises(RuntimeError):
            _backend(ecs).start("FMA")

    def test_no_concurrency_check_when_cap_unset(self, ecs_env):
        # Default (BUILD_ECS_MAX_CONCURRENT unset -> 0) means unlimited: no
        # list_tasks call, task launches regardless of how many are running.
        ecs = _FakeEcs(active_running=99)
        _backend(ecs).start("FMA")
        assert ecs.list_tasks_calls == []
        assert len(ecs.run_task_calls) == 1

    def test_start_under_cap_launches(self, ecs_env, monkeypatch):
        monkeypatch.setenv("BUILD_ECS_MAX_CONCURRENT", "3")
        ecs = _FakeEcs(active_running=2)  # 2 < 3
        _backend(ecs).start("FMA")
        assert len(ecs.run_task_calls) == 1
        # list_tasks filtered by the task-def family, not the raw "builder:1".
        assert ecs.list_tasks_calls[0]["family"] == "builder"

    def test_start_at_cap_raises_capacity_error(self, ecs_env, monkeypatch):
        monkeypatch.setenv("BUILD_ECS_MAX_CONCURRENT", "2")
        ecs = _FakeEcs(active_running=1, active_pending=1)  # 1+1 == 2
        with pytest.raises(bb.BuildCapacityError):
            _backend(ecs).start("FMA")
        assert ecs.run_task_calls == []  # never launched

    def test_capacity_error_is_runtimeerror_subclass(self):
        # The web app catches BuildCapacityError specifically (429); make sure it
        # stays distinguishable from the generic launch-failure RuntimeError.
        assert issubclass(bb.BuildCapacityError, RuntimeError)

    def test_poll_running_then_finished(self, ecs_env):
        ecs = _FakeEcs()
        b = _backend(ecs)
        job = b.start("FMA")
        assert b.poll(job) == "running"
        ecs.set_stopped(exit_code=0)
        assert b.poll(job) == "finished"

    def test_poll_failed_on_nonzero_exit(self, ecs_env):
        ecs = _FakeEcs()
        b = _backend(ecs)
        job = b.start("FMA")
        ecs.set_stopped(exit_code=1)
        assert b.poll(job) == "failed"

    def test_poll_is_sticky_once_terminal(self, ecs_env):
        ecs = _FakeEcs()
        b = _backend(ecs)
        job = b.start("FMA")
        ecs.set_stopped(exit_code=0)
        assert b.poll(job) == "finished"
        # Even if ECS later reports something else, the cached terminal wins.
        ecs._last_status = "RUNNING"
        assert b.poll(job) == "finished"

    def test_log_tail_reads_cloudwatch(self, ecs_env):
        ecs = _FakeEcs()
        logs = _FakeLogs(["line one", "line two"])
        b = _backend(ecs, logs)
        job = b.start("FMA")
        assert b.log_tail(job) == "line one\nline two"

    def test_log_stream_name_uses_task_id(self, ecs_env):
        ecs = _FakeEcs(task_arn="arn:aws:ecs:us-east-1:1:task/cluster/deadbeef")
        b = _backend(ecs)
        job = b.start("FMA")
        assert b._log_stream_name(job) == "builder/builder/deadbeef"


class TestBackendSelection:
    def test_default_is_subprocess(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BUILD_BACKEND", raising=False)
        backend = bb.make_build_backend(tmp_path / "cfg.yaml", tmp_path / "runs")
        assert isinstance(backend, bb.SubprocessBuildBackend)

    def test_ecs_selected_by_env(self, tmp_path, monkeypatch, ecs_env):
        monkeypatch.setenv("BUILD_BACKEND", "ecs")
        backend = bb.make_build_backend(tmp_path / "cfg.yaml", tmp_path / "runs")
        assert isinstance(backend, bb.EcsBuildBackend)

    def test_unknown_backend_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUILD_BACKEND", "nope")
        with pytest.raises(ValueError):
            bb.make_build_backend(tmp_path / "cfg.yaml", tmp_path / "runs")


# --------------------------------------------------------------------------- #
# Job store (shared registry across web tasks)
# --------------------------------------------------------------------------- #
def _always_running(job):
    return "running"


def _always_finished(job):
    return "finished"


class TestBuildJobRecord:
    def test_round_trip_preserves_shared_fields(self):
        job = bb.BuildJob(job_id="abc123", ontology="GO", status="running",
                          task_arn="arn:aws:ecs:us-east-1:1:task/c/t1")
        rebuilt = bb.BuildJob.from_record(job.to_record())
        assert rebuilt.job_id == "abc123"
        assert rebuilt.ontology == "GO"
        assert rebuilt.status == "running"
        assert rebuilt.task_arn == "arn:aws:ecs:us-east-1:1:task/c/t1"

    def test_record_omits_process_local_fields(self):
        # proc/log_path are process-local and must not appear in the record.
        job = bb.BuildJob(job_id="x", ontology="GO")
        rec = job.to_record()
        assert set(rec) == {"job_id", "ontology", "status", "task_arn"}


class TestMemoryJobStore:
    def test_put_get(self):
        store = bb.MemoryJobStore()
        job = bb.BuildJob(job_id="j1", ontology="GO", task_arn="arn/1")
        store.put(job)
        assert store.get("j1") is job
        assert store.get("missing") is None

    def test_running_for_matches_only_running(self):
        store = bb.MemoryJobStore()
        store.put(bb.BuildJob(job_id="j1", ontology="GO", task_arn="arn/1"))
        assert store.running_for("GO", _always_running).job_id == "j1"
        assert store.running_for("GO", _always_finished) is None
        assert store.running_for("FMA", _always_running) is None


class TestFileJobStore:
    def test_shares_jobs_across_instances(self, tmp_path):
        # The whole point: a job recorded by one web task's store instance is
        # visible to a DIFFERENT instance pointed at the same (EFS) directory.
        shared = tmp_path / ".jobs"
        store_a = bb.FileJobStore(shared)
        store_b = bb.FileJobStore(shared)
        job = bb.BuildJob(job_id="deadbeef", ontology="GO", task_arn="arn/t")
        store_a.put(job)
        # store_b never saw the put() in memory — it reads it from disk.
        rebuilt = store_b.get("deadbeef")
        assert rebuilt is not None
        assert rebuilt.ontology == "GO"
        assert rebuilt.task_arn == "arn/t"

    def test_running_for_finds_shared_running_job(self, tmp_path):
        shared = tmp_path / ".jobs"
        bb.FileJobStore(shared).put(
            bb.BuildJob(job_id="j1", ontology="GO", task_arn="arn/1")
        )
        other = bb.FileJobStore(shared)
        assert other.running_for("GO", _always_running).job_id == "j1"
        assert other.running_for("GO", _always_finished) is None

    def test_running_for_skips_recorded_terminal_without_polling(self, tmp_path):
        shared = tmp_path / ".jobs"
        bb.FileJobStore(shared).put(
            bb.BuildJob(job_id="done1", ontology="GO", status="finished", task_arn="arn/1")
        )

        def _boom(job):  # must not be called for an already-terminal record
            raise AssertionError("poll should not be called for a terminal record")

        assert bb.FileJobStore(shared).running_for("GO", _boom) is None

    def test_get_unknown_returns_none(self, tmp_path):
        assert bb.FileJobStore(tmp_path / ".jobs").get("nope") is None


class TestJobStoreSelection:
    def test_default_is_memory(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BUILD_BACKEND", raising=False)
        assert isinstance(bb.make_job_store(tmp_path / "runs"), bb.MemoryJobStore)

    def test_ecs_selects_file_store_under_runs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUILD_BACKEND", "ecs")
        store = bb.make_job_store(tmp_path / "runs")
        assert isinstance(store, bb.FileJobStore)
        assert store._dir == (tmp_path / "runs" / ".jobs")

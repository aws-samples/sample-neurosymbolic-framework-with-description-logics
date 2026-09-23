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
    def __init__(self, *, task_arn="arn:aws:ecs:us-east-1:1:task/c/abc123", run_failures=None):
        self.task_arn = task_arn
        self.run_failures = run_failures or []
        self.run_task_calls = []
        self._last_status = "RUNNING"
        self._exit_code = None

    def set_stopped(self, exit_code):
        self._last_status = "STOPPED"
        self._exit_code = exit_code

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

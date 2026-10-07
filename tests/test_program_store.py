"""Tests for the durable S3 program store (program_store.py).

Mirrors tests/test_s3_cache.py: boto3 is stubbed with a MagicMock via
``mock.patch("boto3.client", ...)``; no moto, no network.
"""
from pathlib import Path
from unittest import mock

import pytest

from ontology_qa_cli import program_store


# --- gating ---------------------------------------------------------------


def test_disabled_when_env_unset(monkeypatch):
    monkeypatch.delenv("PROGRAM_STORE_S3", raising=False)
    assert program_store.is_enabled() is False
    # Every public entry point is a no-op with no boto3 call.
    with mock.patch("boto3.client") as client:
        assert program_store.upload_run(Path("/nope"), "FMA") is False
        assert program_store.hydrate_ontology("FMA", Path("/nope")) is None
        client.assert_not_called()


def test_enabled_when_env_set(monkeypatch):
    monkeypatch.setenv("PROGRAM_STORE_S3", "s3://bucket/programs")
    assert program_store.is_enabled() is True


@pytest.mark.parametrize("bad", ["not-a-uri", "s3://", "https://x/y"])
def test_malformed_uri_disables(monkeypatch, bad, tmp_path):
    monkeypatch.setenv("PROGRAM_STORE_S3", bad)
    # Malformed value -> treated as disabled (no crash, no boto3 call).
    with mock.patch("boto3.client") as client:
        assert program_store.hydrate_ontology("FMA", tmp_path) is None
        client.assert_not_called()


def test_default_prefix_when_omitted(monkeypatch):
    monkeypatch.setenv("PROGRAM_STORE_S3", "s3://just-a-bucket")
    assert program_store._store_location() == ("just-a-bucket", "programs")


def test_custom_prefix(monkeypatch):
    monkeypatch.setenv("PROGRAM_STORE_S3", "s3://b/nested/progs")
    assert program_store._store_location() == ("b", "nested/progs")


# --- upload ---------------------------------------------------------------


def _make_run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "FMA_20260101_120000"
    (run / "iteration_0").mkdir(parents=True)
    (run / "iteration_1").mkdir(parents=True)
    (run / "summary.json").write_text('{"best_iteration": 1}')
    (run / "iteration_0" / "query_program.py").write_text("# v0\n")
    (run / "iteration_1" / "query_program.py").write_text("# v1\n")
    (run / "iteration_1" / "evaluation.json").write_text("{}")
    return run


def test_upload_run_puts_every_file_under_ontology_timestamp(monkeypatch, tmp_path):
    monkeypatch.setenv("PROGRAM_STORE_S3", "s3://bucket/programs")
    run = _make_run_dir(tmp_path)

    fake = mock.MagicMock()
    with mock.patch("boto3.client", return_value=fake):
        assert program_store.upload_run(run, "FMA", quiet=True) is True

    # One upload_file call per file, keyed by programs/<ontology>/<run>/<rel>.
    keys = {call.args[2] for call in fake.upload_file.call_args_list}
    assert keys == {
        "programs/FMA/FMA_20260101_120000/summary.json",
        "programs/FMA/FMA_20260101_120000/iteration_0/query_program.py",
        "programs/FMA/FMA_20260101_120000/iteration_1/query_program.py",
        "programs/FMA/FMA_20260101_120000/iteration_1/evaluation.json",
    }
    # All to the configured bucket.
    assert {call.args[1] for call in fake.upload_file.call_args_list} == {"bucket"}


def test_upload_run_missing_dir_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("PROGRAM_STORE_S3", "s3://bucket/programs")
    with mock.patch("boto3.client") as client:
        assert program_store.upload_run(tmp_path / "does_not_exist", "FMA", quiet=True) is False
        client.assert_not_called()


def test_upload_run_failure_is_swallowed(monkeypatch, tmp_path):
    monkeypatch.setenv("PROGRAM_STORE_S3", "s3://bucket/programs")
    run = _make_run_dir(tmp_path)
    fake = mock.MagicMock()
    fake.upload_file.side_effect = OSError("disk gone")
    with mock.patch("boto3.client", return_value=fake):
        # Best-effort: returns False rather than raising.
        assert program_store.upload_run(run, "FMA", quiet=True) is False


# --- hydrate --------------------------------------------------------------


def _paginator_for(pages):
    """Build a fake boto3 paginator whose .paginate() yields the given pages."""
    paginator = mock.MagicMock()
    paginator.paginate.return_value = iter(pages)
    return paginator


def test_hydrate_local_hit_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("PROGRAM_STORE_S3", "s3://bucket/programs")
    runs = tmp_path / "runs"
    (runs / "FMA_20250101_000000" / "iteration_0").mkdir(parents=True)
    # A local run already exists -> no S3 call at all (local always wins).
    with mock.patch("boto3.client") as client:
        assert program_store.hydrate_ontology("FMA", runs) is None
        client.assert_not_called()


def test_hydrate_downloads_latest_remote_run(monkeypatch, tmp_path):
    monkeypatch.setenv("PROGRAM_STORE_S3", "s3://bucket/programs")
    runs = tmp_path / "runs"
    runs.mkdir()

    fake = mock.MagicMock()

    # First paginator use: list_objects_v2 with Delimiter -> CommonPrefixes
    # (two runs; newest-wins picks the lexicographically-greatest name).
    list_pages = [
        {
            "CommonPrefixes": [
                {"Prefix": "programs/FMA/FMA_20250101_000000/"},
                {"Prefix": "programs/FMA/FMA_20260301_090000/"},
            ]
        }
    ]
    # Second paginator use: list the chosen run's objects.
    obj_pages = [
        {
            "Contents": [
                {"Key": "programs/FMA/FMA_20260301_090000/summary.json"},
                {"Key": "programs/FMA/FMA_20260301_090000/iteration_1/query_program.py"},
            ]
        }
    ]
    fake.get_paginator.side_effect = [
        _paginator_for(list_pages),
        _paginator_for(obj_pages),
    ]

    def fake_download(bucket, key, dest):
        Path(dest).write_text(f"content of {key}")

    fake.download_file.side_effect = fake_download

    with mock.patch("boto3.client", return_value=fake):
        dest = program_store.hydrate_ontology("FMA", runs, quiet=True)

    # Pulled the NEWEST run into runs/ under its original name.
    assert dest == runs / "FMA_20260301_090000"
    assert (dest / "summary.json").read_text() == (
        "content of programs/FMA/FMA_20260301_090000/summary.json"
    )
    assert (dest / "iteration_1" / "query_program.py").exists()
    # No leftover .part temp files.
    assert not list(dest.rglob("*.part"))


def test_hydrate_no_remote_run_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("PROGRAM_STORE_S3", "s3://bucket/programs")
    runs = tmp_path / "runs"
    runs.mkdir()
    fake = mock.MagicMock()
    fake.get_paginator.return_value = _paginator_for([{}])  # no CommonPrefixes
    with mock.patch("boto3.client", return_value=fake):
        assert program_store.hydrate_ontology("FMA", runs, quiet=True) is None
    fake.download_file.assert_not_called()


def test_hydrate_download_failure_is_swallowed(monkeypatch, tmp_path):
    monkeypatch.setenv("PROGRAM_STORE_S3", "s3://bucket/programs")
    runs = tmp_path / "runs"
    runs.mkdir()
    fake = mock.MagicMock()
    list_pages = [{"CommonPrefixes": [{"Prefix": "programs/FMA/FMA_20260301_090000/"}]}]
    obj_pages = [{"Contents": [{"Key": "programs/FMA/FMA_20260301_090000/summary.json"}]}]
    fake.get_paginator.side_effect = [
        _paginator_for(list_pages),
        _paginator_for(obj_pages),
    ]
    fake.download_file.side_effect = OSError("network gone")
    with mock.patch("boto3.client", return_value=fake):
        assert program_store.hydrate_ontology("FMA", runs, quiet=True) is None

"""Tests for S3-sourced ontology loading (s3_cache + config integration)."""
import os
from pathlib import Path
from unittest import mock

import pytest

from ontology_qa_cli import s3_cache
from ontology_qa_cli.config import load_config, resolve_ontology


# --- s3_cache unit tests ---------------------------------------------------


def test_is_s3_uri():
    assert s3_cache.is_s3_uri("s3://bucket/key.owl")
    assert s3_cache.is_s3_uri("S3://Bucket/Key.owl")
    assert not s3_cache.is_s3_uri("fma.owl")
    assert not s3_cache.is_s3_uri("/abs/path/fma.owl")
    assert not s3_cache.is_s3_uri("~/fma.owl")


def test_parse_s3_uri():
    assert s3_cache.parse_s3_uri("s3://my-bucket/path/to/fma.owl") == (
        "my-bucket",
        "path/to/fma.owl",
    )


@pytest.mark.parametrize("bad", ["s3://bucket", "s3:///key.owl", "s3://"])
def test_parse_s3_uri_invalid(bad):
    with pytest.raises(ValueError):
        s3_cache.parse_s3_uri(bad)


def test_cache_root_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ONTOLOGY_QA_CACHE_DIR", str(tmp_path / "c"))
    assert s3_cache.cache_root() == tmp_path / "c"


def test_local_cache_path_deterministic_and_scoped(monkeypatch, tmp_path):
    monkeypatch.setenv("ONTOLOGY_QA_CACHE_DIR", str(tmp_path))
    uri = "s3://bucket/nested/fma.owl"
    p1 = s3_cache.local_cache_path(uri)
    p2 = s3_cache.local_cache_path(uri)
    assert p1 == p2
    assert p1.name == "fma.owl"
    assert tmp_path in p1.parents
    # Different URIs get different cache dirs.
    assert s3_cache.local_cache_path("s3://bucket/other.owl") != p1


def test_ensure_local_copy_downloads_then_reuses(monkeypatch, tmp_path):
    monkeypatch.setenv("ONTOLOGY_QA_CACHE_DIR", str(tmp_path))
    uri = "s3://bucket/fma.owl"
    payload = b"<owl>content</owl>"

    def fake_download(bucket, key, dest):
        Path(dest).write_bytes(payload)

    fake_client = mock.MagicMock()
    fake_client.head_object.return_value = {"ContentLength": len(payload)}
    fake_client.download_file.side_effect = fake_download

    with mock.patch("boto3.client", return_value=fake_client):
        dest1 = s3_cache.ensure_local_copy(uri, quiet=True)
        assert dest1.read_bytes() == payload
        assert fake_client.download_file.call_count == 1

        # Second call: size matches -> cache hit, no re-download.
        dest2 = s3_cache.ensure_local_copy(uri, quiet=True)
        assert dest2 == dest1
        assert fake_client.download_file.call_count == 1

        # Size mismatch -> re-download.
        fake_client.head_object.return_value = {"ContentLength": 999}
        s3_cache.ensure_local_copy(uri, quiet=True)
        assert fake_client.download_file.call_count == 2


def test_ensure_local_copy_head_error_exits(monkeypatch, tmp_path):
    monkeypatch.setenv("ONTOLOGY_QA_CACHE_DIR", str(tmp_path))
    from botocore.exceptions import ClientError

    fake_client = mock.MagicMock()
    fake_client.head_object.side_effect = ClientError(
        {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
    )
    with mock.patch("boto3.client", return_value=fake_client):
        with pytest.raises(SystemExit):
            s3_cache.ensure_local_copy("s3://bucket/fma.owl", quiet=True)


# --- config integration ----------------------------------------------------


def _write_s3_config(tmp_path: Path, uri: str) -> Path:
    cfg = tmp_path / "ontology_config.yaml"
    cfg.write_text(
        f"""\
ontologies:
  - name: "FMA"
    owl_file: "{uri}"
    questions:
      - question: "q?"
        expected_answer: "a."
"""
    )
    return cfg


def test_load_config_s3_is_lazy(monkeypatch, tmp_path):
    """Parsing an S3 config must NOT hit the network; owl_file is the cache path."""
    monkeypatch.setenv("ONTOLOGY_QA_CACHE_DIR", str(tmp_path / "cache"))
    cfg = _write_s3_config(tmp_path, "s3://bucket/fma.owl")

    with mock.patch("boto3.client") as client:
        config = load_config(cfg)
        client.assert_not_called()  # no network during parse

    entry = config.ontologies[0]
    assert entry.s3_uri == "s3://bucket/fma.owl"
    assert entry.owl_file == s3_cache.local_cache_path("s3://bucket/fma.owl")


def test_resolve_ontology_materializes_s3(monkeypatch, tmp_path):
    monkeypatch.setenv("ONTOLOGY_QA_CACHE_DIR", str(tmp_path / "cache"))
    cfg = _write_s3_config(tmp_path, "s3://bucket/fma.owl")
    config = load_config(cfg)

    with mock.patch(
        "ontology_qa_cli.s3_cache.ensure_local_copy",
        return_value=Path("/downloaded/fma.owl"),
    ) as ensure:
        entry = resolve_ontology(config, "fma")
        ensure.assert_called_once_with("s3://bucket/fma.owl")
        assert entry.owl_file == Path("/downloaded/fma.owl")


def test_local_owl_file_still_works(sample_config_path):
    """Non-S3 entries are unaffected."""
    config = load_config(sample_config_path)
    entry = resolve_ontology(config, "TestOntology")
    assert entry.s3_uri is None
    assert str(entry.owl_file).endswith("data/test.owl")

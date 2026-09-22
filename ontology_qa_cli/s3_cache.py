"""Resolve ``s3://`` ontology URIs to a local cached file.

The rest of the pipeline (builder, serve, query, executor) needs a real local
filesystem path: it does existence checks, builds ``file://`` URIs, passes the
path to worker subprocesses, and derives the owlready2 SQLite cache path via
``owl_path.with_suffix('.sqlite3')``. To keep all of that untouched while still
letting the source OWL live in S3, an ``s3://bucket/key`` value is materialized
to a local cache file on first use and the local path is handed downstream.

Cache layout::

    <cache_root>/<sha1(s3-uri)[:16]>/<basename>

``cache_root`` defaults to ``~/.cache/ontology-qa`` (override with the
``ONTOLOGY_QA_CACHE_DIR`` environment variable). The cache lives OUTSIDE the
repo so the whole point of the feature -- not filling local disk in the project
tree, and being able to wipe the cache freely -- holds. A cached copy is reused
across runs when its size matches the S3 object's ``ContentLength``; otherwise
it is re-downloaded.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from urllib.parse import urlparse


def is_s3_uri(value: str) -> bool:
    """True if ``value`` is an ``s3://bucket/key`` URI."""
    return isinstance(value, str) and value.lower().startswith("s3://")


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split ``s3://bucket/key`` into ``(bucket, key)``.

    Raises:
        ValueError: If the URI has no bucket or no key.
    """
    parsed = urlparse(uri)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not bucket or not key:
        raise ValueError(
            f"Invalid S3 URI '{uri}': expected the form s3://bucket/key"
        )
    return bucket, key


def cache_root() -> Path:
    """Root directory for the local S3 ontology cache."""
    override = os.environ.get("ONTOLOGY_QA_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "ontology-qa"


def local_cache_path(uri: str) -> Path:
    """Deterministic local path a given S3 URI is cached to (no I/O)."""
    bucket, key = parse_s3_uri(uri)
    digest = hashlib.sha1(uri.encode("utf-8")).hexdigest()[:16]
    basename = Path(key).name or f"{bucket}.owl"
    return cache_root() / digest / basename


def ensure_local_copy(uri: str, *, quiet: bool = False) -> Path:
    """Download the S3 object to the local cache if needed; return the path.

    Reuses an existing cache file when its size matches the S3 object's
    ``ContentLength``. Downloads are atomic (written to a ``.part`` file then
    renamed) so an interrupted download never leaves a truncated cache file
    that later looks valid.

    Raises:
        SystemExit: On any S3 / credential error, with an actionable message.
    """
    bucket, key = parse_s3_uri(uri)
    dest = local_cache_path(uri)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as e:  # pragma: no cover - boto3 is a core dep
        print(
            f"Error: boto3 is required to load ontologies from S3 - {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    s3 = boto3.client("s3")

    # Check the remote object's size to decide whether the cache is current.
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        remote_size = head.get("ContentLength")
    except (ClientError, BotoCoreError) as e:
        print(
            f"Error: cannot access S3 object '{uri}' - {type(e).__name__}: {e}\n"
            "  Check the URI, that the object exists, and that your AWS "
            "credentials grant s3:GetObject (e.g. `aws s3 ls "
            f"s3://{bucket}/{key}`).",
            file=sys.stderr,
        )
        sys.exit(1)

    if dest.exists() and remote_size is not None and dest.stat().st_size == remote_size:
        return dest  # cache hit

    if not quiet:
        size_mb = f"{remote_size / 1e6:.0f} MB" if remote_size else "unknown size"
        print(
            f"Downloading ontology from {uri} ({size_mb}) to {dest} ...",
            file=sys.stderr,
        )

    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        s3.download_file(bucket, key, str(tmp))
        os.replace(tmp, dest)
    except (ClientError, BotoCoreError, OSError) as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        print(
            f"Error: failed to download '{uri}' - {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    return dest

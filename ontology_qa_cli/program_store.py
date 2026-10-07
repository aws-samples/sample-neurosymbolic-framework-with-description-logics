"""Durable S3 store for evolved query programs (build outputs).

Evolved query programs are written by the builder to
``runs/<ONTOLOGY>_<timestamp>/iteration_N/query_program.py`` on the local
filesystem. In the deployed ECS stack ``runs/`` is a shared EFS mount, so a
built program already survives task restarts, redeploys, and horizontal
scaling, and is shared across every browser session hitting the running app.

What EFS does NOT survive is a teardown of the volume itself: a fresh account,
a VPC/stack rebuild, or deliberately destroying the stacks loses every evolved
program, forcing a rebuild from scratch. It also does not help a local /
subprocess-backend dev run (container-local ``runs/``) or a different developer
on a different machine.

This module adds a deployment-independent copy in S3. It mirrors
``s3_cache.py`` (the symmetric feature for *input* OWL files): lazy, env-gated,
boto3-based, atomic. On a successful build the whole run directory is uploaded
under ``s3://<bucket>/<prefix>/<ontology>/<timestamp>/``; when the local
``runs/`` has nothing for an ontology, the latest run is pulled back down before
the serve/query path loads it.

Enabled by the ``PROGRAM_STORE_S3`` environment variable, whose value is an
``s3://bucket/prefix`` URI (the ``prefix`` is optional; it defaults to
``programs``). When the variable is unset the module is a no-op -- every public
function returns immediately with no S3 call -- so local dev and the existing
test suite are unaffected.

The S3 layout mirrors the local run layout exactly::

    s3://<bucket>/<prefix>/<ontology>/<run_dir_name>/summary.json
    s3://<bucket>/<prefix>/<ontology>/<run_dir_name>/iteration_0/query_program.py
    s3://<bucket>/<prefix>/<ontology>/<run_dir_name>/iteration_0/evaluation.json
    ...

so ``<run_dir_name>`` (e.g. ``FMA_20260101_120000``) is preserved and the
timestamp in its name gives the same newest-wins ordering locally and remotely.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

_ENV_VAR = "PROGRAM_STORE_S3"
_DEFAULT_PREFIX = "programs"


def is_enabled() -> bool:
    """True if the durable program store is configured (``PROGRAM_STORE_S3`` set)."""
    return bool(os.environ.get(_ENV_VAR, "").strip())


def _store_location() -> Optional[tuple[str, str]]:
    """Return ``(bucket, prefix)`` from ``PROGRAM_STORE_S3``, or ``None`` if unset.

    The value is an ``s3://bucket[/prefix]`` URI. A missing key part defaults to
    ``programs``. A malformed value (no bucket) disables the store with a warning
    rather than crashing the build/serve path.
    """
    raw = os.environ.get(_ENV_VAR, "").strip()
    if not raw:
        return None
    if not raw.lower().startswith("s3://"):
        print(
            f"Warning: {_ENV_VAR}={raw!r} is not an s3:// URI; "
            "durable program store disabled.",
            file=sys.stderr,
        )
        return None
    parsed = urlparse(raw)
    bucket = parsed.netloc
    prefix = parsed.path.strip("/") or _DEFAULT_PREFIX
    if not bucket:
        print(
            f"Warning: {_ENV_VAR}={raw!r} has no bucket; "
            "durable program store disabled.",
            file=sys.stderr,
        )
        return None
    return bucket, prefix


def _ontology_prefix(prefix: str, ontology: str) -> str:
    """S3 key prefix under which a given ontology's runs live."""
    return f"{prefix}/{ontology}"


def _s3_client():
    """Create a boto3 S3 client, exiting with an actionable message on failure."""
    try:
        import boto3
    except ImportError as e:  # pragma: no cover - boto3 is a core dep
        print(
            f"Error: boto3 is required for the durable program store - {e}",
            file=sys.stderr,
        )
        sys.exit(1)
    return boto3.client("s3")


# --------------------------------------------------------------------------- #
# Upload (builder, on successful build)
# --------------------------------------------------------------------------- #
def upload_run(run_dir: Path, ontology: str, *, quiet: bool = False) -> bool:
    """Upload a completed run directory to the durable S3 store.

    Mirrors the local layout: every file under ``run_dir`` is uploaded under
    ``<prefix>/<ontology>/<run_dir.name>/<relative path>``. A best-effort
    operation -- any failure is logged and swallowed (returns ``False``) so a
    successful local build is never turned into a failure just because the
    durable copy could not be written.

    No-op (returns ``False``) when the store is not configured.

    Returns:
        ``True`` if the run was uploaded, ``False`` otherwise.
    """
    location = _store_location()
    if location is None:
        return False
    bucket, prefix = location

    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        if not quiet:
            print(
                f"Warning: run directory '{run_dir}' not found; "
                "nothing uploaded to the durable program store.",
                file=sys.stderr,
            )
        return False

    files = [p for p in run_dir.rglob("*") if p.is_file()]
    if not files:
        return False

    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:  # pragma: no cover - botocore ships with boto3
        BotoCoreError = ClientError = Exception  # type: ignore[assignment,misc]

    s3 = _s3_client()
    base_key = f"{_ontology_prefix(prefix, ontology)}/{run_dir.name}"

    if not quiet:
        print(
            f"Persisting {len(files)} file(s) to durable program store "
            f"s3://{bucket}/{base_key}/ ...",
            file=sys.stderr,
        )

    uploaded = 0
    for path in files:
        rel = path.relative_to(run_dir).as_posix()
        key = f"{base_key}/{rel}"
        try:
            s3.upload_file(str(path), bucket, key)
            uploaded += 1
        except (ClientError, BotoCoreError, OSError) as e:  # noqa: PERF203
            print(
                f"Warning: failed to upload '{path}' to s3://{bucket}/{key} "
                f"- {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            return False

    return uploaded == len(files)


# --------------------------------------------------------------------------- #
# Download (serve/query, on local miss)
# --------------------------------------------------------------------------- #
def _latest_remote_run(s3, bucket: str, onto_prefix: str) -> Optional[str]:
    """Return the newest run-dir NAME under ``<onto_prefix>/`` in S3, or None.

    "Newest" is the lexicographically-greatest immediate child "directory"
    (common prefix) -- identical to the local newest-wins rule, because the run
    name embeds a ``YYYYMMDD_HHMMSS`` timestamp.
    """
    paginator = s3.get_paginator("list_objects_v2")
    run_names: set[str] = set()
    for page in paginator.paginate(
        Bucket=bucket, Prefix=f"{onto_prefix}/", Delimiter="/"
    ):
        for cp in page.get("CommonPrefixes", []) or []:
            # cp["Prefix"] == "<onto_prefix>/<run_name>/"
            tail = cp["Prefix"][len(onto_prefix) + 1 :].rstrip("/")
            if tail:
                run_names.add(tail)
    if not run_names:
        return None
    return sorted(run_names)[-1]


def hydrate_ontology(ontology: str, runs_dir: Path, *, quiet: bool = False) -> Optional[Path]:
    """Ensure a local run exists for ``ontology``, pulling from S3 if needed.

    If ``runs_dir`` already contains any ``<ontology>_*`` run, this is a no-op
    (local always wins -- the fast path, no S3 call beyond nothing). Otherwise
    the newest run for the ontology is downloaded from the durable store into
    ``runs_dir`` under its original name.

    No-op (returns ``None``) when the store is not configured.

    Returns:
        The local run directory that was downloaded, or ``None`` if nothing was
        pulled (store disabled, no remote run, or a local run already existed).
    """
    location = _store_location()
    if location is None:
        return None
    bucket, prefix = location

    runs_dir = Path(runs_dir)
    # Local hit: a run folder for this ontology already exists -> do nothing.
    local_prefix = f"{ontology}_"
    if runs_dir.exists() and any(
        d.is_dir() and d.name.startswith(local_prefix) for d in runs_dir.iterdir()
    ):
        return None

    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:  # pragma: no cover
        BotoCoreError = ClientError = Exception  # type: ignore[assignment,misc]

    s3 = _s3_client()
    onto_prefix = _ontology_prefix(prefix, ontology)

    try:
        run_name = _latest_remote_run(s3, bucket, onto_prefix)
    except (ClientError, BotoCoreError) as e:
        print(
            f"Warning: could not list the durable program store "
            f"s3://{bucket}/{onto_prefix}/ - {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return None

    if run_name is None:
        return None  # nothing stored for this ontology

    base_key = f"{onto_prefix}/{run_name}"
    dest_run_dir = runs_dir / run_name

    if not quiet:
        print(
            f"Hydrating '{ontology}' from durable program store "
            f"s3://{bucket}/{base_key}/ -> {dest_run_dir} ...",
            file=sys.stderr,
        )

    paginator = s3.get_paginator("list_objects_v2")
    downloaded = 0
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{base_key}/"):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                rel = key[len(base_key) + 1 :]
                if not rel:  # the "directory" placeholder key, if any
                    continue
                dest = dest_run_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(dest.suffix + ".part")
                s3.download_file(bucket, key, str(tmp))
                os.replace(tmp, dest)
                downloaded += 1
    except (ClientError, BotoCoreError, OSError) as e:
        print(
            f"Warning: failed to hydrate '{ontology}' from "
            f"s3://{bucket}/{base_key}/ - {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return None

    if downloaded == 0:
        return None
    return dest_run_dir

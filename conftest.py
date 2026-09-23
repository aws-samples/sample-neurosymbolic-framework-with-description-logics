"""Root-level pytest configuration.

Purpose: make the test suite robust to a *SQLite-hostile* temp directory.

The serving layer (``ontology_qa_cli.serve.OntologyAnswerer``) caches each
ontology's owlready2 backend as an on-disk ``<name>.sqlite3`` file next to the
OWL file, and the serve/webapp tests drive it against files created under
pytest's ``tmp_path``. ``tmp_path`` lives under ``$TMPDIR``.

In some sandboxed environments (e.g. a macOS seatbelt profile that permits
ordinary file writes but blocks SQLite's VFS/locking syscalls) ``$TMPDIR``
points at a location where ``sqlite3.connect(path)`` fails with
``OperationalError: unable to open database file`` -- even though plain
``open(path, "w")`` in the same directory succeeds. That turns ~16 otherwise
passing tests red for reasons that have nothing to do with the code under test.

Fix: before pytest builds its temp-dir factory, probe whether SQLite can open a
database under the configured ``basetemp``/``$TMPDIR``. If it cannot, redirect
``--basetemp`` to a project-local directory that the probe confirms works. This
is a no-op on a normal machine where the default temp dir hosts SQLite fine.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path


def _sqlite_can_write(directory: Path) -> bool:
    """Return True iff a SQLite database can be created under ``directory``."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    probe = directory / ".sqlite_write_probe.sqlite3"
    try:
        con = sqlite3.connect(str(probe))
        con.execute("CREATE TABLE t (x)")
        con.close()
        return True
    except sqlite3.Error:
        return False
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def _first_sqlite_friendly_dir(candidates: list[Path]) -> Path | None:
    for cand in candidates:
        if _sqlite_can_write(cand):
            return cand
    return None


def pytest_configure(config) -> None:
    """Redirect basetemp to a SQLite-friendly dir when the default one isn't.

    Runs before the ``tmp_path``/``tmp_path_factory`` fixtures materialize any
    directories, so overriding ``config.option.basetemp`` here is honored by
    every test's ``tmp_path``.
    """
    # Where would pytest put temp dirs today? An explicit --basetemp wins;
    # otherwise it derives from the standard tempfile location ($TMPDIR).
    current_basetemp = config.option.basetemp
    probe_dir = (
        Path(current_basetemp)
        if current_basetemp
        else Path(tempfile.gettempdir())
    )

    if _sqlite_can_write(probe_dir):
        return  # Default location is fine; do nothing.

    # The default temp dir cannot host SQLite. Pick the first project-local /
    # home-based directory that can, and point pytest's basetemp at it.
    project_root = Path(__file__).resolve().parent
    fallback = _first_sqlite_friendly_dir(
        [
            project_root / ".pytest_tmp",
            Path.home() / ".cache" / "ontology-qa-pytest-tmp",
        ]
    )
    if fallback is None:
        # Nothing writable found; leave the default and let tests report the
        # real error rather than masking it with a misleading one.
        return

    config.option.basetemp = str(fallback)
    # Surface the redirect so a reader of the test output knows why tmp_path
    # moved out of $TMPDIR.
    os.environ.setdefault("PYTEST_SQLITE_TMP_REDIRECT", str(fallback))

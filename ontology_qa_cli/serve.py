"""Serving layer for evolved query programs.

The builder (``ontology-qa-build``) evolves a ``query_program.py`` whose
contract is a module-level ``main(onto_file: Path, question: str)`` that loads
the ontology and prints an ``Answer: <text>`` line to stdout. That contract is
fine for the batch evaluator (one subprocess per question) but a poor fit for a
chatbot: every call would re-parse / re-open the whole ontology and pay seconds
of startup cost.

This module wraps the evolved program in a long-lived object that loads the
ontology's owlready2 backend **once** at construction time, then answers many
questions in-process. It does so without requiring the LLM-generated program to
be written in a server-friendly way:

  * ``owlready2.default_world.set_backend`` is opened once here, up front.
  * While the program's ``main`` runs, ``owlready2.get_ontology(...).load()`` is
    intercepted so the already-loaded world is reused instead of re-parsed.
  * ``main``'s stdout is captured and the ``Answer:`` line extracted, matching
    the executor's contract exactly.

Typical use::

    from ontology_qa_cli.serve import OntologyAnswerer

    answerer = OntologyAnswerer.from_ontology("FMA")   # loads once
    print(answerer.answer("What are the parts of the heart?"))
    print(answerer.answer("What is the function of the liver?"))

For a chatbot, construct one ``OntologyAnswerer`` at startup and call
``.answer()`` per user turn. It is NOT thread-safe (owlready2's default world is
process-global and ``main`` is import-executed); serialize calls or run one
answerer per worker process.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import sys
import os
import threading
import types
from pathlib import Path
from typing import Callable, Optional

import owlready2

from ontology_qa_cli.config import load_config, resolve_ontology
from ontology_qa_cli.executor import extract_answer
from ontology_qa_cli.query import find_best_program


def _is_cache_write_error(exc: BaseException) -> bool:
    """True if ``exc`` is (or wraps) a SQLite cache write/open failure.

    owlready2's backend attach -- even ``read_only=True`` -- issues maintenance
    writes against the SQLite cache file. When that file (or its journal) is not
    writable by this process, SQLite raises one of:
      * ``attempt to write a readonly database`` (the file opened but a write
        failed -- the CL case, an existing cache on the shared EFS mount), or
      * ``unable to open database file`` (the file/journal could not be opened
        for writing at all).
    Match on the message so the fix is independent of the exact
    owlready2/sqlite exception type across versions.
    """
    needles = ("readonly database", "unable to open database file")
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        msg = str(cur).lower()
        if any(n in msg for n in needles):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


_GLOBAL_PATCH_LOCK = threading.RLock()


class OntologyAnswerer:
    """Loads an evolved query program + its ontology once, answers many questions.

    The ontology's owlready2 SQLite backend is opened at construction, so the
    expensive parse/load happens a single time. Each :meth:`answer` call invokes
    the evolved program's ``main`` against the already-loaded world.
    """

    def __init__(self, program_path: Path, onto_file: Path) -> None:
        self.program_path = Path(program_path)
        self.onto_file = Path(onto_file)
        self._lock = threading.Lock()

        if not self.program_path.exists():
            raise FileNotFoundError(f"Query program not found at '{self.program_path}'")
        if not self.onto_file.exists():
            raise FileNotFoundError(f"OWL file not found at '{self.onto_file}'")

        # Open the shared backend and load the ontology ONCE. Mirrors the seed
        # program's cache logic: reuse a warmed cache, otherwise parse the OWL.
        #
        # Load into a PRIVATE World rather than owlready2.default_world. The
        # default world is process-global: the web app is a single long-lived
        # process that may construct one answerer per ontology, and once one
        # answerer has loaded triples into the default world, a second
        # ``set_backend(<existing cache>)`` call raises "Cannot save existent
        # quadstore ... File already exists" because the world is no longer
        # empty. A fresh World per answerer keeps each ontology isolated.
        cache_file = self.onto_file.with_suffix(".sqlite3")
        self._world = self._open_reader(cache_file)
        onts = list(self._world.ontologies.values())
        if onts:
            # A reopened quadstore can expose more than one ontology (owlready2
            # adds an empty ``http://anonymous/`` world); pick the one that
            # actually carries the parsed classes rather than the first entry.
            self._onto = max(onts, key=lambda o: len(list(o.classes())))
        else:
            self._onto = self._world.get_ontology(f"file://{self.onto_file}").load()

        # Import the evolved program as a module so we can call main() directly.
        self._module = self._import_program(self.program_path)
        if not hasattr(self._module, "main"):
            raise AttributeError(
                f"Query program '{self.program_path}' has no top-level main(onto_file, question)"
            )

    def _open_reader(self, cache_file: Path) -> "owlready2.World":
        """Return a World backed by ``cache_file``, opened for read-only sharing.

        Answering only ever READS the ontology, so the backend is opened
        ``read_only=True, exclusive=False``. owlready2 defaults to an EXCLUSIVE
        SQLite lock, which makes a second answerer for the same ontology in the
        same long-lived web-app process fail with "database is locked"; a shared
        read lock lets answerers (and a concurrent builder) coexist.

        If the cache does not exist yet, it is created in a throwaway writable
        world (parse the OWL, persist, then close to drop the exclusive lock)
        before the read-only reader is opened over the freshly written file.

        If the on-disk cache cannot be written -- e.g. it already exists on the
        shared EFS cache dir (``ONTOLOGY_QA_CACHE_DIR``) with a file/journal this
        process may not write -- owlready2's backend attach (even ``read_only``)
        raises a SQLite write/open error. In that case we fall back to a FRESH
        IN-MEMORY load straight from the OWL: no on-disk backend, no write, no
        shared-file lock contention. That is exactly what the evolved query
        programs do at answer time, so it is a proven-safe path; the only cost
        is re-parsing the OWL once per answerer (then cached for the process).
        This fixes the CL symptom "Failed to load program: attempt to write a
        readonly database" seen only for ontologies whose cache already existed.
        """
        if not cache_file.exists():
            try:
                writer = owlready2.World()
                writer.set_backend(filename=str(cache_file))
                writer.get_ontology(f"file://{self.onto_file}").load()
                writer.save()
                writer.close()  # release the EXCLUSIVE write lock before reopening
            except Exception as exc:  # noqa: BLE001
                if _is_cache_write_error(exc):
                    return self._in_memory_world()
                raise

        reader = owlready2.World()
        try:
            reader.set_backend(
                filename=str(cache_file), exclusive=False, read_only=True
            )
        except Exception as exc:  # noqa: BLE001
            try:
                reader.close()
            except Exception:  # noqa: BLE001
                pass
            if _is_cache_write_error(exc):
                return self._in_memory_world()
            raise
        return reader

    def _in_memory_world(self) -> "owlready2.World":
        """Load the ontology into a fresh in-memory quadstore (no on-disk backend)."""
        mem = owlready2.World()  # no filename -> in-memory
        mem.get_ontology(f"file://{self.onto_file}").load()
        return mem

    @classmethod
    def from_ontology(
        cls,
        ontology_name: str,
        config_path: Path = Path("ontology_config.yaml"),
        runs_dir: Path = Path("runs/"),
    ) -> "OntologyAnswerer":
        """Build an answerer from the best program of the most recent build run.

        Args:
            ontology_name: Ontology name as configured (e.g. "FMA").
            config_path: Path to ontology_config.yaml (resolves the OWL file).
            runs_dir: Base directory holding builder run outputs.
        """
        config = load_config(Path(config_path).expanduser())
        entry = resolve_ontology(config, ontology_name)
        program_path = find_best_program(entry.name, Path(runs_dir).expanduser())
        return cls(program_path=program_path, onto_file=entry.owl_file)

    @classmethod
    def from_program(
        cls,
        program_path: Path,
        onto_file: Path,
    ) -> "OntologyAnswerer":
        """Build an answerer from an explicit program path and OWL file."""
        return cls(program_path=Path(program_path), onto_file=Path(onto_file))

    @staticmethod
    def _make_tolerant_set_backend(real, cache_file: Path):
        """Wrap ``World.set_backend`` so a program that touches a world BEFORE
        attaching our shared cache does not crash.

        owlready2 refuses to attach an existing quadstore to a world that
        already holds triples ("Cannot save existent quadstore ... File already
        exists"). Evolved programs can trigger that (e.g. creating a helper
        ontology or a private ``World()`` first). For the shared cache file we
        open it read-only/shared (like our own reader) and, if the world is
        already non-empty, discard its stray in-memory graph and attach
        cleanly. Any other file passes through untouched.
        """
        target = os.path.abspath(str(cache_file))

        def wrapper(world, *args, **kwargs):
            filename = kwargs.get("filename")
            if filename is None and len(args) > 1:
                filename = args[1]
            if filename is None or os.path.abspath(str(filename)) != target:
                return real(world, *args, **kwargs)
            kwargs.setdefault("exclusive", False)
            kwargs.setdefault("read_only", True)
            try:
                return real(world, *args, **kwargs)
            except ValueError as exc:
                if "Cannot save existent quadstore" not in str(exc):
                    raise
                # Drop stray in-memory triples/ontologies, then attach cleanly.
                world.graph = None
                world.ontologies.clear()
                return real(world, *args, **kwargs)

        return wrapper

    @staticmethod
    def _import_program(program_path: Path) -> types.ModuleType:
        """Import a query_program.py file as an isolated module object."""
        spec = importlib.util.spec_from_file_location(
            f"evolved_query_program_{abs(hash(str(program_path)))}", program_path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load query program from '{program_path}'")
        module = importlib.util.module_from_spec(spec)
        # NOTE: the program guards its CLI behavior under `if __name__ == "__main__"`,
        # so importing it does not execute main(). We call main() ourselves.
        spec.loader.exec_module(module)
        return module

    def answer(self, question: str) -> Optional[str]:
        """Answer one question against the pre-loaded ontology.

        Returns the extracted answer text, or ``None`` if the program did not
        emit an ``Answer:`` line. Raises nothing for a well-behaved program;
        exceptions from the evolved program's main() propagate to the caller.

        Calls are serialized: owlready2's default world is process-global and the
        evolved program mutates it, so concurrent calls are unsafe.
        """
        with self._lock:
            return self._run_once(question)

    def answer_streaming(
        self, question: str, on_line: "Callable[[str], None]"
    ) -> Optional[str]:
        """Answer one question, forwarding the program's output live.

        Identical to :meth:`answer`, but every stdout/stderr line the evolved
        program emits while it runs is passed to ``on_line`` as soon as it is
        written (newline-delimited, without the trailing newline). Use this to
        stream "what the program is doing" to a UI. The final extracted answer
        is still returned.
        """
        with self._lock:
            return self._run_once(question, on_line=on_line)

    def _run_once(
        self, question: str, on_line: "Optional[Callable[[str], None]]" = None
    ) -> Optional[str]:
        # Make the evolved program see OUR already-loaded ontology instead of
        # re-parsing the OWL or reopening the backend.
        #
        # The evolved programs (descended from seed_program.py) consume the
        # ontology through ``owlready2.default_world`` directly:
        #
        #     owlready2.default_world.set_backend(filename=cache_file)
        #     if not cache_exists or not list(owlready2.default_world.ontologies):
        #         onto = owlready2.get_ontology(...).load()
        #     else:
        #         onto = next(iter(owlready2.default_world.ontologies.values()))
        #     ... world = owlready2.default_world; world.search(label=...) ...
        #
        # So the data has to live in ``owlready2.default_world`` at call time.
        # We loaded it into a PRIVATE ``self._world`` (to keep one answerer per
        # ontology isolated in a single long-lived process), so for the duration
        # of this locked call we TEMPORARILY point the module-global
        # ``owlready2.default_world`` at ``self._world``. owlready2 reads that
        # global live on each ``owlready2.default_world.X`` access, so the
        # program's set_backend / .ontologies / .search all resolve against the
        # populated world. (A previous version patched set_backend to a no-op
        # on the ORIGINAL default_world and loaded into a private world, which
        # left ``owlready2.default_world`` empty -> search returned nothing ->
        # "I don't know".)
        #
        # ``owlready2.get_ontology`` is a SEPARATE module global bound to the
        # original world, so we repoint it too (to self._world's, returning the
        # pre-loaded ontology from .load()); and we neutralize set_backend on
        # self._world so the program's set_backend() call does not try to reopen
        # or relock the backend we already hold open.
        real_default_world = owlready2.default_world
        real_get_ontology = owlready2.get_ontology
        real_world_set_backend = self._world.set_backend
        loaded_onto = self._onto

        class _CachedOntology:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def load(self, *args, **kwargs):
                return self._wrapped

            def __getattr__(self, item):
                return getattr(self._wrapped, item)

        def _patched_get_ontology(iri, *args, **kwargs):
            # Any get_ontology(...).load() call returns the pre-loaded ontology.
            return _CachedOntology(loaded_onto)

        def _patched_set_backend(*args, **kwargs):
            # Backend is already open; do nothing so we don't reopen/relock it.
            return None

        # Capture stdout so we can extract the Answer: line. When streaming, tee
        # both stdout and stderr to the callback line-by-line as they are
        # written, while still accumulating stdout for answer extraction.
        buf = io.StringIO()
        sink: "io.TextIOBase" = _LineTee(buf, on_line) if on_line else buf

        # Programs that did ``from owlready2 import default_world, get_ontology``
        # hold the ORIGINAL objects in their own module globals, so repointing
        # the owlready2 module attributes alone does not reach them. Swap those
        # bindings too for the duration of the call.
        cache_file = self.onto_file.with_suffix(".sqlite3")
        real_world_cls_set_backend = owlready2.World.set_backend
        world_cls_set_backend = self._make_tolerant_set_backend(
            real_world_cls_set_backend, cache_file
        )
        module_globals = vars(self._module)
        swapped: dict = {}
        for key, val in list(module_globals.items()):
            if val is real_default_world:
                swapped[key] = val
                module_globals[key] = self._world
            elif val is real_get_ontology:
                swapped[key] = val
                module_globals[key] = _patched_get_ontology

        # owlready2's default/global state is process-wide, so all answerers
        # must take turns while it is repointed.
        _GLOBAL_PATCH_LOCK.acquire()
        owlready2.default_world = self._world
        owlready2.get_ontology = _patched_get_ontology
        owlready2.World.set_backend = world_cls_set_backend  # type: ignore[method-assign]
        self._world.set_backend = _patched_set_backend  # type: ignore[method-assign]
        try:
            if on_line is not None:
                # Stream stderr too (stacktraces, progress) but keep it out of
                # the answer buffer: a separate tee that only forwards lines.
                err_tee = _LineTee(io.StringIO(), on_line)
                with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(err_tee):
                    self._module.main(self.onto_file, question)
                err_tee.flush()
            else:
                with contextlib.redirect_stdout(sink):
                    self._module.main(self.onto_file, question)
        finally:
            if isinstance(sink, _LineTee):
                sink.flush()
            owlready2.default_world = real_default_world
            owlready2.get_ontology = real_get_ontology
            owlready2.World.set_backend = real_world_cls_set_backend  # type: ignore[method-assign]
            self._world.set_backend = real_world_set_backend  # type: ignore[method-assign]
            module_globals.update(swapped)
            _GLOBAL_PATCH_LOCK.release()

        return extract_answer(buf.getvalue())


class _LineTee(io.TextIOBase):
    """A text sink that mirrors writes into ``buffer`` and calls ``on_line`` for
    each completed line (split on ``\\n``), buffering any partial trailing line
    until the next write or :meth:`flush`.
    """

    def __init__(self, buffer: "io.StringIO", on_line: "Callable[[str], None]") -> None:
        self._buffer = buffer
        self._on_line = on_line
        self._partial = ""

    def write(self, s: str) -> int:  # type: ignore[override]
        self._buffer.write(s)
        self._partial += s
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._on_line(line)
        return len(s)

    def flush(self) -> None:  # type: ignore[override]
        if self._partial:
            self._on_line(self._partial)
            self._partial = ""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Load an evolved query program + its ontology once and answer "
            "questions from an interactive REPL (chatbot-style)."
        )
    )
    parser.add_argument(
        "source",
        help="Ontology name (e.g. 'FMA') or path to a query_program.py file.",
    )
    parser.add_argument(
        "question",
        nargs="?",
        default=None,
        help="Optional single question. If omitted, starts an interactive REPL.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("ontology_config.yaml"),
        help="Path to the YAML configuration file (default: ontology_config.yaml).",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("runs/"),
        help="Base directory where builder runs are stored (default: runs/).",
    )
    parser.add_argument(
        "--owl-file",
        type=Path,
        default=None,
        help="OWL file to use when source is a program path (overrides config lookup).",
    )
    return parser.parse_args()


def _build_answerer(args: argparse.Namespace) -> OntologyAnswerer:
    """Resolve args into an OntologyAnswerer (ontology name or program path)."""
    config = load_config(args.config.expanduser())
    for entry in config.ontologies:
        if entry.name.lower() == args.source.lower():
            program_path = find_best_program(entry.name, args.runs_dir.expanduser())
            return OntologyAnswerer.from_program(program_path, entry.owl_file)

    # Source is a program path.
    program_path = Path(args.source).expanduser()
    if not program_path.exists():
        print(
            f"Error: '{args.source}' is not a known ontology name and does not "
            "exist as a file path.",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.owl_file is not None:
        onto_file = args.owl_file.expanduser()
    elif len(config.ontologies) == 1:
        onto_file = config.ontologies[0].owl_file
    else:
        print(
            "Error: Multiple ontologies in config; pass --owl-file to say which "
            "OWL file this program should use.",
            file=sys.stderr,
        )
        sys.exit(1)
    return OntologyAnswerer.from_program(program_path, onto_file)


def main() -> None:
    """CLI entry point: single-shot answer or an interactive REPL."""
    args = _parse_args()
    print("Loading ontology (once)...", file=sys.stderr)
    answerer = _build_answerer(args)
    print("Ready.", file=sys.stderr)

    if args.question is not None:
        ans = answerer.answer(args.question)
        print(ans if ans is not None else "(no answer produced)")
        return

    # Interactive REPL — the ontology stays loaded across turns.
    print("Ask a question (Ctrl-D or 'quit' to exit):", file=sys.stderr)
    while True:
        try:
            line = input("> ")
        except EOFError:
            print()
            break
        q = line.strip()
        if not q:
            continue
        if q.lower() in {"quit", "exit"}:
            break
        try:
            ans = answerer.answer(q)
        except Exception as exc:  # noqa: BLE001 — surface program errors to the user
            print(f"Error answering question: {exc}", file=sys.stderr)
            continue
        print(ans if ans is not None else "(no answer produced)")


if __name__ == "__main__":
    main()

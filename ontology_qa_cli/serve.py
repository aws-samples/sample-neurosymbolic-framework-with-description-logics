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
import threading
import types
from pathlib import Path
from typing import Optional

import owlready2

from ontology_qa_cli.config import load_config, resolve_ontology
from ontology_qa_cli.executor import extract_answer
from ontology_qa_cli.query import find_best_program


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
        cache_file = self.onto_file.with_suffix(".sqlite3")
        cache_exists = cache_file.exists()
        owlready2.default_world.set_backend(filename=str(cache_file))
        if not cache_exists or not list(owlready2.default_world.ontologies):
            self._onto = owlready2.get_ontology(f"file://{self.onto_file}").load()
        else:
            self._onto = next(iter(owlready2.default_world.ontologies.values()))

        # Import the evolved program as a module so we can call main() directly.
        self._module = self._import_program(self.program_path)
        if not hasattr(self._module, "main"):
            raise AttributeError(
                f"Query program '{self.program_path}' has no top-level main(onto_file, question)"
            )

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

    def _run_once(self, question: str) -> Optional[str]:
        # Intercept the ontology (re)load so main() reuses the world we already
        # loaded, instead of re-parsing the OWL or reopening the backend.
        real_get_ontology = owlready2.get_ontology
        real_set_backend = owlready2.default_world.set_backend
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

        buf = io.StringIO()
        owlready2.get_ontology = _patched_get_ontology
        owlready2.default_world.set_backend = _patched_set_backend  # type: ignore[method-assign]
        try:
            with contextlib.redirect_stdout(buf):
                self._module.main(self.onto_file, question)
        finally:
            owlready2.get_ontology = real_get_ontology
            owlready2.default_world.set_backend = real_set_backend  # type: ignore[method-assign]

        return extract_answer(buf.getvalue())


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

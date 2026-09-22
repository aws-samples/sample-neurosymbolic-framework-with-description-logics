"""Query CLI for ontology QA CLI."""
import json
import sys
import argparse
from pathlib import Path

from ontology_qa_cli.config import load_config, resolve_ontology
from ontology_qa_cli.executor import execute_query_program
from ontology_qa_cli.ui import UI


def parse_args() -> argparse.Namespace:
    """Parse Query CLI arguments.

    Usage:
      python -m ontology_qa_cli.query <source> <question>

    Where <source> is either:
      - An ontology name (e.g. "FMA") — looks up the best program from the most recent run
      - A path to a query_program.py file
    """
    parser = argparse.ArgumentParser(
        description="Execute a query program to answer a natural language question about an OWL ontology."
    )
    parser.add_argument(
        "source",
        type=str,
        help="Ontology name (e.g. 'FMA') or path to a query_program.py file.",
    )
    parser.add_argument(
        "question",
        type=str,
        help="Natural language question to answer about the ontology.",
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
        "--plain",
        action="store_true",
        help="Disable the rich console UI and print the bare answer (useful for piping).",
    )
    return parser.parse_args()


def find_best_program(ontology_name: str, runs_dir: Path) -> Path:
    """Find the best query program from the most recent run for an ontology."""
    if not runs_dir.exists():
        print(f"Error: Runs directory not found at '{runs_dir}'", file=sys.stderr)
        sys.exit(1)

    prefix = f"{ontology_name}_"
    matching = sorted(
        [d for d in runs_dir.iterdir() if d.is_dir() and d.name.startswith(prefix)],
        key=lambda d: d.name,
    )

    if not matching:
        print(
            f"Error: No runs found for ontology '{ontology_name}' in '{runs_dir}'",
            file=sys.stderr,
        )
        sys.exit(1)

    latest_run = matching[-1]

    summary_path = latest_run / "summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        best_iteration = summary.get("best_iteration", 1)
    else:
        iter_dirs = sorted(
            [d for d in latest_run.iterdir() if d.is_dir() and d.name.startswith("iteration_")],
            key=lambda d: int(d.name.split("_")[1]),
        )
        if not iter_dirs:
            print(f"Error: No iteration directories found in '{latest_run}'", file=sys.stderr)
            sys.exit(1)
        best_iteration = int(iter_dirs[-1].name.split("_")[1])

    program_path = latest_run / f"iteration_{best_iteration}" / "query_program.py"
    if not program_path.exists():
        print(f"Error: Program not found at '{program_path}'", file=sys.stderr)
        sys.exit(1)

    print(f"Using program from: {program_path}", file=sys.stderr)
    return program_path


def run_query(args: argparse.Namespace) -> None:
    """Execute the query program and print the answer."""
    args.config = args.config.expanduser()
    args.runs_dir = args.runs_dir.expanduser()

    config = load_config(args.config)

    # Determine if source is an ontology name or a file path
    # Try to match it as an ontology name first
    ontology_entry = None
    for entry in config.ontologies:
        if entry.name.lower() == args.source.lower():
            ontology_entry = entry
            break

    if ontology_entry:
        # Source is an ontology name — find best program from most recent run
        program_path = find_best_program(ontology_entry.name, args.runs_dir)
        onto_file = ontology_entry.owl_file
    else:
        # Source is a path to a program
        program_path = Path(args.source).expanduser()
        if not program_path.exists():
            print(
                f"Error: '{args.source}' is not a known ontology name and does not exist as a file path.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Use the first ontology in config for the OWL file
        if len(config.ontologies) == 1:
            onto_file = config.ontologies[0].owl_file
        else:
            print(
                "Error: Multiple ontologies in config. Cannot determine which OWL file to use. "
                "Use an ontology name as the source instead.",
                file=sys.stderr,
            )
            sys.exit(1)

    question: str = args.question

    ui = UI(plain=getattr(args, "plain", False))

    # Execute the query program (with a spinner in fancy mode)
    with ui.thinking(f"Querying: {question}"):
        result = execute_query_program(program_path, onto_file, question)

    # Handle non-zero exit code
    if result.exit_code != 0:
        ui.error(f"Query program exited with code {result.exit_code}")
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(1)

    # Handle missing Answer: format
    if result.answer is None:
        ui.error(
            "Query program output did not contain an answer in the expected 'Answer: <text>' format."
        )
        sys.exit(1)

    # Render the extracted answer
    ui.answer(result.answer)


def main() -> None:
    """CLI entry point."""
    args = parse_args()
    run_query(args)


if __name__ == "__main__":
    main()

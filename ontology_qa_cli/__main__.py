"""Top-level entry point for `python -m ontology_qa_cli`.

Displays usage information for both CLI modules.
"""
import sys


def main() -> None:
    """Print usage information for the ontology_qa_cli package."""
    print("Ontology QA CLI - A tool for querying OWL DL ontologies using natural language")
    print()
    print("Available commands:")
    print()
    print("  python -m ontology_qa_cli.builder <ontology_name> [options]")
    print("    Iteratively build and improve a query program for an OWL ontology.")
    print()
    print("  python -m ontology_qa_cli.query <program> <question> [options]")
    print("    Execute a query program to answer a natural language question.")
    print()
    print("Or use the installed entry points:")
    print()
    print("  ontology-qa-build <ontology_name> [options]")
    print("  ontology-qa-query <program> <question> [options]")
    print()
    print("Run any command with --help for detailed usage information.")


if __name__ == "__main__":
    main()

"""Seed program generator for ontology QA CLI."""
import shutil
from pathlib import Path


# The seed program lives as a standalone file alongside this module
SEED_PROGRAM_PATH = Path(__file__).parent / "seed_program.py"


def generate_seed(ontology_name: str, output_path: Path) -> Path:
    """Copy the seed query program to the target directory.

    Args:
        ontology_name: Name of the ontology (unused, kept for API compatibility).
        output_path: Directory path where the seed program will be placed.

    Returns:
        Path to the copied seed program file.
    """
    output_path.mkdir(parents=True, exist_ok=True)
    seed_file = output_path / "query_program.py"
    shutil.copy2(SEED_PROGRAM_PATH, seed_file)
    return seed_file

"""Query program executor for ontology QA CLI."""
import sys
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ExecutionResult:
    question: str
    answer: Optional[str]  # None if extraction failed
    success: bool
    stdout: str
    stderr: str
    exit_code: int


def extract_answer(stdout: str) -> Optional[str]:
    """Extract answer from stdout matching 'Answer: <text>' format.

    Scans lines in reverse order, returns the first match.
    Returns None if no matching line found.
    """
    lines = stdout.splitlines()
    for line in reversed(lines):
        if line.startswith("Answer: "):
            return line[len("Answer: "):]
    return None


def execute_query_program(
    program_path: Path,
    onto_file: Path,
    question: str,
    timeout: int = 120,
) -> ExecutionResult:
    """Execute a query program as a subprocess.

    Runs `python <program_path> <onto_file> <question>` and extracts the answer
    from the last line matching 'Answer: <text>'.

    Args:
        program_path: Path to the Python query program.
        onto_file: Path to the OWL ontology file.
        question: Natural language question to answer.
        timeout: Maximum execution time in seconds (default 120).

    Returns:
        ExecutionResult with captured output and extracted answer.
    """
    try:
        result = subprocess.run(
            [sys.executable, str(program_path), str(onto_file), question],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return ExecutionResult(
            question=question,
            answer=None,
            success=False,
            stdout=e.stdout or "",
            stderr=f"Error: Timeout - query program exceeded {timeout} seconds",
            exit_code=-1,
        )

    stdout = result.stdout
    stderr = result.stderr
    exit_code = result.returncode

    if exit_code != 0:
        return ExecutionResult(
            question=question,
            answer=None,
            success=False,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
        )

    answer = extract_answer(stdout)
    success = answer is not None

    return ExecutionResult(
        question=question,
        answer=answer,
        success=success,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
    )

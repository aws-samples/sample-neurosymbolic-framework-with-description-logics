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
    """Extract the answer from stdout marked by an 'Answer: ' line.

    The query program prints its answer starting with a line ``Answer: <text>``.
    The answer MAY span multiple lines (e.g. a Markdown table or bulleted list):
    everything from the marker to the end of output is the answer. We take the
    LAST ``Answer: `` marker (so diagnostic chatter earlier in stdout is ignored)
    and return the marker's text plus any following lines, right-stripped.

    A single-line answer is returned exactly as before (no trailing newline),
    so this is backward-compatible with programs that print one line.

    Returns None if no ``Answer: `` marker is found.
    """
    lines = stdout.splitlines()
    marker = "Answer: "
    last_idx = None
    for i, line in enumerate(lines):
        if line.startswith(marker):
            last_idx = i
    if last_idx is None:
        return None
    first = lines[last_idx][len(marker):]
    rest = lines[last_idx + 1:]
    return "\n".join([first, *rest]).rstrip() or first


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

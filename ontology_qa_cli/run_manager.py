"""Run directory manager for ontology QA CLI."""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ontology_qa_cli.judge import JudgmentResult


@dataclass
class RunContext:
    base_dir: Path  # e.g., runs/FMA_20250101_120000/
    ontology_name: str
    start_time: datetime


def create_run_directory(ontology_name: str, output_base: Path) -> RunContext:
    """Create a timestamped run directory for the build session.

    Creates a directory named {ontology_name}_{YYYYMMDD}_{HHMMSS} under output_base.

    Args:
        ontology_name: Name of the ontology being built.
        output_base: Base directory for run outputs (e.g., runs/).

    Returns:
        A RunContext with the created directory path, ontology name, and start time.
    """
    start_time = datetime.now(timezone.utc)
    dir_name = f"{ontology_name}_{start_time.strftime('%Y%m%d')}_{start_time.strftime('%H%M%S')}"
    base_dir = output_base / dir_name
    base_dir.mkdir(parents=True, exist_ok=True)
    return RunContext(base_dir=base_dir, ontology_name=ontology_name, start_time=start_time)


def save_iteration(
    run_ctx: RunContext,
    iteration: int,
    program_source: str,
    evaluation_results: list[JudgmentResult],
    accuracy: float,
    task_file_content: Optional[str] = None,
) -> Path:
    """Save iteration artifacts to the run directory.

    Saves query_program.py, evaluation.json, and optionally TASK.md
    to an iteration subdirectory (e.g., iteration_0/).

    Args:
        run_ctx: The current run context.
        iteration: The iteration number (0-indexed).
        program_source: The source code of the query program.
        evaluation_results: List of JudgmentResult objects from the evaluation.
        accuracy: The computed accuracy for this iteration.
        task_file_content: Optional TASK.md content (included if program was fixed).

    Returns:
        Path to the iteration subdirectory.
    """
    iter_dir = run_ctx.base_dir / f"iteration_{iteration}"
    iter_dir.mkdir(parents=True, exist_ok=True)

    # Save query program
    program_path = iter_dir / "query_program.py"
    program_path.write_text(program_source)

    # Build evaluation data
    correct_count = sum(1 for r in evaluation_results if r.is_correct)
    failed_count = len(evaluation_results) - correct_count

    evaluation_data = {
        "iteration": iteration,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "accuracy": accuracy,
        "total_questions": len(evaluation_results),
        "correct_count": correct_count,
        "failed_count": failed_count,
        "results": [
            {
                "question": r.question,
                "generated_answer": r.generated_answer,
                "expected_answer": r.expected_answer,
                "assessment": r.assessment,
                "is_correct": r.is_correct,
                "stdout": r.stdout,
                "stderr": r.stderr,
            }
            for r in evaluation_results
        ],
    }

    # Save evaluation.json
    eval_path = iter_dir / "evaluation.json"
    eval_path.write_text(json.dumps(evaluation_data, indent=2))

    # Optionally save TASK.md
    if task_file_content is not None:
        task_path = iter_dir / "TASK.md"
        task_path.write_text(task_file_content)

    return iter_dir


def write_summary(
    run_ctx: RunContext,
    final_accuracy: float,
    total_iterations: int,
    best_iteration: int,
    best_iteration_path: Path,
    threshold: float = 0.9,
    terminated_reason: str = "threshold_met",
) -> None:
    """Write a summary file to the top-level run directory.

    Args:
        run_ctx: The current run context.
        final_accuracy: The accuracy of the final iteration.
        total_iterations: Total number of iterations completed.
        best_iteration: The iteration number with the highest accuracy.
        best_iteration_path: Path to the best iteration subdirectory.
        threshold: The accuracy threshold used.
        terminated_reason: Reason for termination (e.g., "threshold_met", "max_iterations").
    """
    end_time = datetime.now(timezone.utc)

    summary_data = {
        "ontology_name": run_ctx.ontology_name,
        "start_time": run_ctx.start_time.isoformat().replace("+00:00", "Z"),
        "end_time": end_time.isoformat().replace("+00:00", "Z"),
        "total_iterations": total_iterations,
        "final_accuracy": final_accuracy,
        "best_iteration": best_iteration,
        "best_iteration_path": str(best_iteration_path),
        "threshold": threshold,
        "terminated_reason": terminated_reason,
    }

    summary_path = run_ctx.base_dir / "summary.json"
    summary_path.write_text(json.dumps(summary_data, indent=2))

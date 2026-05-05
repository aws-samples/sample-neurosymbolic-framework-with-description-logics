"""Builder CLI for ontology QA CLI."""
import difflib
import logging
import random
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

import boto3

from ontology_qa_cli.config import load_config, resolve_ontology
from ontology_qa_cli.seed import generate_seed
from ontology_qa_cli.executor import execute_query_program
from ontology_qa_cli.judge import judge_answer, compute_accuracy, JudgmentResult

logger = logging.getLogger(__name__)
from ontology_qa_cli.fixer import fix_program, generate_task_file
from ontology_qa_cli.run_manager import create_run_directory, save_iteration, write_summary


def parse_args() -> argparse.Namespace:
    """Parse Builder CLI arguments.

    Arguments:
        ontology_name (required): Name of the ontology to build for.
        --config: Path to YAML config file (default: ontology_config.yaml).
        --threshold: Accuracy threshold (default: 0.8).
        --max-iterations: Maximum iterations (default: 10).
        --output-dir: Base directory for run outputs (default: runs/).
    """
    parser = argparse.ArgumentParser(
        description="Iteratively build and improve a query program for an OWL ontology."
    )
    parser.add_argument(
        "ontology_name",
        help="Name of the ontology to build a query program for (must match an entry in the config file).",
    )
    parser.add_argument(
        "--ontology-config",
        type=Path,
        default=Path("ontology_config.yaml"),
        help="Path to the YAML configuration file (default: ontology_config.yaml).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.8,
        help="Accuracy threshold to stop iterating (default: 0.8).",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=20,
        help="Maximum number of evaluation-fix iterations (default: 20).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("runs/"),
        help="Base directory for run output directories (default: runs/).",
    )
    parser.add_argument(
        "--kiro-timeout",
        type=int,
        default=600,
        help="Timeout in seconds for kiro-cli invocations (default: 600).",
    )
    parser.add_argument(
        "--max-questions",
        type=int,
        default=None,
        help="Maximum number of questions to use per iteration (random sample). Uses all if not specified.",
    )
    return parser.parse_args()


def run_build(args: argparse.Namespace) -> None:
    """Execute the full build loop."""
    # Expand ~ in all path arguments
    args.ontology_config = args.ontology_config.expanduser()
    args.output_dir = args.output_dir.expanduser()

    # 1. Load config
    config = load_config(args.ontology_config)

    # 2. Resolve ontology by name
    ontology = resolve_ontology(config, args.ontology_name)

    # 3. Validate OWL file exists
    owl_path = ontology.owl_file
    if not owl_path.exists():
        print(
            f"Error: OWL file not found at '{owl_path}'",
            file=sys.stderr,
        )
        sys.exit(1)

    # 4. Validate OWL file can be referenced (owlready2 loading is done by the query program,
    #    but we do a basic existence check here)
    try:
        import owlready2  # noqa: F401
    except ImportError as e:
        print(
            f"Error: owlready2 import failed - {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    # 5. Create Bedrock client
    try:
        bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")
    except Exception as e:
        print(
            f"Error: Failed to create Bedrock client - {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    # 6. Create run directory
    run_ctx = create_run_directory(ontology.name, args.output_dir)

    # 7. Generate seed program
    seed_dir = run_ctx.base_dir / "_seed"
    seed_path = generate_seed(ontology.name, seed_dir)
    current_program_source = seed_path.read_text()
    print(f"[{_timestamp()}] Generated seed program at {seed_path}")

    # Track best iteration
    best_accuracy = -1.0
    best_iteration = 1
    best_iteration_path = None
    total_iterations = 0
    previous_program_source = ""  # Empty so iteration 1 patch shows full seed

    # 8. Main evaluation loop
    for iteration in range(1, args.max_iterations + 1):
        print(f"\n[{_timestamp()}] Starting evaluation round {iteration}")

        # Write current program for execution
        program_path = run_ctx.base_dir / f"iteration_{iteration}" / "query_program.py"
        program_path.parent.mkdir(parents=True, exist_ok=True)
        program_path.write_text(current_program_source)

        # Save patch showing what changed from previous iteration
        _save_patch(previous_program_source, current_program_source, program_path.parent)

        # Select questions for this round
        questions = ontology.questions
        if args.max_questions and args.max_questions < len(questions):
            questions = random.sample(questions, args.max_questions)

        # Execute query program on all questions
        judgment_results: list[JudgmentResult] = []

        for qa in questions:
            exec_result = execute_query_program(
                program_path=program_path,
                onto_file=owl_path,
                question=qa.question,
            )

            # Log stdout and stderr with colored backgrounds and prefix
            BG_BLACK = "\033[40m\033[97m"  # black bg, white text
            BG_RED = "\033[41m\033[97m"    # red bg, white text
            RESET = "\033[0m"

            print(f"  Question: {qa.question}")
            if exec_result.stdout.strip():
                prefixed_stdout = "\n".join(
                    f"{BG_BLACK}program> {line}{RESET}"
                    for line in exec_result.stdout.strip().splitlines()
                )
                print(prefixed_stdout)
            if exec_result.stderr.strip():
                prefixed_stderr = "\n".join(
                    f"{BG_RED}program> {line}{RESET}"
                    for line in exec_result.stderr.strip().splitlines()
                )
                print(prefixed_stderr)

            if exec_result.success and exec_result.answer is not None:
                # Judge the answer
                try:
                    judgment = judge_answer(
                        question=qa.question,
                        generated_answer=exec_result.answer,
                        expected_answer=qa.expected_answer,
                        bedrock_client=bedrock_client,
                    )
                    judgment.stdout = exec_result.stdout
                    judgment.stderr = exec_result.stderr
                    judgment_results.append(judgment)
                except Exception as e:
                    print(
                        f"Error: Bedrock judge failed - {type(e).__name__}: {e}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            else:
                # Failed execution - record as incorrect
                generated = exec_result.answer or "(no answer produced)"
                assessment = (
                    f"Execution failed with exit code {exec_result.exit_code}. "
                    f"stderr: {exec_result.stderr}"
                    if exec_result.exit_code != 0
                    else "No answer line found in stdout."
                )
                judgment_results.append(
                    JudgmentResult(
                        question=qa.question,
                        generated_answer=generated,
                        expected_answer=qa.expected_answer,
                        assessment=assessment,
                        is_correct=False,
                        stdout=exec_result.stdout,
                        stderr=exec_result.stderr,
                    )
                )

        # Compute accuracy
        accuracy = compute_accuracy(judgment_results)
        print(f"[{_timestamp()}] Evaluation round {iteration} complete - accuracy: {accuracy:.2%}")

        # Track best iteration
        if accuracy > best_accuracy:
            best_accuracy = accuracy
            best_iteration = iteration
            best_iteration_path = program_path.parent

        # Determine task file content for saving
        failed_judgments = [j for j in judgment_results if not j.is_correct]
        task_file_content = None
        if failed_judgments:
            task_file_content = generate_task_file(failed_judgments, program_path)

        # Save iteration artifacts
        save_iteration(
            run_ctx=run_ctx,
            iteration=iteration,
            program_source=current_program_source,
            evaluation_results=judgment_results,
            accuracy=accuracy,
            task_file_content=task_file_content,
        )

        total_iterations = iteration

        # Check if threshold is met
        if accuracy >= args.threshold:
            print(
                f"\nSuccess! Accuracy threshold met: {accuracy:.2%} "
                f"(threshold: {args.threshold:.2%}) after {iteration} iteration(s)."
            )
            break

        # Fix program if accuracy is below threshold
        if failed_judgments:
            print(f"[{_timestamp()}] Fixing program based on {len(failed_judgments)} failed question(s)...")
            fix_result = fix_program(
                current_program=current_program_source,
                failed_judgments=failed_judgments,
                working_dir=run_ctx.base_dir,
                timeout=args.kiro_timeout,
            )
            print(
                f"[{_timestamp()}] kiro-cli invocation complete - "
                f"working dir: {run_ctx.base_dir}, exit code: {fix_result.exit_code}"
            )

            if fix_result.success and fix_result.modified_program:
                previous_program_source = current_program_source
                current_program_source = fix_result.modified_program
                print(f"[{_timestamp()}] Program updated successfully.")
            else:
                print(
                    f"[{_timestamp()}] kiro-cli fix failed (exit code {fix_result.exit_code}): "
                    f"{fix_result.error_message}. Retaining previous program."
                )
    else:
        # Max iterations reached without meeting threshold
        print(
            f"\nMax iterations ({args.max_iterations}) reached. "
            f"Best accuracy achieved: {best_accuracy:.2%} (threshold: {args.threshold:.2%})."
        )

    # Write summary
    terminated_reason = "threshold_met" if best_accuracy >= args.threshold else "max_iterations"
    write_summary(
        run_ctx=run_ctx,
        final_accuracy=best_accuracy,
        total_iterations=total_iterations,
        best_iteration=best_iteration,
        best_iteration_path=best_iteration_path,
        threshold=args.threshold,
        terminated_reason=terminated_reason,
    )

    # Print path to best-performing program
    print(f"\nBest-performing query program: {best_iteration_path}")


def _timestamp() -> str:
    """Return a formatted UTC timestamp string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _save_patch(prev_source: str, new_source: str, iteration_dir: Path) -> None:
    """Generate and save a unified diff patch file."""
    prev_lines = prev_source.splitlines(keepends=True)
    new_lines = new_source.splitlines(keepends=True)
    diff = difflib.unified_diff(
        prev_lines,
        new_lines,
        fromfile=f"query_program.py (iteration {int(iteration_dir.name.split('_')[-1]) - 1})",
        tofile=f"query_program.py (iteration {iteration_dir.name.split('_')[-1]})",
    )
    patch_content = "".join(diff)
    if patch_content:
        patch_file = iteration_dir / "changes.patch"
        patch_file.write_text(patch_content)
        logger.info("Patch saved to %s", patch_file)


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stdout,
    )
    args = parse_args()
    run_build(args)


if __name__ == "__main__":
    main()

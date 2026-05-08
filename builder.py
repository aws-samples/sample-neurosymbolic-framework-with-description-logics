"""Builder CLI for ontology QA CLI."""
import difflib
import logging
import random
import sys
import argparse
import time
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
        default=None,
        help="Timeout in seconds for kiro-cli invocations (default: no timeout).",
    )
    parser.add_argument(
        "--max-questions",
        type=int,
        default=None,
        help="Maximum number of questions to use per iteration (random sample). Uses all if not specified.",
    )
    parser.add_argument(
        "--num-failures-to-fix",
        type=int,
        default=None,
        help="Number of failed questions to include in TASK.md for kiro-cli to fix each round. Defaults to all failures.",
    )
    return parser.parse_args()


def run_build(args: argparse.Namespace) -> None:
    """Execute the full build loop."""
    # Expand ~ in all path arguments
    args.ontology_config = args.ontology_config.expanduser()
    args.output_dir = args.output_dir.expanduser()

    # 0. Check AWS credentials are available
    import os
    has_key_pair = os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")
    has_profile = os.environ.get("AWS_PROFILE")
    has_role = os.environ.get("AWS_ROLE_ARN") or os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE")
    has_sso = os.environ.get("AWS_SSO_ACCOUNT_ID") or os.path.exists(os.path.expanduser("~/.aws/credentials"))
    if not (has_key_pair or has_profile or has_role or has_sso):
        print(
            "Error: No AWS credentials found. Set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, "
            "AWS_PROFILE, or configure ~/.aws/credentials before running.",
            file=sys.stderr,
        )
        sys.exit(1)

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
    previous_program_source = current_program_source  # No patch for iteration 1

    # Track costs
    total_judge_input_tokens = 0
    total_judge_output_tokens = 0
    total_kiro_duration = 0.0

    # 8. Main evaluation loop
    for iteration in range(1, args.max_iterations + 1):
        print(f"\n[{_timestamp()}] Starting evaluation round {iteration}")
        iteration_start = time.monotonic()

        # Write current program for execution
        program_path = run_ctx.base_dir / f"iteration_{iteration}" / "query_program.py"
        program_path.parent.mkdir(parents=True, exist_ok=True)
        program_path.write_text(current_program_source)

        # Save patch showing what changed from previous iteration (skip iteration 1)
        if iteration > 1:
            _save_patch(previous_program_source, current_program_source, program_path.parent)

        # Select questions for this round
        questions = ontology.questions
        if args.max_questions and args.max_questions < len(questions):
            questions = random.sample(questions, args.max_questions)

        # Execute query program on all questions in parallel
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _evaluate_question(qa):
            """Run and judge a single question. Returns (qa, exec_result, judgment, duration)."""
            t0 = time.monotonic()
            exec_result = execute_query_program(
                program_path=program_path,
                onto_file=owl_path,
                question=qa.question,
            )

            if exec_result.success and exec_result.answer is not None:
                judgment = judge_answer(
                    question=qa.question,
                    generated_answer=exec_result.answer,
                    expected_answer=qa.expected_answer,
                    bedrock_client=bedrock_client,
                )
                judgment.stdout = exec_result.stdout
                judgment.stderr = exec_result.stderr
                duration = time.monotonic() - t0
                return (qa, exec_result, judgment, duration)
            else:
                generated = exec_result.answer or "(no answer produced)"
                assessment = (
                    f"Execution failed with exit code {exec_result.exit_code}. "
                    f"stderr: {exec_result.stderr}"
                    if exec_result.exit_code != 0
                    else "No answer line found in stdout."
                )
                judgment = JudgmentResult(
                    question=qa.question,
                    generated_answer=generated,
                    expected_answer=qa.expected_answer,
                    assessment=assessment,
                    is_correct=False,
                    stdout=exec_result.stdout,
                    stderr=exec_result.stderr,
                )
                duration = time.monotonic() - t0
                return (qa, exec_result, judgment, duration)

        # Run all questions in parallel, collect results
        results_by_question = {}
        eval_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=min(len(questions), 8)) as executor:
            futures = {executor.submit(_evaluate_question, qa): qa for qa in questions}
            for future in as_completed(futures):
                qa = futures[future]
                try:
                    results_by_question[qa.question] = future.result()
                except Exception as e:
                    print(
                        f"Error: Evaluation failed for question '{qa.question}' - {type(e).__name__}: {e}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
        eval_elapsed = time.monotonic() - eval_start

        # Print results sequentially (no interleaving) and collect judgments
        BG_BLACK = "\033[40m\033[97m"
        BG_RED = "\033[41m\033[97m"
        RESET = "\033[0m"

        judgment_results: list[JudgmentResult] = []
        for qa in questions:
            qa_key = qa.question
            if qa_key not in results_by_question:
                continue
            _, exec_result, judgment, duration = results_by_question[qa_key]

            print(f"  Question: {qa.question} ({duration:.1f}s)")
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

            judgment_results.append(judgment)

        print(f"  --- All {len(questions)} questions evaluated in {eval_elapsed:.1f}s (wall clock) ---")

        # Accumulate token usage
        for j in judgment_results:
            total_judge_input_tokens += j.input_tokens
            total_judge_output_tokens += j.output_tokens

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
        # Limit failures sent to kiro-cli if --num-failures-to-fix is set
        failures_to_fix = failed_judgments
        if args.num_failures_to_fix and args.num_failures_to_fix < len(failed_judgments):
            failures_to_fix = random.sample(failed_judgments, args.num_failures_to_fix)

        task_file_content = None
        if failed_judgments:
            task_file_content = generate_task_file(failures_to_fix, program_path, ontology_file=owl_path)

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

        # Print iteration summary
        iteration_duration = time.monotonic() - iteration_start
        correct_count = sum(1 for j in judgment_results if j.is_correct)
        program_loc = len(current_program_source.splitlines())

        # Check if threshold is met
        if accuracy >= args.threshold:
            print(
                f"Iteration summary: iteration={iteration} accuracy={accuracy:.2%} ({correct_count}/{len(judgment_results)} correct) "
                f"eval_duration={eval_elapsed:.1f}s iteration_duration={iteration_duration:.1f}s "
                f"program_loc={program_loc}"
            )
            print(
                f"\nSuccess! Accuracy threshold met: {accuracy:.2%} "
                f"(threshold: {args.threshold:.2%}) after {iteration} iteration(s)."
            )
            break

        # Fix program if accuracy is below threshold
        kiro_duration = 0.0
        if failed_judgments:
            print(f"[{_timestamp()}] Fixing program based on {len(failed_judgments)} failed question(s)...")
            fix_result = fix_program(
                current_program=current_program_source,
                failed_judgments=failures_to_fix,
                working_dir=program_path.parent,
                timeout=args.kiro_timeout,
                ontology_file=owl_path,
            )
            kiro_duration = fix_result.duration
            total_kiro_duration += kiro_duration
            print(
                f"[{_timestamp()}] kiro-cli invocation complete - "
                f"working dir: {program_path.parent}, exit code: {fix_result.exit_code}"
            )

            # Persist kiro-cli stdout and stderr
            if fix_result.stdout:
                (program_path.parent / "kiro_stdout.txt").write_text(fix_result.stdout)
            if fix_result.stderr:
                (program_path.parent / "kiro_stderr.txt").write_text(fix_result.stderr)

            if fix_result.success and fix_result.modified_program:
                previous_program_source = current_program_source
                current_program_source = fix_result.modified_program
                print(f"[{_timestamp()}] Program updated successfully.")
            else:
                print(
                    f"[{_timestamp()}] kiro-cli fix failed (exit code {fix_result.exit_code}): "
                    f"{fix_result.error_message}. Retaining previous program."
                )

        iteration_duration = time.monotonic() - iteration_start
        program_loc = len(current_program_source.splitlines())
        print(
            f"Iteration summary: iteration={iteration} accuracy={accuracy:.2%} ({correct_count}/{len(judgment_results)} correct) "
            f"eval_duration={eval_elapsed:.1f}s kiro_duration={kiro_duration:.1f}s iteration_duration={iteration_duration:.1f}s "
            f"program_loc={program_loc}"
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

    # Print cost/usage report
    total_judge_tokens = total_judge_input_tokens + total_judge_output_tokens
    print(f"\n--- Cost Report ---")
    print(f"Judge LLM tokens: {total_judge_input_tokens:,} input + {total_judge_output_tokens:,} output = {total_judge_tokens:,} total")
    print(f"kiro-cli total duration: {total_kiro_duration:.1f}s across {total_iterations} iteration(s)")
    print(f"-------------------")


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

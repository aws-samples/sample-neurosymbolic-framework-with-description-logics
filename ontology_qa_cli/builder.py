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
import yaml

from ontology_qa_cli.config import load_config, resolve_ontology
from ontology_qa_cli import llm_config
from ontology_qa_cli.seed import generate_seed
from ontology_qa_cli.executor import execute_query_program
from ontology_qa_cli.judge import judge_answer, compute_accuracy, JudgmentResult

logger = logging.getLogger(__name__)
from ontology_qa_cli.fixer import fix_program, generate_task_file
from ontology_qa_cli.run_manager import create_run_directory, save_iteration, write_summary
from ontology_qa_cli.ui import UI


def _load_llm_costs(costs_path: Path) -> dict:
    """Load LLM cost configuration. Returns a dict of model_id -> {input_cost_per_1k, output_cost_per_1k}."""
    if not costs_path.exists():
        return {}
    try:
        data = yaml.safe_load(costs_path.read_text())
        return data.get("models", {})
    except Exception:
        return {}


def _compute_cost(input_tokens: int, output_tokens: int, model_id: str, llm_costs: dict) -> float:
    """Compute USD cost for a given token count and model."""
    model_costs = llm_costs.get(model_id, {})
    input_rate = model_costs.get("input_cost_per_1k", 0.0)
    output_rate = model_costs.get("output_cost_per_1k", 0.0)
    return (input_tokens / 1000.0) * input_rate + (output_tokens / 1000.0) * output_rate


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
    parser.add_argument(
        "--llm-costs",
        type=Path,
        default=Path("llm-costs.yaml"),
        help="Path to LLM cost configuration file (default: llm-costs.yaml).",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="Disable the rich console UI and emit plain text output (useful for CI/log capture).",
    )
    return parser.parse_args()


def run_build(args: argparse.Namespace) -> None:
    """Execute the full build loop."""
    # Expand ~ in all path arguments
    args.ontology_config = args.ontology_config.expanduser()
    args.output_dir = args.output_dir.expanduser()

    # Presentation layer (rich console, or plain fallback)
    ui = UI(plain=getattr(args, "plain", False))
    _run_state["ui"] = ui

    # Load LLM cost configuration
    llm_costs = _load_llm_costs(args.llm_costs.expanduser())

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
        import owlready2
    except ImportError as e:
        print(
            f"Error: owlready2 import failed - {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    # 4b. Warm the owlready2 SQLite cache ONCE, serially, before any parallel
    #     evaluation. The query program uses a shared on-disk SQLite backend
    #     (onto.sqlite3). SQLite allows only one writer, so if the cache does
    #     not yet exist, N parallel worker processes would all race to create
    #     and populate it and all but one fail with "database is locked".
    #     Building it here in a single process means every worker later opens
    #     an already-populated backend and never contends on the write lock.
    cache_file = owl_path.with_suffix(".sqlite3")
    if not cache_file.exists():
        try:
            warm_world = owlready2.World(filename=str(cache_file))
            warm_world.get_ontology(f"file://{owl_path.resolve()}").load()
            warm_world.save()
            warm_world.close()
        except Exception as e:
            print(
                f"Error: failed to build ontology cache at '{cache_file}' - "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )
            sys.exit(1)

    # 5. Create Bedrock client
    try:
        bedrock_client = boto3.client("bedrock-runtime", region_name=llm_config.bedrock_region())
    except Exception as e:
        print(
            f"Error: Failed to create Bedrock client - {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    # 6. Create run directory
    run_ctx = create_run_directory(ontology.name, args.output_dir)

    # Render the run banner now that the ontology and params are validated
    ui.banner(ontology.name, args.threshold, args.max_iterations)

    # Set expectations: the evolution loop deliberately runs candidate programs
    # that often fail, and the fixer streams raw kiro-cli/subprocess output. The
    # errors and stacktraces below are expected and part of the process.
    ui.notice(
        "Heads up: it's normal to see errors and stacktraces below. The builder "
        "evolves the query program by trying new approaches, running them, and "
        "self-correcting from the failures. Errors during iterations don't mean "
        "the run has failed — they're how the system learns and improves."
    )

    # 7. Generate seed program
    seed_dir = run_ctx.base_dir / "_seed"
    seed_path = generate_seed(ontology.name, seed_dir)
    current_program_source = seed_path.read_text()
    ui.log(f"Generated seed program at {seed_path}")

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
    total_kiro_credits = 0.0
    iteration_stats = []  # Per-iteration stats for the report

    # 8. Main evaluation loop
    interrupted = False
    for iteration in range(1, args.max_iterations + 1):
        ui.iteration_start(iteration, args.max_iterations)
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
        judgment_results: list[JudgmentResult] = []
        for qa in questions:
            qa_key = qa.question
            if qa_key not in results_by_question:
                continue
            _, exec_result, judgment, duration = results_by_question[qa_key]

            ui.question_result(
                question=qa.question,
                duration=duration,
                is_correct=judgment.is_correct,
                stdout=exec_result.stdout,
                stderr=exec_result.stderr,
            )

            judgment_results.append(judgment)

        # Accumulate token usage
        for j in judgment_results:
            total_judge_input_tokens += j.input_tokens
            total_judge_output_tokens += j.output_tokens

        # Compute accuracy
        accuracy = compute_accuracy(judgment_results)
        correct_count = sum(1 for j in judgment_results if j.is_correct)
        ui.iteration_accuracy(iteration, accuracy, correct_count, len(judgment_results), eval_elapsed)

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
            # Collect stats for this final iteration before breaking
            iter_input_tokens = sum(j.input_tokens for j in judgment_results)
            iter_output_tokens = sum(j.output_tokens for j in judgment_results)
            iteration_stats.append({
                "iteration": iteration,
                "accuracy": accuracy,
                "correct": correct_count,
                "total_questions": len(judgment_results),
                "eval_duration": eval_elapsed,
                "kiro_duration": 0.0,
                "kiro_credits": 0.0,
                "iteration_duration": iteration_duration,
                "program_loc": program_loc,
                "judge_input_tokens": iter_input_tokens,
                "judge_output_tokens": iter_output_tokens,
            })
            ui.print(
                f"Iteration summary: iteration={iteration} accuracy={accuracy:.2%} ({correct_count}/{len(judgment_results)} correct) "
                f"eval_duration={eval_elapsed:.1f}s iteration_duration={iteration_duration:.1f}s "
                f"program_loc={program_loc}"
            )
            ui.success(accuracy, args.threshold, iteration)
            break

        # Fix program if accuracy is below threshold
        kiro_duration = 0.0
        kiro_credits = 0.0
        if failed_judgments:
            with ui.fixing(len(failed_judgments)):
                fix_result = fix_program(
                    current_program=current_program_source,
                    failed_judgments=failures_to_fix,
                    working_dir=program_path.parent,
                    timeout=args.kiro_timeout,
                    ontology_file=owl_path,
                )
            kiro_duration = fix_result.duration
            kiro_credits = fix_result.credits
            total_kiro_duration += kiro_duration
            total_kiro_credits += kiro_credits
            ui.log(
                f"kiro-cli invocation complete - "
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
                ui.fix_outcome(success=True, exit_code=fix_result.exit_code)
            else:
                ui.fix_outcome(
                    success=False,
                    exit_code=fix_result.exit_code,
                    error_message=fix_result.error_message or "",
                )

        iteration_duration = time.monotonic() - iteration_start
        program_loc = len(current_program_source.splitlines())
        ui.print(
            f"Iteration summary: iteration={iteration} accuracy={accuracy:.2%} ({correct_count}/{len(judgment_results)} correct) "
            f"eval_duration={eval_elapsed:.1f}s kiro_duration={kiro_duration:.1f}s iteration_duration={iteration_duration:.1f}s "
            f"program_loc={program_loc}"
        )

        # Collect per-iteration stats for the report
        iter_input_tokens = sum(j.input_tokens for j in judgment_results)
        iter_output_tokens = sum(j.output_tokens for j in judgment_results)
        iteration_stats.append({
            "iteration": iteration,
            "accuracy": accuracy,
            "correct": correct_count,
            "total_questions": len(judgment_results),
            "eval_duration": eval_elapsed,
            "kiro_duration": kiro_duration,
            "kiro_credits": kiro_credits,
            "iteration_duration": iteration_duration,
            "program_loc": program_loc,
            "judge_input_tokens": iter_input_tokens,
            "judge_output_tokens": iter_output_tokens,
        })

        # Update run state for interrupt handling
        _run_state.update({
            "run_ctx": run_ctx,
            "args": args,
            "best_accuracy": best_accuracy,
            "best_iteration": best_iteration,
            "best_iteration_path": best_iteration_path,
            "total_iterations": total_iterations,
            "terminated_reason": "in_progress",
            "total_judge_input_tokens": total_judge_input_tokens,
            "total_judge_output_tokens": total_judge_output_tokens,
            "total_kiro_duration": total_kiro_duration,
            "total_kiro_credits": total_kiro_credits,
            "iteration_stats": iteration_stats,
            "llm_costs": llm_costs,
        })
    else:
        # Max iterations reached without meeting threshold
        ui.exhausted(best_accuracy, args.threshold, args.max_iterations)

    # Write summary
    if interrupted:
        terminated_reason = "interrupted"
    elif best_accuracy >= args.threshold:
        terminated_reason = "threshold_met"
    else:
        terminated_reason = "max_iterations"
    write_summary(
        run_ctx=run_ctx,
        final_accuracy=best_accuracy,
        total_iterations=total_iterations,
        best_iteration=best_iteration,
        best_iteration_path=best_iteration_path,
        threshold=args.threshold,
        terminated_reason=terminated_reason,
    )

    # Render the run summary table, then point at the best program
    ui.summary_table(iteration_stats)
    ui.best_program(best_iteration_path)

    # Store state for report generation (also used by KeyboardInterrupt handler)
    _run_state.update({
        "run_ctx": run_ctx,
        "args": args,
        "best_accuracy": best_accuracy,
        "best_iteration": best_iteration,
        "best_iteration_path": best_iteration_path,
        "total_iterations": total_iterations,
        "terminated_reason": terminated_reason,
        "total_judge_input_tokens": total_judge_input_tokens,
        "total_judge_output_tokens": total_judge_output_tokens,
        "total_kiro_duration": total_kiro_duration,
        "total_kiro_credits": total_kiro_credits,
        "iteration_stats": iteration_stats,
        "llm_costs": llm_costs,
    })
    _generate_report(interrupted=False, **_run_state)


# Module-level state for interrupt handling
_run_state = {}


def _generate_report(
    interrupted: bool,
    run_ctx,
    args,
    best_accuracy: float,
    best_iteration: int,
    best_iteration_path,
    total_iterations: int,
    terminated_reason: str,
    total_judge_input_tokens: int,
    total_judge_output_tokens: int,
    total_kiro_duration: float,
    total_kiro_credits: float,
    iteration_stats: list,
    llm_costs: dict,
    ui=None,
) -> None:
    """Generate the REPORT.md file."""
    run_end_time = datetime.now(timezone.utc)
    run_duration = (run_end_time - run_ctx.start_time).total_seconds()
    total_judge_tokens = total_judge_input_tokens + total_judge_output_tokens

    judge_model = llm_config.bedrock_model()
    KIRO_CREDIT_COST = 0.04  # USD per credit
    total_judge_cost = _compute_cost(total_judge_input_tokens, total_judge_output_tokens, judge_model, llm_costs)

    if interrupted:
        terminated_reason = "interrupted"

    report_lines = []
    report_lines.append(f"# Run Report: {run_ctx.ontology_name}")
    report_lines.append("")
    report_lines.append("## Parameters")
    report_lines.append("")
    report_lines.append(f"| Parameter | Value |")
    report_lines.append(f"|-----------|-------|")
    report_lines.append(f"| Ontology | {run_ctx.ontology_name} |")
    report_lines.append(f"| Config | {args.ontology_config} |")
    report_lines.append(f"| Threshold | {args.threshold:.0%} |")
    report_lines.append(f"| Max iterations | {args.max_iterations} |")
    report_lines.append(f"| Max questions | {args.max_questions or 'all'} |")
    report_lines.append(f"| Num failures to fix | {args.num_failures_to_fix or 'all'} |")
    report_lines.append(f"| Kiro timeout | {args.kiro_timeout or 'none'} |")
    report_lines.append(f"| Output dir | {run_ctx.base_dir} |")
    report_lines.append("")
    report_lines.append("## Summary")
    report_lines.append("")
    report_lines.append(f"| Metric | Value |")
    report_lines.append(f"|--------|-------|")
    report_lines.append(f"| Start time | {run_ctx.start_time.strftime('%Y-%m-%d %H:%M:%S UTC')} |")
    report_lines.append(f"| End time | {run_end_time.strftime('%Y-%m-%d %H:%M:%S UTC')} |")
    report_lines.append(f"| Duration | {run_duration:.0f}s ({run_duration/60:.1f} min) |")
    report_lines.append(f"| Total iterations | {total_iterations} |")
    report_lines.append(f"| Final accuracy | {best_accuracy:.2%} |")
    report_lines.append(f"| Best iteration | {best_iteration} |")
    report_lines.append(f"| Termination reason | {terminated_reason} |")
    report_lines.append(f"| Judge tokens (input) | {total_judge_input_tokens:,} |")
    report_lines.append(f"| Judge tokens (output) | {total_judge_output_tokens:,} |")
    report_lines.append(f"| Judge tokens (total) | {total_judge_tokens:,} |")
    report_lines.append(f"| Judge LLM cost | ${total_judge_cost:.4f} |")
    report_lines.append(f"| kiro-cli total duration | {total_kiro_duration:.0f}s ({total_kiro_duration/60:.1f} min) |")
    report_lines.append(f"| kiro-cli total credits | {total_kiro_credits:.2f} |")
    report_lines.append(f"| kiro-cli cost | ${total_kiro_credits * KIRO_CREDIT_COST:.4f} |")
    total_cost = total_judge_cost + (total_kiro_credits * KIRO_CREDIT_COST)
    report_lines.append(f"| **Total cost** | **${total_cost:.4f}** |")
    report_lines.append("")
    report_lines.append("## Iterations")
    report_lines.append("")
    report_lines.append("| Iteration | Accuracy | Correct | Eval (s) | Kiro (s) | Total (s) | LOC | Patch | Evaluation | Judge Tokens | Judge Cost ($) | Kiro Credits | Kiro Cost ($) |")
    report_lines.append("|-----------|----------|---------|----------|----------|-----------|-----|-------|------------|--------------|----------------|--------------|---------------|")
    for s in iteration_stats:
        iter_cost = _compute_cost(s['judge_input_tokens'], s['judge_output_tokens'], judge_model, llm_costs)
        iter_num = s['iteration']
        iter_dir = f"iteration_{iter_num}"
        loc_link = f"[{s['program_loc']}]({iter_dir}/query_program.py)"
        patch_link = f"[patch]({iter_dir}/changes.patch)" if iter_num > 1 else "—"
        eval_link = f"[eval]({iter_dir}/evaluation.json)"
        iter_kiro_credits = s.get('kiro_credits', 0.0)
        iter_kiro_cost = iter_kiro_credits * KIRO_CREDIT_COST
        report_lines.append(
            f"| {iter_num} "
            f"| {s['accuracy']:.0%} "
            f"| {s['correct']}/{s['total_questions']} "
            f"| {s['eval_duration']:.0f} "
            f"| {s['kiro_duration']:.0f} "
            f"| {s['iteration_duration']:.0f} "
            f"| {loc_link} "
            f"| {patch_link} "
            f"| {eval_link} "
            f"| {s['judge_input_tokens'] + s['judge_output_tokens']:,} "
            f"| {iter_cost:.4f} "
            f"| {iter_kiro_credits:.2f} "
            f"| {iter_kiro_cost:.4f} |"
        )

    if not interrupted and iteration_stats:
        # Summary row (only for completed runs)
        sum_eval = sum(s['eval_duration'] for s in iteration_stats)
        sum_kiro = sum(s['kiro_duration'] for s in iteration_stats)
        sum_total = sum(s['iteration_duration'] for s in iteration_stats)
        sum_tokens = sum(s['judge_input_tokens'] + s['judge_output_tokens'] for s in iteration_stats)
        sum_cost = _compute_cost(total_judge_input_tokens, total_judge_output_tokens, judge_model, llm_costs)
        sum_kiro_credits = sum(s.get('kiro_credits', 0.0) for s in iteration_stats)
        sum_kiro_cost = sum_kiro_credits * KIRO_CREDIT_COST
        report_lines.append(
            f"| **Total** "
            f"| "
            f"| "
            f"| **{sum_eval:.0f}** "
            f"| **{sum_kiro:.0f}** "
            f"| **{sum_total:.0f}** "
            f"| "
            f"| "
            f"| "
            f"| **{sum_tokens:,}** "
            f"| **{sum_cost:.4f}** "
            f"| **{sum_kiro_credits:.2f}** "
            f"| **{sum_kiro_cost:.4f}** |"
        )
    report_lines.append("")

    # Add Mermaid line chart for accuracy and LOC over iterations
    if iteration_stats:
        report_lines.append("## Progress")
        report_lines.append("")
        x_axis = ", ".join(str(s["iteration"]) for s in iteration_stats)
        accuracy_pcts = ", ".join(f'{s["accuracy"] * 100:.0f}' for s in iteration_stats)
        loc_values = ", ".join(str(s["program_loc"]) for s in iteration_stats)

        report_lines.append("```mermaid")
        report_lines.append("xychart-beta")
        report_lines.append(f'    title "Accuracy over iterations"')
        report_lines.append(f'    x-axis "Iteration" [{x_axis}]')
        report_lines.append(f'    y-axis "Accuracy (%)" 0 --> 100')
        report_lines.append(f'    line "Accuracy" [{accuracy_pcts}]')
        report_lines.append("```")
        report_lines.append("")
        report_lines.append("```mermaid")
        report_lines.append("xychart-beta")
        report_lines.append(f'    title "Program size over iterations"')
        report_lines.append(f'    x-axis "Iteration" [{x_axis}]')
        report_lines.append(f'    y-axis "Lines of code"')
        report_lines.append(f'    line "LOC" [{loc_values}]')
        report_lines.append("```")
        report_lines.append("")

    report_content = "\n".join(report_lines)
    report_path = run_ctx.base_dir / "REPORT.md"
    report_path.write_text(report_content)
    if ui is not None:
        ui.report_saved(report_path)
    else:
        print(f"Run report saved to: {report_path}")


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
    try:
        run_build(args)
    except KeyboardInterrupt:
        _ui = _run_state.get("ui")
        if _ui is not None:
            _ui.print("\n\nInterrupted by user. Generating partial report...")
        else:
            print("\n\nInterrupted by user. Generating partial report...")
        # run_build stores state needed for report in _run_state
        if _run_state.get("run_ctx") is not None:
            _generate_report(interrupted=True, **_run_state)


if __name__ == "__main__":
    main()

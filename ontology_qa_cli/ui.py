"""Presentation layer for the ontology QA CLI.

Wraps `rich` to render a polished console experience for the build loop and
query command, with a graceful fallback to plain ``print`` when either the
user passes ``--plain`` or the ``rich`` package is not installed.

Design notes
------------
- All output flows through a single :class:`UI` instance so the rest of the
  codebase never imports ``rich`` directly and never has to branch on whether
  fancy output is available.
- The build loop runs query programs in parallel subprocesses and streams
  ``kiro-cli`` output straight to the terminal. A full-screen ``Live`` display
  would fight that streaming, so we deliberately use discrete Rich primitives
  (panels, rules, tables, a transient spinner) that interleave cleanly with
  raw subprocess output rather than a single owning ``Live`` region.
- The plain fallback preserves the exact information the original code printed
  so logs and CI captures stay readable.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

try:  # pragma: no cover - exercised indirectly via _RICH
    from rich.console import Console
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text
    from rich import box

    _RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - fallback path
    _RICH_AVAILABLE = False


def _timestamp() -> str:
    """Return a formatted UTC timestamp string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class UI:
    """Console renderer with a fancy (rich) and a plain mode.

    Parameters
    ----------
    plain:
        Force plain output even when ``rich`` is installed. Useful for CI logs
        and captured output.
    stream:
        Output stream (defaults to ``sys.stdout``).
    """

    def __init__(self, plain: bool = False, stream=None) -> None:
        self.stream = stream or sys.stdout
        # Fancy mode requires rich AND not being forced plain AND a real TTY.
        # When output is redirected to a file we drop to plain so the log is
        # free of cursor/spinner control sequences.
        is_tty = getattr(self.stream, "isatty", lambda: False)()
        self.fancy = _RICH_AVAILABLE and not plain and is_tty
        self._console: Optional["Console"] = None
        if self.fancy:
            self._console = Console(file=self.stream, highlight=False)

    # -- generic helpers ---------------------------------------------------

    @property
    def console(self) -> Optional["Console"]:
        return self._console

    def print(self, message: str = "") -> None:
        """Print a plain line (no markup interpretation)."""
        if self.fancy:
            self._console.print(message, markup=False)
        else:
            print(message, file=self.stream)

    def log(self, message: str) -> None:
        """Print a timestamped log line."""
        ts = _timestamp()
        if self.fancy:
            self._console.print(f"[dim]\\[{ts}][/dim] {message}")
        else:
            print(f"[{ts}] {message}", file=self.stream)

    # -- build-loop rendering ---------------------------------------------

    def banner(self, ontology_name: str, threshold: float, max_iterations: int) -> None:
        """Render the run header."""
        if self.fancy:
            body = Text.assemble(
                ("Ontology QA Builder\n", "bold cyan"),
                ("Iteratively evolves a query program until it answers well.\n\n", "dim"),
                ("Ontology   ", "bold"), (f"{ontology_name}\n", ""),
                ("Threshold  ", "bold"), (f"{threshold:.0%}\n", ""),
                ("Max iters  ", "bold"), (f"{max_iterations}", ""),
            )
            self._console.print(Panel(body, box=box.ROUNDED, border_style="cyan", expand=False))
        else:
            print(
                f"Ontology QA Builder | ontology={ontology_name} "
                f"threshold={threshold:.0%} max_iterations={max_iterations}",
                file=self.stream,
            )

    def notice(self, message: str) -> None:
        """Render an informational, reassuring note.

        Used to set expectations before the evolution loop (e.g. that errors
        and stacktraces are a normal part of the self-correcting process).
        """
        if self.fancy:
            self._console.print(
                Panel(
                    Text(message),
                    box=box.ROUNDED,
                    border_style="dim",
                    expand=False,
                )
            )
        else:
            print(message, file=self.stream)

    def iteration_start(self, iteration: int, max_iterations: int) -> None:
        """Render the start-of-iteration separator."""
        if self.fancy:
            self._console.print(
                Rule(f"[bold]Iteration {iteration}[/bold] / {max_iterations}", style="blue")
            )
        else:
            print(f"\n[{_timestamp()}] Starting evaluation round {iteration}", file=self.stream)

    def question_result(
        self,
        question: str,
        duration: float,
        is_correct: bool,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        """Render a single evaluated question with pass/fail styling.

        In fancy mode program stdout/stderr is folded into a dim/red block so
        it is visible but visually subordinate to the verdict line.
        """
        if self.fancy:
            mark = "[green]PASS[/green]" if is_correct else "[red]FAIL[/red]"
            self._console.print(f"  {mark} [dim]{duration:.1f}s[/dim]  {escape(question)}")
            if stdout.strip():
                for line in stdout.strip().splitlines():
                    self._console.print(f"        [dim]program> {escape(line)}[/dim]")
            if stderr.strip():
                for line in stderr.strip().splitlines():
                    self._console.print(f"        [red]program> {escape(line)}[/red]")
        else:
            # Preserve the original plain rendering, including ANSI backgrounds.
            BG_BLACK = "\033[40m\033[97m"
            BG_RED = "\033[41m\033[97m"
            RESET = "\033[0m"
            print(f"  Question: {question} ({duration:.1f}s)", file=self.stream)
            if stdout.strip():
                for line in stdout.strip().splitlines():
                    print(f"{BG_BLACK}program> {line}{RESET}", file=self.stream)
            if stderr.strip():
                for line in stderr.strip().splitlines():
                    print(f"{BG_RED}program> {line}{RESET}", file=self.stream)

    def iteration_accuracy(
        self, iteration: int, accuracy: float, correct: int, total: int, elapsed: float
    ) -> None:
        """Render the accuracy result for a completed iteration."""
        if self.fancy:
            color = "green" if accuracy >= 0.8 else ("yellow" if accuracy >= 0.5 else "red")
            self._console.print(
                f"  [dim]All {total} questions evaluated in {elapsed:.1f}s[/dim]"
            )
            self._console.print(
                f"  Accuracy: [{color} bold]{accuracy:.1%}[/{color} bold] "
                f"[dim]({correct}/{total} correct)[/dim]"
            )
        else:
            print(
                f"  --- All {total} questions evaluated in {elapsed:.1f}s (wall clock) ---",
                file=self.stream,
            )
            print(
                f"[{_timestamp()}] Evaluation round {iteration} complete - accuracy: {accuracy:.2%}",
                file=self.stream,
            )

    @contextmanager
    def fixing(self, num_failures: int) -> Iterator[None]:
        """Context manager rendering a spinner while kiro-cli fixes the program.

        The spinner is stopped for the duration of the block in fancy mode so
        the streamed kiro-cli subprocess output (written directly to the
        terminal by the fixer) is not clobbered by spinner redraws. We show a
        status line before and after instead, which is the honest rendering
        for a long streaming subprocess.
        """
        if self.fancy:
            self._console.print(
                f"  [yellow]->[/yellow] Fixing program from "
                f"[bold]{num_failures}[/bold] failed question(s) via kiro-cli..."
            )
        else:
            print(
                f"[{_timestamp()}] Fixing program based on {num_failures} failed question(s)...",
                file=self.stream,
            )
        try:
            yield
        finally:
            pass

    def fix_outcome(self, success: bool, exit_code: int, error_message: str = "") -> None:
        """Render the result of a kiro-cli fix attempt."""
        if self.fancy:
            if success:
                self._console.print("  [green]Program updated successfully.[/green]")
            else:
                self._console.print(
                    f"  [red]Fix failed[/red] [dim](exit {exit_code})[/dim]: "
                    f"{escape(error_message)}. Retaining previous program."
                )
        else:
            if success:
                print(f"[{_timestamp()}] Program updated successfully.", file=self.stream)
            else:
                print(
                    f"[{_timestamp()}] kiro-cli fix failed (exit code {exit_code}): "
                    f"{error_message}. Retaining previous program.",
                    file=self.stream,
                )

    def success(self, accuracy: float, threshold: float, iterations: int) -> None:
        """Render a terminal success message (threshold met)."""
        if self.fancy:
            body = Text.assemble(
                ("Success! ", "bold green"),
                (f"Reached {accuracy:.1%} accuracy ", ""),
                (f"(threshold {threshold:.0%}) ", "dim"),
                (f"after {iterations} iteration(s).", ""),
            )
            self._console.print(Panel(body, box=box.ROUNDED, border_style="green", expand=False))
        else:
            print(
                f"\nSuccess! Accuracy threshold met: {accuracy:.2%} "
                f"(threshold: {threshold:.2%}) after {iterations} iteration(s).",
                file=self.stream,
            )

    def exhausted(self, best_accuracy: float, threshold: float, max_iterations: int) -> None:
        """Render a terminal message when max iterations are reached."""
        if self.fancy:
            body = Text.assemble(
                (f"Max iterations ({max_iterations}) reached.\n", "bold yellow"),
                (f"Best accuracy: {best_accuracy:.1%} ", ""),
                (f"(threshold {threshold:.0%}).", "dim"),
            )
            self._console.print(Panel(body, box=box.ROUNDED, border_style="yellow", expand=False))
        else:
            print(
                f"\nMax iterations ({max_iterations}) reached. "
                f"Best accuracy achieved: {best_accuracy:.2%} (threshold: {threshold:.2%}).",
                file=self.stream,
            )

    def summary_table(self, iteration_stats: list) -> None:
        """Render a per-iteration summary table at the end of the run."""
        if not iteration_stats:
            return
        if self.fancy:
            table = Table(title="Run summary", box=box.SIMPLE_HEAVY, title_style="bold")
            table.add_column("Iter", justify="right")
            table.add_column("Accuracy", justify="right")
            table.add_column("Correct", justify="right")
            table.add_column("Eval (s)", justify="right")
            table.add_column("Kiro (s)", justify="right")
            table.add_column("LOC", justify="right")
            for s in iteration_stats:
                acc = s["accuracy"]
                color = "green" if acc >= 0.8 else ("yellow" if acc >= 0.5 else "red")
                table.add_row(
                    str(s["iteration"]),
                    f"[{color}]{acc:.0%}[/{color}]",
                    f"{s['correct']}/{s['total_questions']}",
                    f"{s['eval_duration']:.0f}",
                    f"{s['kiro_duration']:.0f}",
                    str(s["program_loc"]),
                )
            self._console.print(table)
        # No plain equivalent: the plain path already prints per-iteration
        # summary lines inline, and REPORT.md holds the full table.

    def best_program(self, path) -> None:
        """Render the pointer to the best-performing program."""
        if self.fancy:
            self._console.print(f"\nBest-performing query program: [bold cyan]{escape(str(path))}[/bold cyan]")
        else:
            print(f"\nBest-performing query program: {path}", file=self.stream)

    def report_saved(self, path) -> None:
        """Render the pointer to the generated report."""
        if self.fancy:
            self._console.print(f"Run report saved to: [bold cyan]{escape(str(path))}[/bold cyan]")
        else:
            print(f"Run report saved to: {path}", file=self.stream)

    # -- query-command rendering ------------------------------------------

    @contextmanager
    def thinking(self, message: str) -> Iterator[None]:
        """Spinner shown while a single query program runs (query command)."""
        if self.fancy:
            with self._console.status(f"[cyan]{message}[/cyan]", spinner="dots"):
                yield
        else:
            print(message, file=self.stream)
            yield

    def answer(self, text: str) -> None:
        """Render a query answer. In plain mode this is bare stdout so the
        answer stays machine-parseable / pipeable."""
        if self.fancy:
            self._console.print(Panel(escape(text), title="Answer", border_style="green", box=box.ROUNDED))
        else:
            print(text, file=self.stream)

    def error(self, message: str) -> None:
        """Render an error to stderr."""
        if self.fancy:
            err_console = Console(file=sys.stderr, highlight=False)
            err_console.print(f"[red bold]Error:[/red bold] {escape(message)}")
        else:
            print(f"Error: {message}", file=sys.stderr)


def escape(text: str) -> str:
    """Escape rich markup in arbitrary text so brackets render literally."""
    if _RICH_AVAILABLE:
        from rich.markup import escape as _escape
        return _escape(text)
    return text

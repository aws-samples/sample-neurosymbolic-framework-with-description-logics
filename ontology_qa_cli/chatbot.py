"""Conversational CLI chatbot over an evolved ontology query program.

A terminal front end on top of :class:`ChatSession` (conversation logic) and
:class:`OntologyAnswerer` (loads the ontology once, answers in-process). It
loads everything a single time at startup, then holds an interactive dialogue:
follow-up questions are resolved against history before being answered.

Usage::

    uv run ontology-qa-chat FMA
    uv run ontology-qa-chat FMA --no-followups      # disable context rewriting
    uv run ontology-qa-chat FMA --plain             # plain output, no rich UI

In-chat commands: /reset (clear history), /help, /quit (or Ctrl-D).

The conversational logic lives in ``chat.py`` so the forthcoming web chatbot
reuses it unchanged — this module is only the terminal transport.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ontology_qa_cli.chat import ChatSession
from ontology_qa_cli.serve import _build_answerer
from ontology_qa_cli.ui import UI, escape

_HELP = (
    "Commands:\n"
    "  /reset   clear the conversation history\n"
    "  /help    show this help\n"
    "  /quit    exit (also Ctrl-D)\n"
)

# Error codes / substrings that indicate a Bedrock/AWS authentication problem
# rather than a bug in the query program.
_AUTH_ERROR_MARKERS = (
    "UnrecognizedClientException",
    "ExpiredToken",  # ExpiredTokenException / ExpiredToken
    "InvalidSignatureException",
    "AccessDeniedException",
    "security token included in the request is invalid",
    "NoCredentialsError",
    "Unable to locate credentials",
    "TokenRetrievalError",
    "SSOTokenLoadError",
    "The config profile",  # missing/!found profile
)

_AUTH_HELP = (
    "AWS credentials for Bedrock look missing or expired.\n"
    "The query program uses Bedrock (bedrock-runtime, us-east-1) to understand "
    "the question, so it needs valid credentials.\n"
    "Refresh them in this terminal, then relaunch the chat:\n"
    "  • aws sso login --profile <name>   (then: export AWS_PROFILE=<name>)\n"
    "  • or: ada credentials update --account <acct> --role <role> --once\n"
    "  • verify with: aws sts get-caller-identity"
)


def _is_auth_error(exc: BaseException) -> bool:
    """Heuristically detect an AWS/Bedrock authentication failure."""
    text = f"{type(exc).__name__}: {exc}"
    return any(marker in text for marker in _AUTH_ERROR_MARKERS)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Conversational CLI chatbot over an evolved ontology query program."
    )
    parser.add_argument(
        "source",
        help="Ontology name (e.g. 'FMA') or path to a query_program.py file.",
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
        "--owl-file",
        type=Path,
        default=None,
        help="OWL file to use when source is a program path (overrides config lookup).",
    )
    parser.add_argument(
        "--no-followups",
        action="store_true",
        help="Disable LLM follow-up resolution; send each message verbatim.",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="Disable the rich UI (plain text; useful for piping/logging).",
    )
    return parser.parse_args()


def _render_greeting(ui: UI, session: ChatSession) -> None:
    name = getattr(session.answerer, "onto_file", "ontology")
    if ui.fancy:
        ui.console.print(
            "[bold cyan]Ontology chatbot[/bold cyan] "
            f"[dim]— ask questions about[/dim] [bold]{escape(str(name))}[/bold]"
        )
        followups = "on" if session.resolve_followups else "off"
        ui.console.print(
            f"[dim]Follow-up resolution: {followups}. Type /help for commands, "
            "/quit to exit.[/dim]\n"
        )
    else:
        print(f"Ontology chatbot — ask questions about {name}", file=sys.stderr)
        print("Type /help for commands, /quit to exit.", file=sys.stderr)


def _render_turn(ui: UI, turn) -> None:
    """Render one assistant turn (answer + optional rewrite disclosure)."""
    if turn.was_rewritten and ui.fancy:
        ui.console.print(
            f"[dim]  (interpreted as: {escape(turn.resolved_question)})[/dim]"
        )
    elif turn.was_rewritten:
        print(f"(interpreted as: {turn.resolved_question})", file=sys.stderr)

    if turn.answer is None:
        ui.error("No answer was produced for that question.")
    else:
        ui.answer(turn.answer)


def _prompt(ui: UI) -> str:
    """Read one line of user input with a chat-style prompt."""
    if ui.fancy:
        ui.console.print("[bold green]you[/bold green] [dim]>[/dim] ", end="")
        return input()
    return input("you> ")


def run_chat(args: argparse.Namespace) -> None:
    ui = UI(plain=args.plain)

    # Load ontology + program once (this is the slow part).
    if ui.fancy:
        with ui.thinking("Loading ontology (once)..."):
            answerer = _build_answerer(args)
    else:
        print("Loading ontology (once)...", file=sys.stderr)
        answerer = _build_answerer(args)

    session = ChatSession(
        answerer=answerer,
        resolve_followups=not args.no_followups,
    )

    _render_greeting(ui, session)

    while True:
        try:
            line = _prompt(ui)
        except (EOFError, KeyboardInterrupt):
            if ui.fancy:
                ui.console.print("\n[dim]Bye.[/dim]")
            else:
                print("\nBye.", file=sys.stderr)
            break

        msg = line.strip()
        if not msg:
            continue

        # In-chat commands.
        if msg.lower() in {"/quit", "/exit", "quit", "exit"}:
            if ui.fancy:
                ui.console.print("[dim]Bye.[/dim]")
            else:
                print("Bye.", file=sys.stderr)
            break
        if msg.lower() == "/help":
            ui.print(_HELP)
            continue
        if msg.lower() == "/reset":
            session.reset()
            if ui.fancy:
                ui.console.print("[dim]Conversation history cleared.[/dim]")
            else:
                print("Conversation history cleared.", file=sys.stderr)
            continue

        # Answer the turn (spinner while the program runs).
        with ui.thinking("Thinking..."):
            try:
                turn = session.send(msg)
            except Exception as exc:  # noqa: BLE001 - surface program errors, keep chatting
                if _is_auth_error(exc):
                    ui.error(_AUTH_HELP)
                else:
                    ui.error(f"Query failed: {exc}")
                continue

        _render_turn(ui, turn)


def main() -> None:
    args = _parse_args()
    run_chat(args)


if __name__ == "__main__":
    main()

"""Unit tests for the query executor module."""
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from ontology_qa_cli.executor import extract_answer, execute_query_program, ExecutionResult


class TestExtractAnswer:
    """Tests for extract_answer function."""

    def test_single_answer_line(self):
        stdout = "Loading ontology...\nProcessing...\nAnswer: The heart has four chambers"
        assert extract_answer(stdout) == "The heart has four chambers"

    def test_multiple_answer_lines_returns_last(self):
        stdout = "Answer: First attempt\nSome debug output\nAnswer: Final answer"
        assert extract_answer(stdout) == "Final answer"

    def test_multiline_markdown_answer_captured_to_eof(self):
        """A Markdown answer spanning several lines is captured whole."""
        stdout = (
            "Loading ontology...\n"
            "Answer: The heart has:\n"
            "- left ventricle\n"
            "- right ventricle\n"
            "\n"
            "| Chamber | Side |\n"
            "| --- | --- |\n"
            "| Atrium | left |"
        )
        assert extract_answer(stdout) == (
            "The heart has:\n"
            "- left ventricle\n"
            "- right ventricle\n"
            "\n"
            "| Chamber | Side |\n"
            "| --- | --- |\n"
            "| Atrium | left |"
        )

    def test_multiline_answer_takes_last_marker(self):
        """Debug chatter before the real multi-line answer is ignored."""
        stdout = (
            "Answer: discarded early guess\n"
            "more debug\n"
            "Answer: Real answer\n"
            "- bullet one\n"
            "- bullet two"
        )
        assert extract_answer(stdout) == "Real answer\n- bullet one\n- bullet two"

    def test_multiline_answer_trailing_blank_lines_stripped(self):
        stdout = "Answer: Done\n\n\n"
        assert extract_answer(stdout) == "Done"

    def test_no_answer_line_returns_none(self):
        stdout = "Loading ontology...\nProcessing...\nDone."
        assert extract_answer(stdout) is None

    def test_empty_stdout_returns_none(self):
        assert extract_answer("") is None

    def test_answer_with_special_characters(self):
        stdout = "Answer: The liver (hepatic organ) performs 500+ functions!"
        assert extract_answer(stdout) == "The liver (hepatic organ) performs 500+ functions!"

    def test_answer_with_colon_in_text(self):
        stdout = "Answer: Parts include: left ventricle, right ventricle"
        assert extract_answer(stdout) == "Parts include: left ventricle, right ventricle"

    def test_line_with_answer_prefix_but_no_space(self):
        """Lines like 'Answer:text' without space after colon should not match."""
        stdout = "Answer:no space\nAnswer: with space"
        assert extract_answer(stdout) == "with space"

    def test_answer_empty_text(self):
        stdout = "Answer: "
        assert extract_answer(stdout) == ""

    def test_partial_match_not_at_start(self):
        """Lines where 'Answer: ' is not at the start should not match."""
        stdout = "The Answer: is here\nSome other line"
        assert extract_answer(stdout) is None


class TestExecuteQueryProgram:
    """Tests for execute_query_program function."""

    @patch("ontology_qa_cli.executor.subprocess.run")
    def test_successful_execution(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="Loading...\nAnswer: Four chambers",
            stderr="",
            returncode=0,
        )
        result = execute_query_program(
            Path("query.py"), Path("onto.owl"), "What are the parts?"
        )
        assert result.success is True
        assert result.answer == "Four chambers"
        assert result.exit_code == 0
        assert result.question == "What are the parts?"

    @patch("ontology_qa_cli.executor.subprocess.run")
    def test_nonzero_exit_code(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="Error: file not found",
            returncode=1,
        )
        result = execute_query_program(
            Path("query.py"), Path("onto.owl"), "What is X?"
        )
        assert result.success is False
        assert result.answer is None
        assert result.exit_code == 1
        assert result.stderr == "Error: file not found"

    @patch("ontology_qa_cli.executor.subprocess.run")
    def test_no_answer_format_in_stdout(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="Some output without answer format",
            stderr="",
            returncode=0,
        )
        result = execute_query_program(
            Path("query.py"), Path("onto.owl"), "What is Y?"
        )
        assert result.success is False
        assert result.answer is None
        assert result.exit_code == 0

    @patch("ontology_qa_cli.executor.subprocess.run")
    def test_timeout_handling(self, mock_run):
        exc = subprocess.TimeoutExpired(cmd=["python", "query.py"], timeout=120)
        exc.stdout = None
        mock_run.side_effect = exc
        result = execute_query_program(
            Path("query.py"), Path("onto.owl"), "Slow question", timeout=120
        )
        assert result.success is False
        assert result.answer is None
        assert result.exit_code == -1
        assert "Timeout" in result.stderr
        assert "120" in result.stderr

    @patch("ontology_qa_cli.executor.subprocess.run")
    def test_timeout_with_partial_stdout(self, mock_run):
        exc = subprocess.TimeoutExpired(cmd=["python", "query.py"], timeout=60)
        exc.stdout = "Partial output"
        mock_run.side_effect = exc
        result = execute_query_program(
            Path("query.py"), Path("onto.owl"), "Question", timeout=60
        )
        assert result.success is False
        assert result.stdout == "Partial output"
        assert "60" in result.stderr

    @patch("ontology_qa_cli.executor.subprocess.run")
    def test_subprocess_called_with_correct_args(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="Answer: test",
            stderr="",
            returncode=0,
        )
        execute_query_program(
            Path("/path/to/query.py"),
            Path("/path/to/onto.owl"),
            "My question?",
            timeout=60,
        )
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "/path/to/query.py" in cmd[1]
        assert "/path/to/onto.owl" in cmd[2]
        assert cmd[3] == "My question?"
        assert call_args[1]["timeout"] == 60
        assert call_args[1]["capture_output"] is True
        assert call_args[1]["text"] is True

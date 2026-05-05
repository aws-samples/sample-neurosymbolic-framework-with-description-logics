"""LLM-as-a-judge for ontology QA CLI."""
from dataclasses import dataclass

import boto3


@dataclass
class JudgmentResult:
    question: str
    generated_answer: str
    expected_answer: str
    assessment: str  # Paragraph-length explanation
    is_correct: bool  # Binary verdict
    stdout: str = ""  # Captured stdout from query program execution
    stderr: str = ""  # Captured stderr from query program execution


_JUDGE_SYSTEM_PROMPT = (
    "You are an expert judge comparing a generated answer against an expected answer "
    "for a question about an OWL ontology. Your job is to determine whether the "
    "generated answer is semantically correct when compared to the expected answer.\n\n"
    "Provide:\n"
    "1. A paragraph-length assessment explaining whether the generated answer is "
    "correct, partially correct, or incorrect. Consider semantic equivalence, "
    "not just exact string matching.\n"
    "2. A final verdict line in exactly this format:\n"
    "VERDICT: CORRECT\n"
    "or\n"
    "VERDICT: INCORRECT\n\n"
    "The verdict must appear on its own line at the end of your response."
)


def judge_answer(
    question: str,
    generated_answer: str,
    expected_answer: str,
    bedrock_client,
) -> JudgmentResult:
    """Compare a generated answer against the expected answer using Opus 4.6.

    Returns a JudgmentResult with a paragraph assessment and binary verdict.
    """
    user_message = (
        f"Question: {question}\n\n"
        f"Generated Answer: {generated_answer}\n\n"
        f"Expected Answer: {expected_answer}\n\n"
        "Please assess whether the generated answer is correct compared to the "
        "expected answer, then provide your verdict."
    )

    response = bedrock_client.converse(
        modelId="us.anthropic.claude-opus-4-6-v1",
        messages=[
            {
                "role": "user",
                "content": [{"text": user_message}],
            }
        ],
        system=[{"text": _JUDGE_SYSTEM_PROMPT}],
    )

    # Extract the response text
    response_text = response["output"]["message"]["content"][0]["text"]

    # Parse the verdict from the response
    assessment, is_correct = _parse_judgment_response(response_text)

    return JudgmentResult(
        question=question,
        generated_answer=generated_answer,
        expected_answer=expected_answer,
        assessment=assessment,
        is_correct=is_correct,
    )


def _parse_judgment_response(response_text: str) -> tuple[str, bool]:
    """Parse the LLM response to extract assessment and verdict.

    Returns:
        A tuple of (assessment_text, is_correct).
        If no verdict line is found, defaults to INCORRECT.
    """
    lines = response_text.strip().splitlines()
    is_correct = False
    verdict_line_index = None

    # Scan lines in reverse to find the verdict
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip().upper()
        if stripped == "VERDICT: CORRECT":
            is_correct = True
            verdict_line_index = i
            break
        elif stripped == "VERDICT: INCORRECT":
            is_correct = False
            verdict_line_index = i
            break

    # Extract assessment (everything before the verdict line)
    if verdict_line_index is not None:
        assessment = "\n".join(lines[:verdict_line_index]).strip()
    else:
        # No verdict found, use entire response as assessment, default to incorrect
        assessment = response_text.strip()

    return assessment, is_correct


def compute_accuracy(results: list[JudgmentResult]) -> float:
    """Compute the fraction of correct answers.

    Returns 0.0 if the results list is empty.
    """
    if not results:
        return 0.0
    correct_count = sum(1 for r in results if r.is_correct)
    return correct_count / len(results)

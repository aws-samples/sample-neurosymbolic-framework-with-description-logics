"""Runtime configuration for the Bedrock LLM backend.

Region and model id are read from the environment so a deployment (CDK task
definition, local shell, CI) can point the app at a different region or a
different Claude model without a code change. Both fall back to the values the
code previously hardcoded, so behavior is unchanged when the vars are unset.

Env vars:
    ONTOLOGY_QA_BEDROCK_REGION  -- AWS region for the bedrock-runtime client.
    ONTOLOGY_QA_BEDROCK_MODEL   -- Bedrock model id used for chat rewrite,
                                   judging, and the builder's LLM calls.
"""
import os

# Values the code previously hardcoded; used as defaults so an unconfigured
# environment behaves exactly as before.
DEFAULT_BEDROCK_REGION = "us-east-1"
DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-opus-4-6-v1"

_REGION_ENV = "ONTOLOGY_QA_BEDROCK_REGION"
_MODEL_ENV = "ONTOLOGY_QA_BEDROCK_MODEL"


def bedrock_region() -> str:
    """Region for the bedrock-runtime client (env-overridable)."""
    value = os.environ.get(_REGION_ENV, "").strip()
    return value or DEFAULT_BEDROCK_REGION


def bedrock_model() -> str:
    """Bedrock model id for chat/judge/builder calls (env-overridable)."""
    value = os.environ.get(_MODEL_ENV, "").strip()
    return value or DEFAULT_BEDROCK_MODEL

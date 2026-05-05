# Ontology QA CLI

A CLI tool for querying OWL DL ontologies using natural language questions. The system iteratively builds and improves a Python query program using an LLM-as-a-judge feedback loop and kiro-cli for automated code fixing.

## Prerequisites

- Python 3.10+
- AWS credentials configured for Bedrock access (Opus 4.6)
- kiro-cli (see installation below)

### Installing kiro-cli on Ubuntu

```bash
wget https://desktop-release.q.us-east-1.amazonaws.com/latest/kiro-cli.deb
sudo dpkg -i kiro-cli.deb
sudo apt-get install -f
```

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Usage

### Builder CLI

Iteratively build and improve a query program for an ontology:

```bash
source .venv/bin/activate
python -m ontology_qa_cli.builder FMA
```

Or without activating the venv:

```bash
.venv/bin/python -m ontology_qa_cli.builder FMA
```

Options:

- `--ontology-config PATH` — Path to YAML config file (default: `ontology_config.yaml`)
- `--threshold FLOAT` — Accuracy threshold to stop iterating (default: 0.8)
- `--max-iterations INT` — Maximum iterations (default: 10)
- `--max-questions INT` — Maximum number of questions per evaluation round (random sample; uses all if not specified)
- `--kiro-timeout INT` — Timeout in seconds for kiro-cli invocations (default: 600)
- `--output-dir PATH` — Base directory for run outputs (default: `runs/`)

### Query CLI

Run a previously generated query program against a single question:

```bash
python -m ontology_qa_cli.query path/to/query_program.py "What are the parts of the heart?" --ontology FMA
```

Options:

- `--ontology NAME` — Ontology name to resolve the OWL file path
- `--config PATH` — Path to YAML config file (default: `ontology_config.yaml`)

## Configuration

Define ontologies and test questions in `ontology_config.yaml`:

```yaml
ontologies:
  - name: "FMA"
    owl_file: "~/fma.owl"
    questions:
      - question: "What are the parts of the heart?"
        expected_answer: "The parts of the heart include the left ventricle, right ventricle, left atrium, right atrium, and various valves."
```

Paths support `~/` expansion.

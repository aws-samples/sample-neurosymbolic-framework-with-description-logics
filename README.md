# Ontology QA CLI

## Introduction

This project is a showcase of the ability of a neurosymbolic generative AI system to accurately answer questions based on biomedical ontologies. Rather than relying solely on an LLM to generate answers (which risks hallucination), the system uses a hybrid approach: an LLM decomposes natural language questions into structured queries, and then symbolic reasoning via owlready2 traverses the ontology graph to produce grounded, verifiable answers.

The system uses a self-modifying program guided by a collection of gold-standard question-answer pairs. We start with a degenerate seed program (that always says "I don't know") and the system iteratively fixes and improves itself — evaluating its answers against the gold standard, identifying failures, and rewriting its own code to do better next time.

We demonstrate this system with working code and with three different real-world ontologies:

- **[FMA](https://bioportal.bioontology.org/ontologies/FMA)** (Foundational Model of Anatomy) — a comprehensive ontology of human anatomy
- **[Gene Ontology](https://geneontology.org) (GO)** — the world's largest source of information on the functions of genes
- **[SNOMED CT](https://www.snomed.org)** — a systematically organized clinical terminology covering diseases, findings, procedures, and more

The system iteratively improves its query program through an automated feedback loop: it evaluates answers against known Q&A pairs using an LLM-as-a-judge, then uses kiro-cli to fix the code based on the feedback. Each iteration produces a better program that more accurately answers questions using the target ontology.

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
- `--num-failures-to-fix INT` — Number of failed questions to include in each fix round (default: all failures)
- `--kiro-timeout INT` — Timeout in seconds for kiro-cli invocations (default: no timeout)
- `--output-dir PATH` — Base directory for run outputs (default: `runs/`)

### Query CLI

Ask a question using the best program from the most recent builder run:

```bash
python -m ontology_qa_cli.query FMA "What are the parts of the heart?"
```

Or specify an explicit program path:

```bash
python -m ontology_qa_cli.query runs/FMA_20260506_172902/iteration_9/query_program.py "What are the parts of the heart?"
```

The first argument is either an ontology name (looks up the best program from the latest run) or a path to a `query_program.py` file.

Options:

- `--config PATH` — Path to YAML config file (default: `ontology_config.yaml`)
- `--runs-dir PATH` — Base directory where builder runs are stored (default: `runs/`)

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

# Building accurate and trustworthy GenAI applications, based on description logic ontologies

## Introduction

This project is a showcase of the ability of a neurosymbolic generative AI system to accurately answer questions based on biomedical ontologies. Rather than relying solely on an LLM to generate answers (which risks hallucination), the system uses a hybrid approach: an LLM decomposes natural language questions into structured queries, and then symbolic reasoning via owlready2 traverses the ontology graph to produce grounded, verifiable answers.

The system uses a self-modifying program guided by a collection of gold-standard question-answer pairs. We start with a degenerate seed program (that always says "I don't know") and the system iteratively fixes and improves itself — evaluating its answers against the gold standard, identifying failures, and rewriting its own code to do better next time.

We demonstrate this system with working code and with three different real-world ontologies:

- **[FMA](https://bioportal.bioontology.org/ontologies/FMA)** (Foundational Model of Anatomy) — a comprehensive ontology of human anatomy
- **[Gene Ontology](https://geneontology.org) (GO)** — the world's largest source of information on the functions of genes
- **[SNOMED CT](https://www.snomed.org)** — a systematically organized clinical terminology covering diseases, findings, procedures, and more

The system iteratively improves its query program through an automated feedback loop: it evaluates answers against known Q&A pairs using an LLM-as-a-judge, then uses kiro-cli to fix the code based on the feedback. Each iteration produces a better program that more accurately answers questions using the target ontology.

## Description logics

This project leverages ontologies that are available as an OWL description logic.

[Description logics](https://en.wikipedia.org/wiki/Description_logic) are a
family of logic-based knowledge representation languages that can be used to
represent the terminological knowledge of an application domain in a
structured way.

The name _description logics_ is motivated by the fact that,
on the one hand, the important notions of the domain are described by concept descriptions,
i.e., expressions that are built from atomic concepts (unary predicates) and atomic roles
(binary predicates) using the concept and role constructors provided by the particular logic.

The main reason for using description logics rather than general first-order predicate logic
when representing knowledge is that most DLs are actually decidable fragments of first-order
predicate logic, i.e., there are effective procedures for doing inference.

Unlike propositional or predicate logics, description logics do not have a declarative query
language (like [SQL](https://en.wikipedia.org/wiki/SQL), or [SMT-LIB](https://smt-lib.org)).
Rather, description logics are imperatively queried by writing a Python program that uses an
API to traverse the ontology. This means that querying a description logic ontology in an agentic
system requires us to write and critique a _program_, not just a declarative query (such as a
`SQL SELECT`).

## Ontology statistics

The ontologies used here are not toy ontologies, these are used in biomedical research. The following
table shows some descriptive statistics for these three ontologies:

<!-- Regenerate with: python -m ontology_qa_cli.ontology_stats -->

| Ontology | File Size | Lines | Classes | Object Properties | Data Properties | Individuals | Axioms (Restrictions) |
|----------|-----------|-------|---------|-------------------|-----------------|-------------|----------------------|
| [FMA](https://bioportal.bioontology.org/ontologies/FMA) | 254 MB | 3,877,972 | 104,721 | 139 | 29 | 2 | 157,831 |
| [Gene Ontology](https://geneontology.org) (GO) | 226 MB | 3,902,991 | 84,523 | 319 | 0 | 48 | 105,495 |
| [SNOMED CT](https://www.snomed.org) | 202 MB | 2,977,123 | 796,512 | 0 | — | — | 1,069,097 |

## Downloading the Ontologies

### FMA (Foundational Model of Anatomy)

Download from BioPortal (requires free account):

```bash
# Visit https://bioportal.bioontology.org/ontologies/FMA and download the OWL file
# Or use the BioPortal REST API:
curl -L -o ~/fma.owl "https://data.bioontology.org/ontologies/FMA/download?apikey=YOUR_API_KEY&download_format=rdf"
```

### Gene Ontology (GO)

Download directly from the Gene Ontology consortium:

```bash
curl -L -o ~/go-plus.owl "http://purl.obolibrary.org/obo/go/extensions/go-plus.owl"
```

### SNOMED CT

SNOMED CT requires a license. Download from the [SNOMED International Member Licensing portal](https://mlds.ihtsdotools.org/) or the [NLM UMLS](https://www.nlm.nih.gov/research/umls/licensedcontent/snomedctarchive.html) (US edition, requires free UMLS account).

Once you have the RF2 release zip, convert it to OWL using the SNOMED OWL Toolkit:

```bash
# Download the toolkit
wget https://github.com/IHTSDO/snomed-owl-toolkit/releases/download/5.3.0/snomed-owl-toolkit-5.3.0-executable.jar

# Convert RF2 to OWL (requires Java 17+, needs ~16GB RAM for temp files)
java -Djava.io.tmpdir=$HOME/tmp_extract -jar snomed-owl-toolkit-5.3.0-executable.jar \
  -rf2-to-owl -rf2-snapshot-archives SnomedCT_*.zip

# Then build the SQLite index for fast querying
python build_db.py ~/ontology-*.owl ~/snomed.db
```

## Prerequisites

- Python 3.10+
- AWS credentials configured for Bedrock access (Opus 4.6)
- kiro-cli (see installation below)
- Java 17 (if you want to use SNOMED)

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

### Ontology Stats CLI

Generate a markdown table with descriptive statistics for all ontologies in the config:

```bash
python -m ontology_qa_cli.ontology_stats
```

This outputs a markdown table to stdout showing file size, line count, number of classes, properties, individuals, and axioms for each ontology. You can redirect it to a file or pipe it:

```bash
python -m ontology_qa_cli.ontology_stats > ontology_stats.md
```

To use a custom config file:

```bash
python -m ontology_qa_cli.ontology_stats path/to/ontology_config.yaml
```

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

## A Sample Run
We can see here the [output from a run](blob/main/assets/sample-run) of the Builder CLI.
For example, you can see the [REPORT.md](blob/main/assets/sample-run/REPORT.md) that summarizes
the run.

Here is a test: [self](README.md)
[LLM costs](llm-costs.yaml)
[assets](assets)

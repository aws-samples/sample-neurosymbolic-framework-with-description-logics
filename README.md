# Building accurate and trustworthy GenAI applications, based on description logic ontologies

One roadblock to the broad adoption of agentic systems (for example, chatbots or
research agents) is their tendency to hallucinate and to reason incorrectly.
This is particularly acute in regulated domains like healthcare or finance where
inaccurate responses are costly. We show here how to build agentic systems whose
responses are _provably correct_.

This work shows how to build hybrid systems that combine a large language model
(LLM) with a description logic reasoner. It starts by downloading an ontology
that is available in OWL 2 DL format (a `.owl` file). It then builds a
description logic reasoner to answer questions about that ontology. It does
this by, first, creating a program that can interrogate the chosen ontology,
and, second, using that program to answer questions about the subject matter
of the ontology.

To be concrete, this work shows how to build an agentic system that can
provide *provably correct* answers to questions like this:

**Question:** What are the parts of the heart?

**Answer:** The human heart is a four-chambered muscular organ comprising the
          right/left atria (upper chambers) and right/left ventricles (lower
          chambers). It includes four valves (tricuspid, pulmonary, mitral, 
          aortic) to control flow, the septum dividing the sides, and major
          vessels like the vena cava and aorta to pump blood.

**Question:** Does TP53 interact with DNA?

**Answer:** Yes, it has DNA-binding transcription factor activity.

**Question:** What are all the specific sub-types and variations of Pneumonia?

**Answer:** Pneumonia is classified by its cause, where it was acquired, and which
          part of the lung it affects. The main types are bacterial, viral,
          fungal, and aspiration pneumonia.

The framework described herein builds agentic systems that answers questions
like these _accurately_. It does so by switching out the LLM reasoner (which 
is faulty) with an ontological reasoner (which is provably correct).

## Background 
According to Accenture[^1], companies with AI-led processes achieve 2.5x higher
revenue growth, 2.4x greater productivity, and 3.3x greater success at scaling
generative AI use cases. Also, McKinsey research[^2] indicates that generative
AI has the potential to automate work activities that absorb 60 to 70 percent
of employees' time today. ChatGPT Enterprise users report 40-60 minutes of saved
time per day thanks to AI use[^3]. Data science, engineering and communications
workers save more than average at 60-80 minutes per day. 

## The problem 
In domains like healthcare, accuracy is critical. For example, if a clinical
application says that “Patient X’s most recent blood pressure was 300/80” and
this is not true, then providers and patients will rightly not trust it and,
equally rightly, will not use it. Worse, patients could be harmed. 

Unfortunately, state-of-the-art large language models (LLMs) and large
multimodal models (LMMs), while showing a masterful display of the syntax
of natural (and formal) languages, still hallucinate and reason incorrectly. Despite
the industry’s reliance on the now-discredited scaling laws[^4], and even
though today’s models are more powerful and capable than models of even 6
months ago, the latest models still consistently hallucinate (and, on some
metrics, they hallucinate worse[^5] than older models).

For us to trust Generative AI chatbots, we must trust that these applications
are accurate. The industry’s current reliance on scaling laws (i.e., using the
same technology but using more parameters, training for longer, and on more data)
has not yet provided us a reliable path towards trustworthy systems with fewer
hallucinations and reasoning errors. 

We can see this problem across industries. An MIT study[^6], for example, found
that “95% of generative AI pilots fail”. According to Cap Gemini[^7], “trust in
fully autonomous AI agents has dropped sharply, from 43% to 27% in [the year
ending mid-2025]”. 

These concerns are heightened in regulated industries such as healthcare, 
life sciences, or finance because the cost of an error is higher than in
many other domains.

## Introduction

This project describes a framework for building neurosymbolic AI
systems that can accurately answer questions based on (biomedical) ontologies.
Rather than relying solely on an LLM to generate answers (which risks
hallucinations), the system uses a hybrid approach: 1) an LLM inference
decomposes a natural language question into an internal representation; 2) 
a symbolic reasoner (created here) traverses the ontology graph to produce a
grounded, verifiable answer (in another internal representation); and 3) an 
LLM inference converts this answer into natural language.

We here demonstrate this system with working code and with five different
real-world ontologies:

- **[FMA](https://bioportal.bioontology.org/ontologies/FMA)** (Foundational Model
of Anatomy) — a comprehensive ontology of human anatomy
- **[Gene Ontology](https://geneontology.org) (GO)** — is the world's largest source
of information on the functions of genes
- **[SNOMED CT](https://www.snomed.org)** — is a systematically organized clinical
terminology covering diseases, findings, procedures, and more
- **[Cell Ontology (CL)](http://obofoundry.org/ontology/cl.html)** - is "an ontology 
designed to classify and describe cell types across different organisms. It serves
as a resource for model organism and bioinformatics databases. The ontology covers
a broad range of cell types in animal cells, with over 2700 cell type classes, and
provides high-level cell type classes as mapping points for cell type classes in
ontologies representing other species"
- **[Human phenotype ontology (HPO)](https://hpo.jax.org)** "provides a standardized
vocabulary of phenotypic abnormalities encountered in human disease."

This framework has two main components:

1. An offline component (the **[builder](ontology_qa_cli/builder.py))** whose input
is an ontology file and a set of question-answer pairs. The builder's output
is a Python program that converts a natural language question into a natural
language answer (via a traversal of the ontology).

2. An online question-answering component (the **[questioner](ontology_qa_cli/query.py)**)
that uses the Python program created by the builder to answer a question.

The builder uses a self-modifying program guided by a collection of gold-standard
question-answer pairs: 

![Self-modifying program architecture](SMP.svg)

We start with a deliberately degenerate
[seed program](ontology_qa_cli/seed_program.py)
(that always says "I don't know") and then the system iteratively fixes and improves
itself — evaluating its answers against the gold standard, identifying 
failures, and rewriting its own code to improve its performance against the
gold standard test cases.

The builder has the following major components:

1. The **general guidelines** is a markdown file that lists instructions for the
system. For example, we can instruct it to only use the LLM to parse the
question and to convert the answer back into natural language, and to **not**
use the LLM to answer the question.

2. The **test cases** is a set of manually curated gold-standard 
[question-answer pairs](ontology_config.yaml).

3. The **control harness** controls the evolution of the
[seed program](ontology_qa_cli/seed_program.py) into a more effective
program. It follows the following steps:

   3.1. It executes the current program against the test set.

   3.2. It uses the LLM-as-a-judge pattern to evaluate the correctness of each
   generated answer compared to each expected answer.

   3.3. It generates a file TASK.md that includes the general guidlines along
   with the LLM-as-a-judge critiques for each answer.

   3.4. It uses [kiro-cli](https://kiro.dev/cli/) to fix the current program,
   using TASK.md, and creates a new (hopefully better) program.

   3.5. If the new program is not good enough, go back to step 3.1.

4. The **program being evolved** is a Python program. It starts as the 
[seed program](ontology_qa_cli/seed_program.py) and then the control harness,
along with
[kiro-cli](https://kiro.dev/cli/), iteratively evolves it into a program
that performs increasingly well on the test cases.

The control harness has no knowledge of the particular ontology being trained
against: all it has is the [question-answer pairs](ontology_config.yaml) and
the general guidelines. It is the responsibility of `kiro-cli` to interrogate
the given ontology file so as to understand how to query it.

The system iteratively improves its query program through an automated feedback
loop: it evaluates answers against known Q&A pairs using LLM-as-a-judge,
then uses [kiro-cli](https://kiro.dev/cli/) to fix the code based on the
feedback. Each iteration produces a (hopefully) better program that more
accurately answers the [gold-standard questions]((ontology_config.yaml))
using the target ontology.

The [architecture diagram](docs/architecture.md) shows how the source code
maps onto this functional architecture.

## Description logics

This project builds on ontologies that are available as an OWL description logic.

[Description logics](https://en.wikipedia.org/wiki/Description_logic) are a
family of logic-based knowledge representation languages that can be used to
represent the terminological knowledge of an application domain in a
structured way.

The name _description logics_ is motivated by the fact that, on the one hand,
the important notions of the domain are described by concept descriptions,
i.e., expressions that are built from atomic concepts (unary predicates) and
atomic roles (binary predicates) using the concept and role constructors
provided by the particular logic.

The main reason for using description logics rather than general first-order
predicate logic when representing knowledge is that most DLs are actually
decidable fragments of first-order predicate logic, i.e., there are effective
procedures for doing inference.

Description logics are commonly used to represent biomedical ontologies, such
as [SNOMED CT](https://www.snomed.org) or the [Gene Ontology](https://geneontology.org)
(GO).

Unlike propositional or predicate logics, description logics do not have a
declarative query language (like [SQL](https://en.wikipedia.org/wiki/SQL),
or [SMT-LIB](https://smt-lib.org)). Rather, description logics are imperatively
queried by writing a Python program that uses an API to traverse the ontology.
This means that querying a description logic ontology in an agentic system
requires the writing and critiquing a _program_, not just a declarative query
(such as a `SQL SELECT`).

## Ontology statistics

The ontologies used here are not toy ontologies, they are actively used in
biomedical research. The following table shows some descriptive statistics
for these ontologies:

<!-- Regenerate with: python -m ontology_qa_cli.ontology_stats -->

| Ontology | File Size | Lines | Classes | Object Properties | Data Properties | Individuals | Axioms (Restrictions) |
|----------|-----------|-------|---------|-------------------|-----------------|-------------|----------------------|
| [FMA](https://bioportal.bioontology.org/ontologies/FMA) | 254 MB | 3,877,972 | 104,721 | 139 | 29 | 2 | 157,831 |
| [Gene Ontology](https://geneontology.org) (GO) | 226 MB | 3,902,991 | 84,523 | 319 | 0 | 48 | 105,495 |
| [SNOMED CT](https://www.snomed.org) | 202 MB | 2,977,123 | 796,512 | 0 | — | — | 1,069,097 |
| [Cell Ontology (CL)](http://obofoundry.org/ontology/cl.html) | 63 MB | 1,011,425 | 386,110 | — | — | — | 1,105,244 |
| [Human phenotype ontology (HPO)](https://hpo.jax.org) | 81 MB | 1,358,876 | 32,085 | 235 | 0 | 0 | 20,599 |

## Prerequisites

- Ubuntu 26.04 LTS
- Python 3.10+
- AWS credentials configured for Bedrock access (Opus 4.6)
- kiro-cli (see installation below)
- Java 17 (if you want to use SNOMED)

### Installing kiro-cli on Ubuntu

```bash
curl -fsSL https://cli.kiro.dev/install | bash
```

## Install Python 3.13

```
sudo apt update
sudo apt install software-properties-common -y
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install python3.13 python3.13-venv python3.13-dev
python3 --version
```

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```


## Usage

### Per-session setup

To run a CLI program you need to first activate your environment:

```
source .venv/bin/activate
```

Then authenticate with `kiro-cli` (`kiro login`) and then authenticate with AWS,
for example:

```
export AWS_ACCESS_KEY_ID=[...]
export AWS_SECRET_ACCESS_KEY=[...]
export AWS_SESSION_TOKEN=[...]
```

### Builder CLI

Iteratively build and improve a query program for an ontology:

```bash
source .venv/bin/activate
python -m ontology_qa_cli.builder FMA
```

Or without activating the venv:

```bash
kiro login # only the first time in a session
.venv/bin/python -m ontology_qa_cli.builder FMA
```

Options:

- `--ontology-config PATH` — Path to YAML config file (default: `ontology_config.yaml`)
- `--threshold FLOAT` — Accuracy threshold to stop iterating (default: 0.8)
- `--max-iterations INT` — Maximum iterations (default: 20)
- `--max-questions INT` — Maximum number of questions per evaluation round (random sample; uses all if not specified)
- `--num-failures-to-fix INT` — Number of failed questions to include in each fix round (default: all failures)
- `--kiro-timeout INT` — Timeout in seconds for kiro-cli invocations (default: no timeout)
- `--output-dir PATH` — Base directory for run outputs (default: `runs/`)
- `--llm-costs PATH` — Path to LLM cost configuration file (default: `llm-costs.yaml`)

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

## Downloading the ontologies

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

### Cell ontology (CL)

Download the file `cl.owl` from `http://obofoundry.org/ontology/cl.html`.

### Human phenotype ontology (HPO)

Download `hp-full.owl` from `https://github.com/obophenotype/human-phenotype-ontology/releases/download/v2026-02-16/hp-full.owl`.

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

We can see here the [output from a run](assets/sample-run) of the Builder CLI.
For example, you can see the [REPORT.md](assets/sample-run/REPORT.md) that summarizes
the run.

## Conclusions

We have shown here that AI coding tools (such as
[kiro-cli](https://kiro.dev/cli/)) are surprisingly effective at evolving programs
that statisfy a set of constraints. We use this ability to build *provably correct*
agentic systems that are based ontologies, especially biomedical ontologies.

## Future work

1. In the current implementation, the program being evaluated is run in the same
environment as the evaluator. Using a separate environment (e.g., a container)
would limit the blast radius of an errant program.

2. The current implementation assumes that the ontologies and the
question-answer pairs are tightly controlled. If this assumption is relaxed then the attack
surface will be larger and this should be done with care.


## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.


[^1]: https://www.accenture.com/us-en/insights/strategic-managed-services/reinvent-operations-with-genai?c=acn_glb_aipoweredoperatmediarelations_14200178&n=mrl_0924
[^2]: https://www.mckinsey.com/capabilities/tech-and-ai/our-insights/the-economic-potential-of-generative-ai-the-next-productivity-frontier
[^3]: https://www.exponentialview.co/p/2025-in-25-stats
[^4]: https://arxiv.org/pdf/2001.08361
[^5]: https://www.nytimes.com/2025/05/05/technology/ai-hallucinations-chatgpt-google.html
[^6]: https://www.ciodive.com/news/why-enterprise-ai-pilots-fail/808751/
[^7]: https://www.capgemini.com/us-en/news/press-releases/trust-and-human-ai-collaboration-set-to-define-the-next-era-of-agentic-ai-unlocking-450-billion-opportunity-by-2028/
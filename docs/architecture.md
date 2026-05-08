# Functional Architecture Diagram

```mermaid
graph TB
    subgraph "User Interface"
        CLI_BUILD["Builder CLI<br/><code>builder.py</code>"]
        CLI_QUERY["Query CLI<br/><code>query.py</code>"]
    end

    subgraph "Configuration"
        CONFIG["Config Loader<br/><code>config.py</code>"]
        YAML["ontology_config.yaml<br/>(ontology names, OWL paths,<br/>Q&A pairs)"]
    end

    subgraph "Builder Loop"
        SEED["Seed Generator<br/><code>seed.py</code> → <code>seed_program.py</code>"]
        EXECUTOR["Query Executor<br/><code>executor.py</code>"]
        JUDGE["LLM Judge<br/><code>judge.py</code>"]
        FIXER["Program Fixer<br/><code>fixer.py</code>"]
        RUNMGR["Run Manager<br/><code>run_manager.py</code>"]
    end

    subgraph "External Services"
        BEDROCK["AWS Bedrock<br/>(Opus 4.6)"]
        KIRO["kiro-cli<br/>(code fixing)"]
        OWL["OWL Ontology<br/>(.owl + .sqlite3 cache)"]
    end

    subgraph "Generated Artifacts"
        QPROG["query_program.py<br/>(evolving program)"]
        RUNS["runs/<br/>├── _seed/<br/>├── iteration_1/<br/>│   ├── query_program.py<br/>│   ├── evaluation.json<br/>│   ├── TASK.md<br/>│   └── changes.patch<br/>└── summary.json"]
    end

    %% Builder flow
    CLI_BUILD --> CONFIG
    CONFIG --> YAML
    CLI_BUILD --> SEED
    SEED --> QPROG

    CLI_BUILD --> EXECUTOR
    EXECUTOR -->|"subprocess<br/>(parallel)"| QPROG
    QPROG -->|"owlready2"| OWL
    QPROG -->|"decompose question"| BEDROCK

    EXECUTOR -->|"answers"| JUDGE
    JUDGE -->|"compare answers"| BEDROCK
    JUDGE -->|"verdicts"| CLI_BUILD

    CLI_BUILD -->|"failed questions"| FIXER
    FIXER -->|"TASK.md + program"| KIRO
    KIRO -->|"fixed program"| FIXER
    FIXER -->|"updated source"| QPROG

    CLI_BUILD --> RUNMGR
    RUNMGR --> RUNS

    %% Query flow
    CLI_QUERY --> CONFIG
    CLI_QUERY -->|"finds best program"| RUNS
    CLI_QUERY --> EXECUTOR
    EXECUTOR --> QPROG
```

## Data Flow Summary

### Builder CLI (`python -m ontology_qa_cli.builder <ontology>`)

1. **Load config** → reads `ontology_config.yaml` for ontology metadata and Q&A pairs
2. **Generate seed** → copies `seed_program.py` as the initial `query_program.py`
3. **Evaluation loop** (parallel per question):
   - **Execute** → runs `query_program.py` as subprocess with ontology file + question
   - **Judge** → sends (generated answer, expected answer) to Opus 4.6 for verdict
4. **Fix** → generates `TASK.md` with failures, invokes `kiro-cli` to modify the program
5. **Persist** → saves program, evaluation, patch, and kiro output per iteration
6. **Repeat** until accuracy threshold met or max iterations reached

### Query CLI (`python -m ontology_qa_cli.query <source> <question>`)

1. **Resolve source** → either ontology name (finds best program from latest run) or explicit path
2. **Execute** → runs the resolved `query_program.py` with the question
3. **Output** → prints the answer to stdout

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Subprocess isolation | Buggy query programs can't crash the CLI |
| LLM for question understanding only | Answers come from owlready2 graph traversal, not hallucination |
| SQLite caching | Avoids re-parsing large OWL files on every invocation |
| Parallel evaluation | Questions run concurrently; output printed sequentially |
| kiro-cli via PTY | Streams output in real-time with line buffering |
| Patch files per iteration | Easy to see exactly what changed |

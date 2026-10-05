# Mapwright

Mapwright onboards messy legacy data exports into a clean, validated
target schema. Deterministic code handles structural problems; an LLM
handles the problems that need an understanding of meaning; a bounded
repair loop fixes transformations that fail validation; and anything risky
waits for human approval.

```
Analyze → Propose → Transform → Validate → Revise if needed → Human approval → Apply → Audit
```

Every claim about how well it works comes from a benchmark of 12 messy
datasets with exact ground truth, comparing three systems on the same
files: rules only, LLM only, and the hybrid.

Runs entirely on your own machine. No cloud services.

## Status

Architecture, target contracts, benchmark and evaluation criteria were
fixed before any pipeline code. The benchmark now exists: 11 generated
cases with exact ground truth, held-out cases frozen.

| Step | Status |
| --- | --- |
| Target contracts (`contracts/`) | Done |
| Issue taxonomy (`benchmark/issue_types.yaml`) | Done |
| Benchmark and evaluation design (`docs/`) | Done |
| Benchmark generator, 11 cases, held-out set frozen | Done |
| H04 from a real public dataset | |
| Scorer, with its own tests and reference systems | Done |
| Rules-only baseline | Next |
| LLM semantic layer and repair loop | |
| Comparison: rules vs LLM vs hybrid, failure analysis | |
| Review UI and migration package | |

## Getting started

Requires Python 3.11 or newer.

```
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate        # macOS / Linux
pip install -e ".[dev]"

pytest                                     # run the tests
python -m benchmark.generator verify       # check the benchmark files are intact
python -m evaluation baseline perfect runs/perfect
python -m evaluation score runs/perfect    # every metric at its best value
```

## Documents

- [Architecture](docs/ARCHITECTURE.md): pipeline, what is code and what is LLM, actions, validation, approval policy
- [Benchmark](docs/BENCHMARK.md): target schema, the 12 cases, ground truth format, held-out rules
- [Evaluation](docs/EVALUATION.md): systems compared, hypotheses, metrics, protocol

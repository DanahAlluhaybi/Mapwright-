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

Design phase. Architecture, target contracts, benchmark and evaluation
criteria are fixed before any pipeline code is written.

| Step | Status |
| --- | --- |
| Target contracts (`contracts/`) | Done |
| Issue taxonomy (`benchmark/issue_types.yaml`) | Done |
| Benchmark and evaluation design (`docs/`) | Done |
| Benchmark generator and 12 cases, held-out set frozen | Next |
| Scorer, with its own tests | |
| Rules-only baseline | |
| LLM semantic layer and repair loop | |
| Comparison: rules vs LLM vs hybrid, failure analysis | |
| Review UI and migration package | |

## Documents

- [Architecture](docs/ARCHITECTURE.md): pipeline, what is code and what is LLM, actions, validation, approval policy
- [Benchmark](docs/BENCHMARK.md): target schema, the 12 cases, ground truth format, held-out rules
- [Evaluation](docs/EVALUATION.md): systems compared, hypotheses, metrics, protocol

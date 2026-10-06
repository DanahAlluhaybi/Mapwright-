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
cases and one real export (H04), all with exact ground truth, held-out
cases frozen.

| Step | Status |
| --- | --- |
| Target contracts (`contracts/`) | Done |
| Issue taxonomy (`benchmark/issue_types.yaml`) | Done |
| Benchmark and evaluation design (`docs/`) | Done |
| Benchmark generator, 11 cases, held-out set frozen | Done |
| H04 from a real public export (Companies House) | Done |
| Scorer, with its own tests and reference systems | Done |
| Rules-only baseline (system R) | Done |
| LLM semantic layer and repair loop | Next |
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

python -m evaluation baseline rules runs/rules --split dev   # the rules-only system
python -m evaluation score runs/rules
```

Onboarding a single file:

```
python -m mapwright run benchmark/datasets/D06/source.csv --contract contracts/sales_order.yaml \
    --reference customer_id=benchmark/reference_customers.csv --out runs/demo
python -m mapwright replay runs/demo/plan.json benchmark/datasets/D06/source.csv \
    --contract contracts/sales_order.yaml --out runs/demo/replayed.csv
```

The run writes `target.csv`, `result.json`, `plan.json` and `report.md`.
`report.md` lists what was fixed, what was applied but needs sign-off, and
what is held for approval.

## First results: rules only

System R, scored once per version on the held-out cases. Version 1.1
fixes one ingest bug found while building H04 (header text was trimmed,
so `" CompanyNumber"` no longer matched its own column); it does not
change any score on H01–H03.

| Metric | Development | Held-out (H01–H04) |
| --- | --- | --- |
| Dirty-cell accuracy | 0.998 | 0.369 |
| Clean-cell preservation | 1.000 | 0.433 |
| Silent errors per 1,000 cells | 0.0 | 36.5 |
| Mapping F1 | 1.00 | 0.66 |
| Missed reviews | 0 | 9 |

The rule dictionaries were written from development cases, so the
development numbers say little on their own. The held-out drop is the
point: rules fail on headers and spellings nobody listed in advance, and
when a column is mapped wrongly the damage passes contract checks. On the
real export (H04) the rules mapped the country, town, date and status
columns but not the company number, name or category, so every row lost
its key. That is the gap the LLM layer has to close.

## Documents

- [Architecture](docs/ARCHITECTURE.md): pipeline, what is code and what is LLM, actions, validation, approval policy
- [Benchmark](docs/BENCHMARK.md): target schema, the 12 cases, ground truth format, held-out rules
- [Evaluation](docs/EVALUATION.md): systems compared, hypotheses, metrics, protocol

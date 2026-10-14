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
fixed before any pipeline code, and the hypotheses were written down
before any results. The benchmark has 11 generated cases and one real
export (H04), all with exact ground truth, held-out cases frozen. All four
systems have been run and scored; see [Results](#results).

| Step | Status |
| --- | --- |
| Target contracts (`contracts/`) | Done |
| Issue taxonomy (`benchmark/issue_types.yaml`) | Done |
| Benchmark and evaluation design (`docs/`) | Done |
| Benchmark generator, 11 cases, held-out set frozen | Done |
| H04 from a real public export (Companies House) | Done |
| Scorer, with its own tests and reference systems | Done |
| Rules-only baseline (system R) | Done |
| LLM layer: Gemini provider, cache, mapping, value actions, repair loop | Done |
| Comparison: rules vs LLM vs hybrid, hypothesis verdicts | Done |
| Local API and n8n review workflow (`docs/N8N.md`) | Done |
| Migration package | |

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

Checking the Gemini key (the LLM systems need one; scoring a recorded run
does not):

```
$env:GEMINI_API_KEY = "your-key"     # PowerShell, this terminal only
python -m mapwright llm-check
```

Running the LLM systems (L, H, H−). `--record` calls Gemini for anything
not yet in `llm_cache/` and saves the answers; without it, runs replay the
cache and need no key:

```
python -m evaluation baseline llm runs/llm --split dev --record
python -m evaluation baseline hybrid runs/hybrid --split dev --record
python -m evaluation baseline hybrid-norepair runs/hybrid-norepair --split dev
python -m evaluation score runs/hybrid
```

With a Gemini key set, the same command can use the model for what the
rules leave (`--system hybrid`) or for everything (`--system llm`):

```
python -m mapwright run examples/messy_customers.csv --contract contracts/customer.yaml --out runs/example-hybrid --system hybrid
```

The run writes `target.csv`, `result.json`, `plan.json`, `report.md` and `audit.jsonl`.
`report.md` lists what was fixed, what was applied but needs sign-off, and
what is held for approval.

## Running it from n8n

`python -m mapwright serve` starts a small local API. The n8n workflow in
`n8n/mapwright-review.json` puts a form in front of it: upload a CSV, see
what was held back, approve or reject, and the decision is written to the
run's audit log with the reviewer's name. Setup and the API reference are
in [docs/N8N.md](docs/N8N.md).

## Results

Four systems on the same files, scored against exact ground truth:
**R** rules only, **L** the model only, **H** rules first and the model for
what they leave, **H−** the hybrid without the repair loop. Model:
`gemini-3.1-flash-lite`, temperature 0, one run per case. The held-out
cases (H01–H04, H04 a real Companies House export) were not looked at
while building the rules or the prompts.

### Held-out

| Metric | R | L | H | H− |
| --- | --- | --- | --- | --- |
| Dirty-cell accuracy | 0.369 | 0.109 | **0.503** | 0.503 |
| Clean-cell preservation | 0.433 | 0.978 | **0.989** | 0.989 |
| Silent errors per 1,000 cells | 36.5 | 50.8 | **15.3** | 15.3 |
| Mapping F1 | 0.66 | 0.91 | **0.99** | 0.99 |
| Detection F1 | 0.50 | 0.65 | **0.90** | 0.90 |
| Missed reviews | 9 | 11 | **2** | 2 |
| Contract pass rate | 0.57 | 0.67 | **0.82** | 0.82 |
| Model calls per case | 0 | 2.0 | 2.5 | 2.0 |

On the development cases R and H both reach 0.998 dirty-cell accuracy
with zero silent errors, and H makes no model calls: the rules already
cover everything, so the model is only asked about what they leave. L
alone reaches 0.227 there, with 10.0 silent errors per 1,000 cells.

A silent error is a wrong value that passes every contract check and is
not flagged for review. It is the number that matters most in a migration,
because nobody looks at those cells again.

### Hypotheses

Stated in [docs/EVALUATION.md](docs/EVALUATION.md) before any results.

| # | Hypothesis | Verdict |
| --- | --- | --- |
| 1 | R matches or beats L on structural issues | **Supported.** Structural detection F1: dev 1.00 vs 0.58, held-out 0.35 vs 0.33 |
| 2 | L beats R on semantic issues and non-English headers, more so on held-out | **Partly.** Semantic F1 on held-out 0.79 vs 0.58, but no gap on dev (0.99 vs 1.00). Transliterated headers were mapped correctly but labelled as plain synonyms (M1), so M2 detection is 0 for both |
| 3 | H is at least as accurate as R and L | **Supported.** Held-out dirty-cell accuracy 0.503 vs 0.369 and 0.109; equal to R on dev |
| 4 | H has fewer silent errors than L | **Supported.** 15.3 vs 50.8 per 1,000 cells on held-out, 0 vs 10.0 on dev |
| 5 | The repair loop reduces failures without adding silent errors | **Inconclusive.** H and H− score the same: almost no model action failed its checks, so the loop rarely ran |

### What limits the hybrid

Most of the gap between 0.503 and a perfect score is not the model getting
things wrong. It is the approval policy holding correct fixes for a
person. Any model action that changes more than 20% of a column waits for
approval. With that one rule removed
(`evaluation/policies/no-share-limit.yaml`):

| Held-out | H− | H− without the 20% rule |
| --- | --- | --- |
| Dirty-cell accuracy | 0.503 | 0.960 |
| Silent errors per 1,000 cells | 15.3 | 15.3 |
| Escalation precision | 0.54 | 0.86 |

Accuracy nearly doubles and silent errors do not move, so the held fixes
were almost all right. If a reviewer approves every correct held action,
H reaches 0.996. The rule stays on by default: approving a batch of model
changes is a person's decision, and the cost is review time, not wrong
data. L does not gain the same way (0.109 to 0.596, with silent errors
rising to 52.8), which is why the rules go first.

### Reproducing

Every model answer is in `llm_cache/`, so the runs replay without an API
key:

```
python -m evaluation baseline hybrid runs/hybrid --model gemini-3.1-flash-lite
python -m evaluation score runs/hybrid
python -m evaluation baseline hybrid-norepair runs/h-noshare --model gemini-3.1-flash-lite --policy evaluation/policies/no-share-limit.yaml
```

The same for `rules`, `llm` and `hybrid-norepair`. The held-out rules
numbers come from version 1.1 of system R, which fixed one ingest bug
found while building H04 (header text was trimmed, so `" CompanyNumber"`
no longer matched its own column); the fix changed no score on H01–H03.

### Limits

One model, one run per case, 12 cases. The numbers show direction, not
precise effect sizes. Repeated runs and a second model are the next
things to add.

## Documents

- [Architecture](docs/ARCHITECTURE.md): pipeline, what is code and what is LLM, actions, validation, approval policy
- [Benchmark](docs/BENCHMARK.md): target schema, the 12 cases, ground truth format, held-out rules
- [Evaluation](docs/EVALUATION.md): systems compared, hypotheses, metrics, protocol
- [n8n](docs/N8N.md): local API and the review workflow

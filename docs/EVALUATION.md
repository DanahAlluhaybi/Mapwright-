# Evaluation

The question the evaluation answers: **does an LLM layer add anything over
deterministic rules, where exactly, and at what cost?** A result where
rules win on some issue types is a valid finding, not a failure.

## Systems compared

All systems share the same ingestion, action vocabulary, SQL compiler,
executor and validator. Only the decision-making differs.

| System | Detection | Mapping and fixes | Repair loop |
| --- | --- | --- | --- |
| **R** Rules-only | Deterministic detectors | Synonym dictionaries, header similarity, normalizer tables. Anything ambiguous is escalated. | No |
| **L** LLM-only | LLM reads profiles and samples | LLM proposes the full action plan in one pass | No |
| **H** Hybrid | Deterministic detectors, then LLM for what rules leave unresolved | Rules first, LLM for semantic issues and unmapped columns | Yes, up to 2 revisions per failed action |
| **H−** Hybrid without repair | Same as H | Same as H | No |

H− exists only to isolate what the repair loop contributes.

## Hypotheses, stated before any results

1. R matches or beats L on structural issues (S1–S8).
2. L beats R on semantic issues (V1–V7) and non-English headers (M2), and
   the gap is larger on held-out cases.
3. H scores at least as high as both R and L on dirty-cell accuracy.
4. H has a lower silent-error rate than L.
5. The repair loop (H vs H−) reduces execution failures without increasing
   silent errors.

Each hypothesis is reported as supported, not supported, or inconclusive.

## Metrics

### 1. Schema mapping

One prediction is a pair (set of source columns → target field).

- **Mapping precision** = correct pairs / predicted pairs
- **Mapping recall** = correct pairs / ground-truth pairs
- **Mapping F1**

A split field (M3) counts as correct only if every source column is right.
Correctly dropping an irrelevant column (M5) counts as a correct pair with
target `drop`.

### 2. Issue detection

One ground-truth instance is `(issue type, target field, source row)` for
cell- and row-level issues, and `(issue type, target field)` for
column-level issues.

- **Precision, recall and F1** per issue type, per layer (mapping,
  structural, semantic), and overall.
- **Location-only F1**: the same, ignoring the issue type label. This
  separates "found the problem" from "named it correctly".

Detected issues with no matching instance count as false positives. Those
are the detection hallucinations.

Matching details:

- Duplicate issues (S2, S8) are matched per group: a detection counts if
  it pairs the two rows of a group, in either order.
- Cell-level detections on rows whose lineage is `drop` are ignored. Those
  rows should be removed, so flagging their cells is neither right nor
  wrong.

### 3. Transformation correctness

Output rows are aligned to ground-truth rows by primary key, using
`row_lineage` from the manifest.

- **Dirty-cell accuracy** (headline metric): among cells where the source
  value differs from the ground truth, the share the output gets exactly
  right. Easy, already-clean cells are excluded so they cannot inflate the
  score.
- **Clean-cell preservation**: among cells that were already correct in
  the source, the share left correct. This catches over-correction.
- Cells marked `unrecoverable` in the manifest are excluded from both
  accuracy metrics. They are scored through escalation recall and
  fabrication rate instead.
- **Overall cell accuracy**: all ground-truth cells. Rows missing from the
  output count every cell as wrong.
- **Row recall**: ground-truth rows present in the output.
- **Spurious row rate**: output rows that should have been dropped or
  never existed.

Cells are compared in canonical form: strings exactly, decimals within
0.005, dates as ISO strings, null equals null.

### 4. Validation and safety

- **Contract pass rate**: share of contract checks (types, required,
  unique, ranges, patterns, allowed values, foreign keys) that pass on the
  output.
- **Silent-error rate**: wrong cells that still pass every contract check,
  per 1,000 output cells. This is the most dangerous failure mode: output
  that looks valid and is wrong. Contract pass rate minus correctness is
  exactly the gap this metric shows.
- **Fabrication rate**: output cells that are filled in where the ground
  truth is null, as a share of ground-truth nulls. This counts values the
  system invented.

### 5. Execution and repair

- **Action validity rate**: proposed actions that are well-formed and
  reference real columns, values and target fields. Invalid ones count as
  hallucinated actions.
- **Execution success rate**: valid actions that run without error.
- **Repair recovery rate** (H only): failed actions fixed within the
  revision budget.
- **Mean revisions per failed action**.

### 6. Human approval

The ground-truth manifest marks each issue `auto` or `review`.

- **Escalation rate**: actions sent for approval / all actions.
- **Escalation precision**: escalated actions that touch a `review` issue /
  escalated actions. Low precision means the reviewer is flooded.
- **Escalation recall**: `review` issues covered by an escalated action /
  all `review` issues.
- **Missed reviews** (count): `review` issues that were applied
  automatically. Reported per case; every one is listed in the failure
  analysis.

Approval is scored two ways:

- **Unattended**: escalated actions are not applied. This is what the
  system achieves alone.
- **Oracle reviewer**: escalated actions are resolved correctly. This is
  the upper bound with a perfect human.

The difference between the two is the value the human adds.

### 7. Cost and latency

Per case: LLM calls, input and output tokens, wall-clock time, and the
estimated cost at the model's published paid price (even when run on a
free tier). Rules-only is the zero-cost reference.

## Protocol

- Temperature 0. Each LLM system runs 3 times per case; results report the
  mean and the min–max range.
- Every LLM request and response is cached by model, prompt version and
  request hash. Re-scoring never calls the API, and anyone can replay a
  published run without an API key.
- Model name and run date are recorded with every result.
- Prompts and rules are tuned on development cases only. Held-out cases
  are scored once per system version, and results are reported
  separately.
- The scorer is deterministic and has its own unit tests, written against
  hand-computed expected values.

## Report

`mapwright bench` produces `results/<run-id>/`:

- `scores.json`: every metric, per case, per system
- `summary.md`: the headline tables below
- `failures.md`: every missed review, fabrication and silent error, with
  the source value, output value and ground truth

Headline tables:

1. Systems × {dirty-cell accuracy, clean-cell preservation, silent-error
   rate, mapping F1, detection F1, missed reviews, cost per case}, with
   development and held-out results side by side.
2. Detection F1 per issue type × system, the table that answers "where
   does the LLM help?"
3. H vs H−: execution success, repair recovery, silent errors.

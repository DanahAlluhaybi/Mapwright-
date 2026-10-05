# Benchmark

The benchmark exists before the pipeline does. Every design decision in
Mapwright is judged against it, and the three systems (rules-only, LLM-only,
hybrid) are compared on exactly the same files.

## What a benchmark case is

Each case is a messy legacy export plus everything needed to score a run
against it:

```
benchmark/datasets/D02/
  source.csv          the messy export, as a legacy system would produce it
  ground_truth.csv    the correct result, in the target contract's canonical form
  manifest.json       mapping truth, every injected issue, row lineage
```

### How cases are built

Cases are generated, not hand-edited, so the ground truth is exact:

1. **Clean records.** A seeded generator produces valid records for the target
   contract (`contracts/customer.yaml` or `contracts/sales_order.yaml`). This
   is `ground_truth.csv`.
2. **Legacy rendering.** A source "style" renames, splits, merges, reorders
   and drops columns, and re-formats values the way a specific kind of
   legacy system would (ERP abbreviations, Arabic headers, spreadsheet
   exports).
3. **Issue injection.** Issues from `benchmark/issue_types.yaml` are injected
   at controlled rates. Every injected instance is written to the manifest
   with its column and source row numbers.

Because the source is derived from the ground truth, every source row is
traceable to a target row (or to "should be dropped", for duplicates).

### Manifest format

```json
{
  "dataset_id": "D02",
  "entity": "customer",
  "split": "dev",
  "seed": 2,
  "style": "legacy_erp",
  "encoding": "utf-8",
  "source_rows": 318,
  "target_rows": 300,
  "column_mapping": [
    {"source": ["CUST_NO"], "target": "customer_id"},
    {"source": ["FIRST_NM", "LAST_NM"], "target": "full_name"},
    {"source": ["CTRY"], "target": "country_code"}
  ],
  "dropped_columns": ["ROW_HASH"],
  "missing_target_columns": [],
  "issues": [
    {"issue": "V1", "column": "CTRY", "target": "country_code",
     "rows": [4, 17, 52], "risk": "auto"},
    {"issue": "S8", "column": "CUST_NO", "target": "customer_id",
     "rows": [88, 241], "risk": "review"}
  ],
  "row_lineage": {"1": "C-000101", "2": "C-000102", "88": "drop"}
}
```

Source rows are numbered from 1, excluding the header. `row_lineage` maps
each source row to the primary key of its ground-truth row, or to `"drop"`
when the correct behavior is to remove it.

When a target field has no source at all (`missing_target_columns`, issue
M6), its ground-truth cells are null and the correct behavior is to
escalate, never to invent values. Those cells are excluded from cell
accuracy and scored through escalation recall and fabrication rate.

## Target schema

Two entities, defined as machine-readable contracts:

| Contract | Rows per case | Why it is in the benchmark |
| --- | --- | --- |
| `customer` | 200–500 | Master data onboarding: names, contacts, countries, cities, flags. Most semantic issues live here. |
| `sales_order` | 300–800 | Transactional data: amounts, currencies, statuses, and a foreign key into customers. |

Order cases reference a fixed, clean customer table
(`benchmark/reference_customers.csv`) so foreign-key checks have a stable
target.

## The 12 cases

Eight development cases are used while building rules and prompts. Four
held-out cases are generated from styles and value variants that never
appear in the development set.

| ID | Split | Entity | Source style | Main issues |
| --- | --- | --- | --- | --- |
| D01 | dev | customer | Modern CRM export, English headers | M1, S3, S4, S6 |
| D02 | dev | customer | Legacy ERP, abbreviated headers (`CUST_NM`, `CTRY`) | M1, M5, V1, V5, V2 |
| D03 | dev | customer | Arabic headers, mixed Arabic and English values | M2, V1, V3, V5, S3 |
| D04 | dev | customer | Split names, mixed phone formats | M3, S6, V3, V7 |
| D05 | dev | customer | Duplicate-heavy merge of two branch exports | S2, S8, M5, S4 |
| D06 | dev | sales_order | Finance export, amounts with symbols and separators | S1, V4, V6, V2 |
| D07 | dev | sales_order | POS export, merged date-time, orphan customers | M4, S5, S7, V6 |
| D08 | dev | sales_order | Spreadsheet export, Windows-1256 encoding, title rows above header, no channel column | S1, V4, V1, M1, M6 |
| H01 | held-out | customer | Unseen header style (camelCase plus transliterated Arabic) | M1, M2, V1, V5, V2 |
| H02 | held-out | customer | All customer issue types at a higher noise rate | all customer types |
| H03 | held-out | sales_order | Unseen status phrasings and currency notations | V4, V6, S1, S7 |
| H04 | held-out | customer | Built from a real public dataset, re-mapped by hand | whatever the data contains |

H04 is the honesty check: it is the only case whose messiness was not
designed by us. If no suitable public dataset is found, H04 becomes one more
generated held-out style, and the report says so.

### Held-out rules

- Held-out cases are generated and committed **before** any LLM prompt is
  written. The commit hash is recorded in `benchmark/FROZEN`.
- Held-out files are never opened while tuning rules or prompts. Results on
  them are reported separately from development results, never averaged in.
- Held-out value variants (new country spellings, new status phrasings)
  are drawn from a separate list that the rules dictionaries are not built
  from. This is deliberate: real data contains variants nobody listed in
  advance, and that is exactly the gap being measured.

## Noise levels

Each injected issue type affects 3–10% of the relevant cells or rows in a
case, except H02, which uses 10–20%. Every case contains at least one issue
whose correct handling is `review`, so the approval gate is always exercised,
and every issue type except the stretch type V8 appears in at least one
development case.

## Known limitations

- Eleven of twelve cases are synthetic. Generated messiness is cleaner than
  real messiness; H04 exists to show how large that gap is.
- One target schema per entity. Mapping onto an unseen target contract is
  out of scope.
- Case sizes are small (hundreds of rows) to keep LLM cost and evaluation
  time practical. Throughput at scale is not measured.

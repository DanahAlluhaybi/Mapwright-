# Benchmark

The benchmark exists before the pipeline does. Every design decision in
Mapwright is judged against it, and the systems being compared (rules-only,
LLM-only, hybrid) run on exactly the same files.

## What a benchmark case is

Each case is a messy legacy export plus everything needed to score a run
against it:

```
benchmark/datasets/D02/
  source.csv          the messy export, as a legacy system would produce it
  ground_truth.csv    the correct result, in the target contract's canonical form
  manifest.json       mapping truth, every issue, row lineage
```

### How cases are built

Cases are generated from definitions in `benchmark/generator/cases.py`, not
hand-edited, so the ground truth is exact:

1. **Clean records.** A seeded generator produces valid records for the
   target contract (`contracts/customer.yaml` or `contracts/sales_order.yaml`).
2. **Legacy rendering.** Each case has a source style: its own headers,
   split or merged columns, irrelevant extra columns, and column-wide
   formats (day-first dates, Y/N flags, upper-case names, coded types).
3. **Issue injection.** Issues from `benchmark/issue_types.yaml` are
   injected cell by cell at set rates. A cell is corrupted at most once.
4. **Duplicates and orphans.** Exact duplicates, conflicting duplicates and
   orders for unknown customers are inserted as extra source rows.

Some injections change what the correct output is. An invalid email's
correct output is null. A blank required value cannot be recovered, so its
ground truth is null and the right behavior is to escalate. An amount
written as `12.5K` is generated from a value that really is 12,500.00.
`ground_truth.csv` always holds the correct output, not the original
clean record.

The generator records every cell it changes. A test re-derives every
difference between source and ground truth and fails if any difference is
unexplained by the manifest, or if the manifest claims a change that is
not there.

### Manifest format

Abridged, with entries from different cases for illustration:

```json
{
  "dataset_id": "D02",
  "entity": "customer",
  "split": "dev",
  "seed": 102,
  "style": "legacy_erp",
  "pool": "dev",
  "encoding": "utf-8",
  "header_line": 1,
  "source_rows": 300,
  "target_rows": 300,
  "column_mapping": [
    {"source": ["CUST_NO"], "target": "customer_id"},
    {"source": ["first_name", "last_name"], "target": "full_name"}
  ],
  "dropped_columns": ["ROW_HASH", "LAST_UPD_BY"],
  "missing_target_columns": [],
  "issues": [
    {"issue": "M1", "target": "customer_id", "columns": ["CUST_NO"],
     "unit": "column", "risk": "auto"},
    {"issue": "V2", "target": "signup_date", "columns": ["OPEN_DT"],
     "unit": "column", "risk": "review"},
    {"issue": "V1", "target": "country_code", "columns": ["CTRY"],
     "unit": "cell", "risk": "auto", "rows": [3, 9, 14]},
    {"issue": "S3", "target": "customer_type", "columns": ["CUST_TYP"],
     "unit": "cell", "risk": "review", "unrecoverable": true, "rows": [41]},
    {"issue": "S8", "target": "customer_id", "columns": ["customer_id"],
     "unit": "row", "risk": "review", "groups": [[12, 13]]}
  ],
  "row_lineage": {"1": "C-100001", "2": "C-100002", "13": "drop"}
}
```

- Source rows are numbered from 1, starting after the header line.
  `header_line` is 3 when two title rows sit above the header.
- `row_lineage` maps each source row to the primary key of its
  ground-truth row, or to `"drop"` when the correct behavior is to remove
  it.
- Column-level issues (M1–M6, V2) name the columns. Cell-level issues add
  `rows`. Duplicates (S2, S8) use `groups`: the original row first, then its
  copy. Orphan orders (S7) list the orphan rows.
- `unrecoverable: true` (S3, M6) marks cells whose ground truth is null
  because the source holds no value. The correct behavior is to escalate,
  never to invent one.

## Target schema

Two entities, defined as machine-readable contracts:

| Contract | Rows per case | Why it is in the benchmark |
| --- | --- | --- |
| `customer` | 250–300 | Master data onboarding: names, contacts, countries, cities, flags. Most semantic issues live here. |
| `sales_order` | 300–350 | Transactional data: amounts, currencies, statuses, and a foreign key into customers. |

Order cases reference a fixed, clean customer table
(`benchmark/reference_customers.csv`, 400 rows) so foreign-key checks have a
stable target.

## The cases

Eight development cases are used while building rules and prompts. Three
generated held-out cases use header styles and value variants that never
appear in development. A fourth held-out case, H04, comes from a real
public export.

| ID | Split | Entity | Source style | Issues |
| --- | --- | --- | --- | --- |
| D01 | dev | customer | Modern CRM export, readable English headers | M1, S3, S4, S6 |
| D02 | dev | customer | Legacy ERP: abbreviated headers, upper-case names, coded flags, day-first dates | M1, M5, S4, V1, V2, V5 |
| D03 | dev | customer | Arabic headers, mixed Arabic and English values, placeholder nulls | M2, S3, S9, V1, V3, V5 |
| D04 | dev | customer | Split names, Saudi mobiles in local formats, phones typed into the email column | M1, M3, S6, S9, V3, V7 |
| D05 | dev | customer | Two branch exports merged: duplicates and conflicting records | M5, S2, S4, S8 |
| D06 | dev | sales_order | Finance export: month-first dates, formatted amounts, free-text statuses | M1, S1, V1, V2, V4, V6 |
| D07 | dev | sales_order | Point-of-sale export: timestamps, sign errors, unknown customers | M1, M4, M5, S5, S7, V1, V6 |
| D08 | dev | sales_order | Spreadsheet saved as Windows-1256, title rows, no channel column | M1, M6, S1, S9, V1, V4, V6 |
| H01 | held-out | customer | camelCase and transliterated Arabic headers; every date is day/month ambiguous | M1, M2, V1, V2, V3, V5 |
| H02 | held-out | customer | Most cell-level customer issues at once, at higher noise | M1, M5, S1–S6, S8, S9, V1, V3, V4, V5, V7 |
| H03 | held-out | sales_order | Unseen status phrasings, currency notations and channel names | M1, S1, S7, V1, V4, V6 |
| H04 | held-out | customer | Real export: Companies House basic company data, 55 columns, mapped by hand | M1, M5, M6, S4, V1, V2, V5 |

H04 is the honesty check: it is the only case whose messiness was not
designed by us. Its source is 300 rows sampled from the Companies House
Free Company Data Product (UK company register, October 2026 snapshot,
Open Government Licence v3.0): companies registered in London and
incorporated since 2000, with every column and value exactly as
published. Nothing is injected. The ground truth comes from a fixed,
documented mapping written once in `benchmark/real/companies_house.py`,
which `verify` re-runs against the frozen hashes.

What makes it hard is real: 48 irrelevant columns, headers with leading
spaces (`" CompanyNumber"`), upper-case names with punctuation and
digits, a legal category that has to be read as a customer type, a free
text status that has to become a boolean, day-first dates, and three
contract fields (email, phone, credit limit) the export simply does not
have. It is narrower than the generated cases: no Arabic, one country,
and, because part 1 of the export is sorted by name, only names starting
with a digit, A or B. Acronyms in names follow the contract's title-case
rule literally (`(UK)` becomes `(Uk)`).

D02's day-first dates can be resolved from evidence (some days are above
12). H01's cannot: every day is 12 or below, so the only correct behavior is
to ask a human.

### Held-out rules

- Held-out cases are generated and frozen **before** any LLM prompt is
  written. `benchmark/FROZEN` records a SHA-256 hash of every held-out file,
  and `python -m benchmark.generator verify` fails if any of them changes.
- Held-out files are never opened while tuning rules or prompts. Results on
  them are reported separately from development results, never averaged in.
- Held-out value variants (new country spellings, status phrasings,
  placeholder tokens) come from a separate pool that shares no value with
  the development pool, and a test enforces that. This is deliberate: real
  data contains variants nobody listed in advance, and that is exactly the
  gap being measured.

## Noise levels

Column-wide formats change every non-null value in their column. Injected
issues hit between 3% and 70% of eligible cells, depending on the issue: a
placeholder-null injection at 70% only touches cells that are null to begin
with, while invalid emails stay at 4–8%. Every case contains at least one
issue whose correct handling is `review`, and every issue type except the
stretch type V8 appears in at least one development case. Tests enforce
both.

## Regenerating

```
python -m benchmark.generator generate   # write every case and the reference table
python -m benchmark.generator verify     # regenerate in memory and compare with disk and FROZEN
```

Generation is deterministic. `verify` should always print `OK`; if it does
not, a case definition or the generator changed after the benchmark was
frozen.

## Known limitations

- Eleven of the twelve cases are synthetic. Generated messiness is cleaner
  than real messiness; H04 exists to show how large that gap is, but it is
  one export from one source.
- One target schema per entity. Mapping onto an unseen target contract is
  out of scope.
- Case sizes are small (hundreds of rows) to keep LLM cost and evaluation
  time practical. Throughput at scale is not measured.

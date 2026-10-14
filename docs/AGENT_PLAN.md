# Interactive data agent: current state and plan

Phase 1 of the brief in the conversation of 2026-10-14: what Mapwright
already does, what the interactive agent needs, and the order to build it
in. No code was changed for this document.

## 1. What exists today

About 4,900 lines in `src/mapwright/`, 163 tests, all passing.

| Area | What exists | Where |
| --- | --- | --- |
| Ingest | CSV with encoding, delimiter and header-line detection; Excel sheets converted to the same table | `ingest.py`, `excel.py` |
| Profiling | Per column: rows, blanks, distinct count, top values, value shapes ("A-9"), length range, share of values that parse as dates, numbers, emails, phones | `profile.py` |
| Target schemas | YAML contracts: field types, required, unique, patterns, allowed values, ranges, reference lists, primary key | `contracts/`, `contracts.py` |
| Column mapping | Header dictionaries (Arabic and English) plus the model for the rest | `mapping.py`, `llmplan.py` |
| Typed actions | `map_column`, `combine_columns`, `drop_column`, `normalize_text`, `set_null`, `map_values`, `parse_date`, `parse_number`, `normalize_phone`, `move_value`, `deduplicate`, `drop_rows`. Each one computes its exact cell changes before anything is applied | `actions.py` |
| Rule planner | Proposes actions per field from the contract and the rule dictionaries | `planner.py`, `rules/` |
| Model layer | The model proposes actions as JSON; code checks each one against the action vocabulary before it can run; bounded repair loop; Gemini or a local model through Ollama; every answer cached | `llmplan.py`, `llm/` |
| Approval policy | Apply, flag or hold, from one YAML file; destructive actions are always held | `policy.py`, `policy.yaml` |
| Validation | Contract checks after every step | `contracts.py`, `validate.py` |
| Audit and replay | `audit.jsonl` with actor, decision and table hashes per step; `plan.json` replays exactly, with approvals | `audit.py`, `pipeline.replay` |
| Review | Local API, upload page (CSV and Excel, rules editor, approve, download), n8n workflow | `server.py`, `ui.html`, `n8n/` |
| Evidence | 12-case benchmark with exact ground truth, scorer, three-run results, `evaluation.check` | `benchmark/`, `evaluation/` |

The original data is already never changed: every run copies the upload
and writes new files next to it.

## 2. The one structural gap

Everything today is driven by a **target schema chosen in advance**
(customer or sales order). The brief asks for the opposite starting point:
an unknown file, whose meaning has to be discovered and agreed with the
user.

The engine does not need to be replaced to close this gap. It needs a new
first step:

> **Infer a draft schema from the file itself.** Profile every column,
> propose its type and role (identifier, category, date, amount, flag,
> free text...), and let the user confirm or change it. The confirmed
> draft is an ordinary contract, so the existing planner, actions,
> policy, validation, audit and replay all work on it unchanged.

Everything else in the brief builds on that step.

## 3. Capabilities: have, partial, missing

| Brief section | Status | Notes |
| --- | --- | --- |
| 3. Dataset understanding | **Partial** | Profiles exist but are not shown to the user, and there is no type or role inference, outlier check or cross-column check |
| 4. Column-aware conversation | **Missing** | No per-column view and no chat |
| 5. Plans, previews, approval | **Mostly there** | Typed actions, exact effects, held actions and approval all exist. Missing: before/after examples in the review, a `to_boolean` action, and turning a sentence into an action |
| 6. Data quality in the conversation | **Partial** | The engine and its findings exist; what is missing is explaining them in plain language and letting the user choose a fix |
| 7. Analytics and insights | **Missing** | Nothing computes trends, group comparisons or forecasts |
| 8. Reports and dashboards | **Partial** | `report.md` and the cleaning summary only |
| 9. n8n | **Partial** | Upload, review and approve workflow, with a token. Missing: run status for long jobs, retries and idempotency keys, scheduled checks |
| 10. Architecture rules | **Already followed** | Model output is never executed as code; it becomes typed actions that code validates. Only samples and summaries go to the model |

## 4. Design rules carried over

1. **The model never touches data directly.** It chooses from a fixed
   vocabulary: actions for cleaning, query specs for analysis. Code
   validates the choice and computes every number.
2. **Every change is previewed** with counts and before/after examples,
   and anything destructive or uncertain waits for approval.
3. **The original upload is never modified.** Each session keeps its own
   folder, and every approved step is in the audit log and the replayable
   plan.
4. **Facts and guesses are labelled.** Profile numbers are facts; the
   meaning of a column is a hypothesis until the user confirms it.
5. **The model only sees what it needs**: column names, profiles and a
   few sample values, never the whole file. A local model through Ollama
   keeps even that on the machine.
6. **The benchmark stays the evidence for cleaning.** New analytics
   features get their own tests with known answers.

## 5. Plan

Each phase ends with something a person can use from the page, plus
tests. The phase numbers follow the brief.

### Phase 2: Understand the file (first, and the largest gap)

- `infer.py`: from the existing profiles, propose for each column a type
  (text, integer, decimal, date, boolean, category, identifier, email,
  phone) and a role, with the evidence ("98% parse as dd/mm/yyyy"; "12
  distinct values over 4,000 rows"). Deterministic; no model needed.
- Optional model step: a one-line plain-language guess of what each
  column means, clearly marked as a guess.
- A draft contract generated from the inference, so the existing pipeline
  can run on any file with no schema chosen in advance.
- Page: an **Explore** view listing every column with type, blanks,
  distinct values, examples and detected issues, the rules editor reused
  to confirm or change types, and a new "Detect automatically" schema
  option.

### Phase 3: Instructions in plain language

- Select one or more columns and type a request ("convert to yes/no",
  "these five spellings are one company").
- The model turns it into one or more typed actions **from the existing
  vocabulary**, plus a few new ones: `to_boolean` (with a visible
  true/false mapping), `rename_column`, `split_column`, `fill_missing`
  (only explicit strategies, never invented values).
- Code computes the effect; the page shows affected rows and five
  before/after examples. Ambiguous requests come back as one focused
  question with example interpretations.
- Approved actions are appended to the session's plan, so the whole
  session replays.

### Phase 4: Data quality in the conversation

- Show the engine's findings per column in plain language, ordered by
  rows affected, each with the fixes the engine can apply.
- Label each one as a confirmed error (breaks a confirmed rule), a
  possible anomaly, or a recommendation.

### Phase 5: Analysis

- A small query vocabulary run by code: count, sum, average, median by
  group; trend by day, week or month; top and bottom N; share of total;
  two-column comparison; simple outlier list (IQR).
- The model maps a question to one or more of these queries; code runs
  them and returns the numbers; the model then writes the explanation
  from those numbers only, with correlation never described as cause.
- Forecasting only when there are enough periods, with a held-back
  check and the error shown. Otherwise it says why not.

### Phase 6: Reports and dashboards

- Reports in Markdown and HTML assembled from the session: profile, data
  quality, changes made with before/after counts, analysis results.
- A dashboard page built from the queries the user kept, with filters.
  Charts drawn from computed numbers only.
- Exports: cleaned CSV and Excel, the report, the audit log.

### Phase 7: n8n

- Long jobs run in the background with `GET /runs/<id>` status
  (`queued`, `running`, `waiting_for_approval`, `done`, `failed`).
- Idempotency keys so a repeated trigger does not start a second run.
- Workflows: scheduled check of a file or folder with a notification
  when quality drops; report delivery by email or Telegram.

### Phase 8: End-to-end

- The 12 acceptance criteria as one scripted browser test, plus failure
  tests: bad files, model down, timeouts, a repeated trigger.

## 6. Effort and risks

- **Size.** Phases 2–4 extend what exists and are the core of the brief.
  Phases 5–6 are new subsystems. Built in this order, each phase can
  stop at a working state.
- **Model quality.** A local model is weaker; every model step can
  therefore fall back to "ask the user", and nothing it proposes runs
  without validation.
- **Project focus.** The benchmark measures cleaning, not analysis. The
  README should present the agent as built on the measured engine, and
  not claim analysis accuracy without its own tests.
- **Privacy.** Files from work stay local: use Ollama for them, and keep
  sessions under `runs/`, which git ignores.

## 7. Recommended next step

Phase 2, column type and role inference plus the Explore view. It is
deterministic, needs no model, closes the main gap, and every later phase
builds on it.

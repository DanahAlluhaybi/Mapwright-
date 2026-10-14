# Mapwright Agent

The agent works on any CSV or Excel file, with no target schema chosen in
advance. It profiles the file, explains each column with the evidence, turns
requests in plain language into steps you approve, runs analysis whose
numbers are all computed by code, and builds a report and a dashboard.

Start it like the rest of Mapwright (`start.bat`, or
`python -m mapwright serve --open`) and open **http://127.0.0.1:8765/agent**.

## The journey

1. **Open a file.** CSV or Excel (choose the sheet). The upload is stored
   unchanged; everything else is built from it.
2. **Understand it.** Every column gets a type (yes/no, whole number,
   number, date, email, phone, identifier, category, text) with the
   evidence: "412 of 420 filled values (98%) read as dates (dd/mm/yyyy)".
   The meaning from the column name and the guess of what the whole file
   is are labelled **guess**. If a type is wrong, change it in the column
   panel; that is recorded.
3. **Ask.** Tick columns, then type a request. Examples that work with the
   built-in rules, in English or Arabic:

   | Request | What happens |
   | --- | --- |
   | convert Active to yes/no · حول Active الى نعم/لا | a yes/no conversion to preview; if the values are not yes/no words, a question about which one means yes |
   | convert Joined into a consistent date format | dates rewritten as yyyy-mm-dd; if day and month cannot be told apart, a question first |
   | standardise these names | spacing and capitalisation; different spellings merged to the most common one |
   | Individual = INDIVIDUAL, Company = BUSINESS | your own mapping |
   | fill missing Credit | a question: leave them, most common value, median, remove the rows, or a value you type |
   | remove duplicates | duplicate rows, with a warning if removed rows differ from the kept one |
   | which columns are useful for sales analysis | amounts, dates, groups and identifiers, from the types |
   | top 5 City by Credit · trend of Amount per month · forecast Amount per quarter · relationship between Type and Credit · find suspicious records | analysis with a chart, facts and caveats |
   | show the data quality issues · create a dashboard · report | findings with fixes, the dashboard, the report |

   With an AI model chosen at the top (Gemini, or a free local model
   through Ollama), free-form requests work too. The model only sees column
   names, counts and a few examples, and its answer goes through the same
   checks as the rules: anything outside the vocabulary is refused, never
   run.
4. **Review and approve.** Each proposed step shows how many cells and rows
   it changes, before/after examples, values it could not convert (left as
   they were), and a risk label: *safe* (formatting), *review* (changes
   values) or *destructive* (removes or fills data). Nothing runs until you
   approve it. After it runs, the data is checked again and the page says
   which findings were resolved and whether any new ones appeared.
5. **Analyse.** Results say whether they are *calculated* or an
   *estimate*. Relationships carry a note that they do not show cause.
   Forecasts are only made with at least 8 periods of history, choose
   between three simple methods by testing on the last periods they did not
   see, and show the error. An incomplete last period is pointed out (and
   left out of forecasts).
6. **Share.** *Add to dashboard* keeps a result. The dashboard can be
   filtered by a category or a date range; the report collects the profile,
   the findings, every step with its effect, and the kept results. Download
   the cleaned CSV or Excel file, and the steps as a plan.
7. **Repeat.** *Undo the last step* rebuilds the table without it. The
   steps can be replayed on next month's file through the API or n8n.

## What the agent will not do

- Run code it was given or that a model wrote. It runs a fixed list of
  operations and queries, each checked before it runs.
- Invent data. Missing values stay missing unless you choose how to fill
  them, and filled values are labelled as estimates.
- Change the uploaded file.
- Send the file to a model. Only names, counts and a few example values.
- State a cause from a correlation, or a forecast as a fact.

## Operations and queries

Operations: `to_boolean`, `parse_date`, `parse_number`, `normalize_text`,
`normalize_email`, `normalize_phone`, `map_values`, `set_missing`,
`fill_missing`, `rename_column`, `drop_column`, `split_column`,
`drop_rows`, `deduplicate` (`src/mapwright/agent/ops.py`).

Queries: `overview`, `summary`, `group`, `share`, `trend`, `forecast`,
`relation`, `outliers`, `missing`, each with optional `where` filters
(`src/mapwright/agent/analysis.py`).

## Files

Each session lives in `runs/sessions/<id>/` (ignored by git):
`original.csv`, `plan.json` (approved steps), `history.jsonl` (every
proposal, approval, rejection and undo, with who and the effect),
`conversation.jsonl`, `pinned.json`.

## API

The page uses these; n8n or a script can too. JSON unless noted.

| Request | Does |
| --- | --- |
| `POST /sessions?filename=x.xlsx&sheet=S` with the file as the body | open a session |
| `GET /sessions/<id>` | overview: columns, finding counts, pending steps |
| `GET /sessions/<id>/columns/<name>` | one column with evidence and findings |
| `POST /sessions/<id>/columns/<name>/type` `{"type", "reviewer"}` | confirm or change a type |
| `POST /sessions/<id>/ask` `{"text", "columns", "model"}` | interpret a request |
| `POST /sessions/<id>/proposals` `{"op"}` | preview an operation |
| `POST /sessions/<id>/proposals/<pid>` `{"decision", "reviewer"}` | approve or reject |
| `POST /sessions/<id>/undo` | undo the last step |
| `POST /sessions/<id>/query` `{"query"}` | run an analysis query |
| `POST /sessions/<id>/pins` `{"query"}` | add to the dashboard |
| `GET /sessions/<id>/report`, `/dashboard?column=&value=` | HTML pages |
| `GET /sessions/<id>/export/csv`, `xlsx`, `plan` | downloads |
| `POST /jobs`, `GET /jobs/<id>` | background jobs, see [N8N.md](N8N.md) |

## Limits

- Files are processed in memory in plain Python: comfortable up to tens of
  thousands of rows.
- Phone normalisation knows international formats and Saudi mobile
  numbers; other local formats are left for review.
- The analysis is descriptive. The cleaning engine is measured by the
  benchmark; the agent's analysis is covered by tests with known answers,
  not by a benchmark.

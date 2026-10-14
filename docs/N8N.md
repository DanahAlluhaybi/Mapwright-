# Running Mapwright from n8n

n8n handles the workflow around a run: a person uploads a file, sees what
Mapwright held back, decides, and gets the result. Mapwright still does
all the data work and keeps the audit log. n8n never changes data itself;
it only calls Mapwright's local API.

```
n8n form (upload) → POST /runs → n8n form (review) → POST /runs/<id>/review → n8n page (result)
```

Everything runs on your machine.

## 1. Start the Mapwright API

In the project folder, with the virtual environment active:

```
python -m mapwright serve --reference customer_id=benchmark/reference_customers.csv
```

It listens on `http://127.0.0.1:8765`. Leave this terminal open. Check it
from a browser: `http://127.0.0.1:8765/health` should show `{"ok": true}`.

For `hybrid` or `llm` runs, set `GEMINI_API_KEY` in that terminal before
starting the server. Runs are kept in `runs/api/<id>/` (ignored by git).

## 2. Start n8n

In a second terminal (needs Node.js):

```
npx n8n
```

Open `http://localhost:5678` and create the local owner account.

If you run n8n in Docker instead, the API is not on the container's
`127.0.0.1`: start Mapwright with `--host 0.0.0.0`, set
`MAPWRIGHT_API_TOKEN`, and change the URLs in the workflow to
`http://host.docker.internal:8765` (see "Token" below).

## 3. Import the workflow

In n8n: **Workflows → Add workflow → ⋯ → Import from File** and pick
`n8n/mapwright-review.json`. It has five nodes:

| Node | Type | What it does |
| --- | --- | --- |
| Upload file | Form Trigger | Form with the CSV, the contract and the system (rules, hybrid, llm) |
| Start run | HTTP Request | Sends the file to `POST /runs` |
| Review | Form (next page) | Shows the summary and every held action; asks for a decision and your name |
| Send decision | HTTP Request | Sends the decision to `POST /runs/<id>/review` |
| Done | Form (ending) | Shows the result and the link to the output CSV |

## 4. Try it

Click **Execute workflow** (or open the Upload file node and use its test
URL). Upload `benchmark/datasets/D05/source.csv` with contract `customer`
and system `rules`: the rules hold one deduplication that would remove 8
rows. On the review page:

- **Approve all held actions** applies every held action.
- **Approve only the ids below** applies the ids you type, e.g. `a17`.
- **Keep held actions out** approves the run without the held actions.
- **Reject the run** writes no approved output.

The decision, your name and the hash of the approved table are added to
`runs/api/<id>/audit.jsonl`. A run can be reviewed once.

To use the form without the editor open, **Activate** the workflow and use
the production URL of the Upload file node.

## The API

| Request | Returns |
| --- | --- |
| `GET /health` | `{"ok": true}` |
| `GET /contracts` | contract names |
| `POST /runs?contract=customer&system=rules&filename=x.csv` with the raw CSV as the body | the run summary (status 201) |
| `POST /runs` with JSON `{"source_path": "...", "contract": "...", "system": "..."}` | same, for a file inside the project folder |
| `GET /runs` | recent runs |
| `GET /runs/<id>` | the run summary |
| `GET /runs/<id>/report` | `report.md` |
| `GET /runs/<id>/target.csv` | the output; the approved output once approved |
| `GET /runs/<id>/audit` | `audit.jsonl` |
| `POST /runs/<id>/review` with JSON `{"decision": "approve" or "reject", "approve": "all" or ["a17"] or "a17, a20", "reviewer": "name", "note": "..."}` | the updated summary |

The run summary has `status` (`awaiting_review`, `complete`, `approved`,
`rejected`), `rows`, `held_actions` (id, type, targets, why it was held,
cells changed, rows removed), `needs_review`, `usage`, and two ready-made
texts for messages: `summary_text` and `review_text`.

Errors come back as `{"error": "..."}` with status 400 (bad input), 404,
409 (already reviewed), 422 (file could not be processed) or 502 (the
model call failed).

## Background jobs and the daily check

Long or scheduled work goes through jobs, which n8n starts and then polls:

| Request | Does |
| --- | --- |
| `POST /jobs` `{"kind": "check", "source_path": "private/customers.csv", "threshold": 0.95, "idempotency_key": "check-2026-10-14"}` | profile a file and report its quality |
| `POST /jobs` `{"kind": "apply_plan", "source_path": "private/october.csv", "session": "s-..."}` | repeat a session's approved steps on a new file |
| `GET /jobs/<id>` | `queued`, `running`, `done` (with `result`) or `failed` (with `error`) |

- `source_path` must be inside the folder the server was started from.
- The same `idempotency_key` returns the first job instead of starting a
  second one, so a repeated trigger does no double work. A failed job can
  be retried with the same key.
- `apply_plan` keeps all steps or none: if a step no longer fits the new
  file, the job fails and names the step.
- A job that runs past the time limit (10 minutes), or was running when
  the server stopped, is reported as failed.
- A `check` result has `quality_score`, finding counts, the top findings,
  `below_threshold` when a threshold was given, a one-line `summary_text`,
  and links to the report and the cleaned CSV.

`n8n/mapwright-daily-check.json` is a second workflow built on this:

```
Every morning → Start check → Wait 10 s → Get status → Finished? ─no→ Wait
                                                        └yes→ Needs attention? → Message → Notify
```

Import it like the first one, change `source_path` in **Start check** to
your file, and replace **Notify** with an email, Telegram or Slack node.
The message holds the summary and the report link; it never includes rows
from the file.

## Token

The API listens on `127.0.0.1` only, so nothing outside your machine can
reach it. If you open it wider, set a token first:

```
$env:MAPWRIGHT_API_TOKEN = "a long random string"
python -m mapwright serve --host 0.0.0.0
```

Then, in both HTTP Request nodes, turn on **Send Headers** and add
`Authorization` = `Bearer <the token>` (or use an n8n Header Auth
credential).

## Ideas to extend it

Each is a small n8n change; Mapwright stays the same.

- Send `summary_text` to Telegram or email when a run is waiting, with a
  link to the review form.
- Skip the review page when `status` is `complete` (an **If** node on
  `{{ $json.status }}`).
- Watch a folder and start a run for each new CSV, using
  `POST /runs` with `source_path`.
- Post the approved CSV somewhere (Google Drive, a database) after the
  Done step.

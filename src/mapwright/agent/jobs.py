"""Background jobs, for n8n and other schedulers.

A job runs in a worker thread and is polled with ``GET /jobs/<id>``:
``queued`` → ``running`` → ``done`` or ``failed``. Jobs are stored as JSON
files, so their status survives a restart (a job that was running when the
server stopped is reported as failed, not left hanging).

- ``check``: profile a file and report its data quality.
- ``apply_plan``: repeat a session's approved steps on a new file. All
  steps run or none are kept: if one step no longer fits the file, the job
  fails and says which step and why.

An ``idempotency_key`` makes a repeated trigger return the first job
instead of starting a second one. A job that runs longer than the time
limit is reported as failed.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import threading
import time
from pathlib import Path

from . import ops
from .quality import all_findings, quality_score
from .session import SessionError, SessionStore, now

JOB_ID = re.compile(r"^j-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
KINDS = ("check", "apply_plan")


class JobError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class JobRunner:
    def __init__(self, root: Path, sessions: SessionStore, read_source, timeout: float = 600.0, workers: int = 2):
        self.root = Path(root)
        self.sessions = sessions
        self.read_source = read_source  # (source_path) -> (bytes, filename); enforces the data folder
        self.timeout = timeout
        self.slots = threading.Semaphore(workers)
        self.lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        self._recover()

    # Storage ----------------------------------------------------------------

    def _path(self, jid: str) -> Path:
        return self.root / f"{jid}.json"

    def _save(self, job: dict) -> None:
        tmp = self._path(job["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._path(job["id"]))

    def _keys(self) -> dict:
        path = self.root / "keys.json"
        return json.loads(path.read_text("utf-8")) if path.exists() else {}

    def _recover(self) -> None:
        for path in self.root.glob("j-*.json"):
            job = json.loads(path.read_text("utf-8"))
            if job["status"] in ("queued", "running"):
                job.update(status="failed", error="the server stopped before the job finished; run it again", finished=now())
                self._save(job)

    def get(self, jid: str) -> dict:
        if not JOB_ID.match(jid or ""):
            raise JobError(f"no job {jid!r}", 404)
        path = self._path(jid)
        if not path.exists():
            raise JobError(f"no job {jid!r}", 404)
        job = json.loads(path.read_text("utf-8"))
        if job["status"] == "running" and time.time() - job.get("started_ts", time.time()) > self.timeout:
            job.update(status="failed", error=f"took longer than {self.timeout:.0f} seconds", finished=now())
            self._save(job)
        return {k: v for k, v in job.items() if k != "started_ts"}

    def list(self, limit: int = 30) -> list[dict]:
        paths = sorted(self.root.glob("j-*.json"), reverse=True)[:limit]
        return [self.get(p.stem) for p in paths]

    # Starting ---------------------------------------------------------------

    def start(self, body: dict) -> tuple[dict, bool]:
        """Returns the job and whether it is new (False when the idempotency key was seen)."""
        kind = body.get("kind")
        if kind not in KINDS:
            raise JobError(f"kind must be one of {', '.join(KINDS)}")
        if not body.get("source_path"):
            raise JobError("source_path is required: a file inside the server's data folder")
        if kind == "apply_plan":
            self.sessions.get(str(body.get("session", "")))  # fail now, not in the background
        key = str(body.get("idempotency_key") or "").strip()
        with self.lock:
            if key:
                existing = self._keys().get(key)
                if existing:
                    job = self.get(existing)
                    if job["status"] != "failed":
                        return job, False
            jid = f"j-{dt.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
            job = {"id": jid, "kind": kind, "status": "queued", "created": now(), "idempotency_key": key or None,
                   "request": {k: body.get(k) for k in ("source_path", "sheet", "session", "threshold") if body.get(k) is not None},
                   "result": None, "error": None}
            self._save(job)
            if key:
                keys = self._keys()
                keys[key] = jid
                (self.root / "keys.json").write_text(json.dumps(keys, indent=2) + "\n", encoding="utf-8")
        threading.Thread(target=self._run, args=(jid,), daemon=True).start()
        return self.get(jid), True

    def _run(self, jid: str) -> None:
        with self.slots:
            job = json.loads(self._path(jid).read_text("utf-8"))
            job.update(status="running", started=now(), started_ts=time.time())
            self._save(job)
            try:
                result = self._check(job) if job["kind"] == "check" else self._apply_plan(job)
                job.update(status="done", result=result)
            except (SessionError, JobError, ops.OpError, ValueError, OSError) as error:
                job.update(status="failed", error=str(error))
            except Exception as error:  # report, never crash the worker
                job.update(status="failed", error=f"{type(error).__name__}: {error}")
            job["finished"] = now()
            current = json.loads(self._path(jid).read_text("utf-8"))
            if current["status"] == "failed" and "longer than" in (current.get("error") or ""):
                return  # already reported as timed out
            self._save(job)

    def _open(self, job: dict):
        data, filename = self.read_source(job["request"]["source_path"])
        return self.sessions.create(data, filename, job["request"].get("sheet"))

    def _summary(self, session, threshold: float | None) -> dict:
        table, columns = session.state()
        findings = all_findings(table, columns)
        score = quality_score(table, columns, findings)
        counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("error", "anomaly", "recommendation")}
        result = {
            "session": session.id, "rows": table.size, "columns": len(table.columns), "quality_score": score,
            "findings": counts, "top_findings": [f"[{f['severity']}] {f['title']}" for f in findings[:8]],
            "report": f"/sessions/{session.id}/report", "download_csv": f"/sessions/{session.id}/export/csv",
        }
        if threshold is not None:
            result["below_threshold"] = score < float(threshold)
        result["summary_text"] = (
            f"{session.meta.get('filename')}: {table.size:,} rows, quality {score:.0%}, "
            f"{counts['error']} errors, {counts['anomaly']} anomalies, {counts['recommendation']} recommendations."
        )
        return result

    def _check(self, job: dict) -> dict:
        session = self._open(job)
        return self._summary(session, job["request"].get("threshold"))

    def _apply_plan(self, job: dict) -> dict:
        source = self.sessions.get(job["request"]["session"])
        plan = source.plan
        if not plan:
            raise JobError(f"session {source.id} has no approved steps to repeat")
        session = self._open(job)
        table = session.original()
        for number, spec in enumerate(plan, start=1):
            try:
                table, _ = ops.run(table, spec)
            except ops.OpError as error:
                raise JobError(f"step {number} ({ops.describe(spec)}) does not fit this file: {error}. No steps were kept.") from error
        session._write("plan.json", plan)
        session._log("history.jsonl", {"event": "plan_applied", "actor": "job", "from_session": source.id, "steps": len(plan)})
        result = self._summary(session, job["request"].get("threshold"))
        result["steps_applied"] = len(plan)
        result["from_session"] = source.id
        return result

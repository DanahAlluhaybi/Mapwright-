"""One agent session: an uploaded file, the steps approved on it, and what was asked.

Files under ``<root>/<id>/``:

    original.csv       the upload as read, with row numbers; never changed
    meta.json          file name, sheet, created, column type overrides, model
    plan.json          approved operations, in order; replaying them on the
                       original gives the current table
    proposals.json     previewed operations waiting for a decision
    history.jsonl      every proposal, approval, rejection and undo, with who and what changed
    conversation.jsonl requests and the kind of answer they got
    pinned.json        analysis queries kept for the dashboard

The current table is always rebuilt from the original and the plan, so a
session can be reproduced, undone step by step, or replayed on a new file.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets
import threading
from pathlib import Path

from . import analysis, ops
from .infer import ColumnInfo, dataset_summary, infer_column, infer_table
from .quality import all_findings, quality_score
from .table import Table, load_csv, read_table

SESSION_ID = re.compile(r"^s-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
TYPES = ("boolean", "integer", "decimal", "date", "email", "phone", "identifier", "category", "text")
ROLE_OF = {"boolean": "flag", "integer": "measure", "decimal": "measure", "date": "date", "email": "contact",
           "phone": "contact", "identifier": "identifier", "category": "category", "text": "text"}


class SessionError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Session:
    def __init__(self, directory: Path):
        self.dir = directory
        self.lock = threading.RLock()
        self._cache: tuple[str, Table, list[ColumnInfo]] | None = None

    # Files -----------------------------------------------------------------

    @property
    def id(self) -> str:
        return self.dir.name

    def _read(self, name: str, default):
        path = self.dir / name
        return json.loads(path.read_text("utf-8")) if path.exists() else default

    def _write(self, name: str, data) -> None:
        tmp = self.dir / (name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.dir / name)

    def _log(self, name: str, event: dict) -> None:
        with (self.dir / name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": now(), **event}, ensure_ascii=False) + "\n")

    def _lines(self, name: str) -> list[dict]:
        path = self.dir / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]

    @property
    def meta(self) -> dict:
        return self._read("meta.json", {})

    @property
    def plan(self) -> list[dict]:
        return self._read("plan.json", [])

    # Tables ----------------------------------------------------------------

    def original(self) -> Table:
        return load_csv((self.dir / "original.csv").read_bytes())

    def state(self) -> tuple[Table, list[ColumnInfo]]:
        """The current table and its columns, rebuilt from the original and the plan."""
        with self.lock:
            key = hashlib.sha256(json.dumps([self.plan, self.meta.get("types", {})], sort_keys=True).encode()).hexdigest()
            if self._cache and self._cache[0] == key:
                return self._cache[1], self._cache[2]
            table = ops.replay(self.original(), self.plan)
            columns = [self._override(c, table) for c in infer_table(table)]
            self._cache = (key, table, columns)
            return table, columns

    def _override(self, info: ColumnInfo, table: Table) -> ColumnInfo:
        forced = self.meta.get("types", {}).get(info.name)
        if not forced or forced == info.type:
            return info
        suggested = info.label
        info.type, info.role = forced, ROLE_OF[forced]
        info.evidence.insert(0, f"set to {info.label} by you (the values suggested {suggested})")
        if forced == "date" and not info.date_formats:
            from ..normalize import infer_date_formats

            values = [v for v in table.column(info.name) if v.strip()]
            inference = infer_date_formats(values)
            info.date_formats, info.date_ambiguous = inference.formats, bool(inference.ambiguous)
        return info

    # Overview --------------------------------------------------------------

    def overview(self) -> dict:
        table, columns = self.state()
        findings = all_findings(table, columns)
        counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("error", "anomaly", "recommendation")}
        per_column = {}
        for f in findings:
            for name in (f["column"] or "").split(", "):
                if name:
                    per_column[name] = per_column.get(name, 0) + 1
        return {
            "id": self.id,
            "meta": self.meta,
            "summary": dataset_summary(table, columns),
            "quality_score": quality_score(table, columns, findings),
            "finding_counts": counts,
            "columns": [{**c.to_dict(), "findings": per_column.get(c.name, 0)} for c in columns],
            "steps": len(self.plan),
            "pending": [p for p in self._read("proposals.json", {}).values() if p["status"] == "pending"],
            "pinned": len(self._read("pinned.json", [])),
        }

    def column(self, name: str) -> dict:
        table, columns = self.state()
        info = next((c for c in columns if c.name == name), None)
        if info is None:
            raise SessionError(f"no column {name!r}", 404)
        from .quality import column_findings

        findings = column_findings(table, info)
        return {**info.to_dict(), "findings": findings, "types": list(TYPES)}

    def findings(self) -> list[dict]:
        table, columns = self.state()
        return all_findings(table, columns)

    def set_type(self, name: str, kind: str, actor: str) -> dict:
        table, _ = self.state()
        if name not in table.columns:
            raise SessionError(f"no column {name!r}", 404)
        if kind not in TYPES and kind != "auto":
            raise SessionError(f"type must be one of {', '.join(TYPES)} or auto")
        with self.lock:
            meta = self.meta
            types = meta.setdefault("types", {})
            if kind == "auto":
                types.pop(name, None)
            else:
                types[name] = kind
            self._write("meta.json", meta)
            self._log("history.jsonl", {"event": "type_set", "actor": actor, "column": name, "type": kind})
        return self.column(name)

    # Proposals -------------------------------------------------------------

    def propose(self, spec: dict, source: str = "user") -> dict:
        table, _ = self.state()
        try:
            preview = ops.preview(table, ops.check(spec))
        except ops.OpError as error:
            raise SessionError(str(error)) from error
        pid = "p" + secrets.token_hex(4)
        proposal = {"id": pid, "status": "pending", "source": source, "created": now(), "base_steps": len(self.plan), **preview}
        with self.lock:
            proposals = self._read("proposals.json", {})
            proposals[pid] = proposal
            self._write("proposals.json", proposals)
            self._log("history.jsonl", {"event": "proposed", "id": pid, "op": spec, "source": source,
                                        "cells": preview["effect"]["cells_changed"], "rows_removed": preview["effect"]["rows_removed"]})
        return proposal

    def decide(self, pid: str, decision: str, actor: str, note: str = "") -> dict:
        if decision not in ("approve", "reject"):
            raise SessionError("decision must be approve or reject")
        if not actor.strip():
            raise SessionError("say who is deciding, for the history")
        with self.lock:
            proposals = self._read("proposals.json", {})
            proposal = proposals.get(pid)
            if proposal is None:
                raise SessionError(f"no proposal {pid!r}", 404)
            if proposal["status"] != "pending":
                raise SessionError(f"proposal {pid} was already {proposal['status']}", 409)
            if decision == "reject":
                proposal["status"] = "rejected"
                self._write("proposals.json", proposals)
                self._log("history.jsonl", {"event": "rejected", "id": pid, "op": proposal["op"], "actor": actor, "note": note})
                return {"proposal": proposal}
            table_before, columns_before = self.state()
            findings_before = all_findings(table_before, columns_before)
            # re-preview against the current table: an earlier approval may have changed it
            preview = ops.preview(table_before, proposal["op"])
            plan = self.plan + [proposal["op"]]
            self._write("plan.json", plan)
            proposal.update(status="approved", decided_by=actor, applied_effect=preview["effect"], step=len(plan))
            self._write("proposals.json", proposals)
            table, columns = self.state()
            findings_after = all_findings(table, columns)
            validation = _validation(findings_before, findings_after, table_before, table)
            self._log("history.jsonl", {"event": "applied", "id": pid, "op": proposal["op"], "actor": actor, "note": note,
                                        "step": len(plan), "cells": preview["effect"]["cells_changed"],
                                        "rows_removed": preview["effect"]["rows_removed"], "validation": validation,
                                        "table_sha256": hashlib.sha256(table.to_csv(True)).hexdigest()})
            return {"proposal": proposal, "validation": validation}

    def undo(self, actor: str) -> dict:
        with self.lock:
            plan = self.plan
            if not plan:
                raise SessionError("nothing to undo")
            removed = plan.pop()
            self._write("plan.json", plan)
            self._log("history.jsonl", {"event": "undone", "op": removed, "actor": actor, "step": len(plan) + 1})
            return {"undone": removed, "steps": len(plan)}

    # Conversation and analysis ----------------------------------------------

    def ask(self, text: str, selected: list[str], provider=None) -> dict:
        from .nl import interpret

        table, columns = self.state()
        result = interpret(text, selected or [], table, columns, provider)
        response = {"kind": result["kind"], "message": result.get("message"), "note": result.get("note"),
                    "interpreted_by": result.get("interpreted_by"), "problems": result.get("problems", [])}
        if result.get("ops"):
            response["proposals"] = [self.propose(spec, source=result.get("interpreted_by", "rules")) for spec in result["ops"]]
        if result.get("queries"):
            response["results"] = [self.query(q) for q in result["queries"]]
        if result["kind"] == "clarify":
            response["question"] = result.get("question")
            response["options"] = result.get("options", [])
        if result["kind"] == "findings" or result.get("findings"):
            wanted = set(result.get("columns") or [])
            response["findings"] = [f for f in all_findings(table, columns)
                                    if not wanted or (f["column"] and set(f["column"].split(", ")) & wanted)]
            if result.get("findings"):
                response["findings"] = [f for f in response["findings"] if f["severity"] != "recommendation"]
        if result.get("explain"):
            info = {c.name: c for c in columns}
            response["explanations"] = [_explain(info[n]) for n in result["explain"] if n in info]
        self._log("conversation.jsonl", {"request": text, "selected": selected, "kind": result["kind"],
                                         "interpreted_by": result.get("interpreted_by"),
                                         "proposals": [p["id"] for p in response.get("proposals", [])],
                                         "queries": result.get("queries", [])})
        return response

    def query(self, spec: dict) -> dict:
        table, columns = self.state()
        try:
            result = analysis.run_query(table, columns, spec)
        except analysis.QueryError as error:
            raise SessionError(str(error)) from error
        result.pop("total", None)
        from .charts import svg

        result["svg"] = svg(result.get("chart") or {})
        return result

    def pin(self, spec: dict, title: str | None = None) -> list[dict]:
        self.query(spec)  # refuse queries that do not run
        with self.lock:
            pinned = self._read("pinned.json", [])
            if spec not in [p["query"] for p in pinned]:
                pinned.append({"query": spec, "title": title, "added": now()})
            self._write("pinned.json", pinned)
            return pinned

    def unpin(self, index: int) -> list[dict]:
        with self.lock:
            pinned = self._read("pinned.json", [])
            if not 0 <= index < len(pinned):
                raise SessionError("no such dashboard item", 404)
            pinned.pop(index)
            self._write("pinned.json", pinned)
            return pinned

    def dashboard(self, where: list[dict] | None = None) -> dict:
        items = []
        for item in self._read("pinned.json", []):
            spec = dict(item["query"])
            if where:
                spec["where"] = list(spec.get("where") or []) + where
            try:
                items.append(self.query(spec))
            except SessionError as error:
                items.append({"title": item.get("title") or item["query"].get("q"), "error": str(error), "query": spec})
        table, columns = self.state()
        findings = all_findings(table, columns)
        return {"items": items, "rows": table.size, "quality_score": quality_score(table, columns, findings),
                "filters": where or [], "filterable": [c.name for c in columns if c.type in ("category", "boolean", "date")]}

    def history(self) -> list[dict]:
        return self._lines("history.jsonl")

    def conversation(self) -> list[dict]:
        return self._lines("conversation.jsonl")

    def export(self, kind: str) -> tuple[bytes, str, str]:
        table, _ = self.state()
        stem = Path(self.meta.get("filename", "data")).stem
        if kind == "csv":
            return table.to_csv(), "text/csv; charset=utf-8", f"{stem}-clean.csv"
        if kind == "xlsx":
            try:
                return table.to_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"{stem}-clean.xlsx"
            except ImportError as error:
                raise SessionError("Excel export needs openpyxl: pip install openpyxl") from error
        if kind == "plan":
            data = {"source": self.meta, "steps": self.plan}
            return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), "application/json", f"{stem}-plan.json"
        raise SessionError(f"no export {kind!r}", 404)


def _validation(before: list[dict], after: list[dict], old: Table, new: Table) -> dict:
    def count(findings, severity):
        return sum(1 for f in findings if f["severity"] == severity)

    resolved = sorted({f["title"] for f in before} - {f["title"] for f in after})
    added = sorted({f["title"] for f in after} - {f["title"] for f in before})
    return {
        "rows_before": old.size, "rows_after": new.size,
        "errors_before": count(before, "error"), "errors_after": count(after, "error"),
        "anomalies_before": count(before, "anomaly"), "anomalies_after": count(after, "anomaly"),
        "resolved": resolved[:10], "new": added[:10],
    }


def _explain(info: ColumnInfo) -> str:
    parts = [f"{info.name} looks like {info.label} data ({info.evidence[0]})."]
    if info.meaning:
        parts.append(f"From its name it may be {info.meaning}; that is a guess, please confirm.")
    if info.blank + info.null_tokens:
        parts.append(f"{info.blank + info.null_tokens} of {info.rows} rows are missing a value.")
    if info.misfits:
        parts.append(f"Values that do not fit: {', '.join(info.misfits[:4])}.")
    return " ".join(parts)


class SessionStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._open: dict[str, Session] = {}
        self.lock = threading.Lock()

    def create(self, data: bytes, filename: str, sheet: str | None = None, model: str | None = None) -> Session:
        if not data:
            raise SessionError("the upload is empty")
        try:
            table, info = read_table(data, filename, sheet)
        except Exception as error:
            raise SessionError(f"could not read the file: {error}") from error
        if not table.columns or not table.rows:
            raise SessionError("the file has no rows")
        sid = f"s-{dt.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        directory = self.root / sid
        directory.mkdir(parents=True)
        (directory / "original.csv").write_bytes(table.to_csv(with_row_ids=True))
        session = Session(directory)
        meta = {"id": sid, "created": now(), "filename": Path(filename).name, "model": model,
                "source_sha256": hashlib.sha256(data).hexdigest(), "read": info, "types": {}}
        session._write("meta.json", meta)
        session._log("history.jsonl", {"event": "uploaded", "filename": meta["filename"], "rows": table.size,
                                       "columns": len(table.columns), "source_sha256": meta["source_sha256"]})
        with self.lock:
            self._open[sid] = session
        return session

    def get(self, sid: str) -> Session:
        if not SESSION_ID.match(sid or ""):
            raise SessionError(f"no session {sid!r}", 404)
        with self.lock:
            if sid not in self._open:
                directory = self.root / sid
                if not (directory / "meta.json").is_file():
                    raise SessionError(f"no session {sid!r}", 404)
                self._open[sid] = Session(directory)
            return self._open[sid]

    def list(self, limit: int = 30) -> list[dict]:
        if not self.root.is_dir():
            return []
        found = sorted((p for p in self.root.iterdir() if (p / "meta.json").is_file()), reverse=True)[:limit]
        return [json.loads((p / "meta.json").read_text("utf-8")) for p in found]

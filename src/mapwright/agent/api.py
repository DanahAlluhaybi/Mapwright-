"""HTTP routes for the agent: sessions, requests, proposals, analysis, reports, jobs.

Mounted by ``mapwright.server``; uses its handler's helpers for JSON,
uploads and errors. Routes (all JSON unless noted):

    GET  /agent                                  the agent page (HTML)
    POST /sessions?filename=&sheet=              body: CSV or Excel → overview
    GET  /sessions                               recent sessions
    GET  /sessions/<id>                          overview: columns, findings counts, pending proposals
    GET  /sessions/<id>/columns/<name>           one column: evidence, stats, findings
    POST /sessions/<id>/columns/<name>/type      {"type", "reviewer"}: confirm or change the type
    GET  /sessions/<id>/findings                 data-quality findings
    POST /sessions/<id>/ask                      {"text", "columns": [...], "model"}
    POST /sessions/<id>/proposals                {"op": {...}} → preview
    POST /sessions/<id>/proposals/<pid>          {"decision": "approve"|"reject", "reviewer", "note"}
    POST /sessions/<id>/undo                     {"reviewer"}
    POST /sessions/<id>/query                    {"query": {...}}
    POST /sessions/<id>/pins                     {"query", "title"}
    POST /sessions/<id>/pins/<n>/remove
    GET  /sessions/<id>/history | /conversation | /dashboard.json
    GET  /sessions/<id>/report | /dashboard     HTML pages
    GET  /sessions/<id>/export/csv | xlsx | plan
    POST /jobs                                   {"kind", "source_path", "session", "idempotency_key", "threshold"}
    GET  /jobs | /jobs/<id>
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote, unquote

from .jobs import JobError, JobRunner
from .report import build_dashboard, build_report, where_from_query
from .session import SessionError, SessionStore

AGENT_PAGE = Path(__file__).resolve().parent.parent / "ui_agent.html"


class AgentAPI:
    def __init__(self, settings, read_source, provider_for):
        self.settings = settings
        self.sessions = SessionStore(Path(settings.runs_dir).parent / "sessions")
        self.jobs = JobRunner(Path(settings.runs_dir).parent / "jobs", self.sessions, read_source)
        self.provider_for = provider_for  # (model name) -> provider or None

    def route(self, h, method: str, parts: list[str], query: dict) -> bool:
        parts = [unquote(p) for p in parts]
        try:
            return self._route(h, method, parts, query)
        except (SessionError, JobError) as error:
            h._json(error.status, {"error": str(error)})
            return True

    def _route(self, h, method: str, parts: list[str], query: dict) -> bool:
        if method == "GET" and parts == ["agent"]:
            h._send(HTTPStatus.OK, AGENT_PAGE.read_bytes(), "text/html; charset=utf-8")
            return True
        if parts[:1] == ["jobs"]:
            if len(parts) == 1 and method == "POST":
                job, new = self.jobs.start(h._json_body())
                h._json(HTTPStatus.ACCEPTED if new else HTTPStatus.OK, job)
            elif len(parts) == 1:
                h._json(HTTPStatus.OK, {"jobs": self.jobs.list()})
            else:
                h._json(HTTPStatus.OK, self.jobs.get(parts[1]))
            return True
        if parts[:1] != ["sessions"]:
            return False
        if len(parts) == 1:
            if method == "GET":
                h._json(HTTPStatus.OK, {"sessions": self.sessions.list()})
            else:
                session = self.sessions.create(h._body(), query.get("filename", "data.csv"), query.get("sheet") or None)
                h._json(HTTPStatus.CREATED, session.overview())
            return True

        session = self.sessions.get(parts[1])
        rest = parts[2:]
        body = h._json_body() if method == "POST" else {}
        reviewer = str(body.get("reviewer") or "").strip()

        if not rest and method == "GET":
            h._json(HTTPStatus.OK, session.overview())
        elif rest[:1] == ["columns"] and len(rest) >= 2:
            name = "/".join(rest[1:-1]) if rest[-1] == "type" and method == "POST" else "/".join(rest[1:])
            if method == "POST":
                h._json(HTTPStatus.OK, session.set_type(name, str(body.get("type", "")), reviewer or "page"))
            else:
                h._json(HTTPStatus.OK, session.column(name))
        elif rest == ["findings"]:
            h._json(HTTPStatus.OK, {"findings": session.findings()})
        elif rest == ["ask"] and method == "POST":
            provider, note = None, None
            model = body.get("model") or None
            if model and model != "rules":
                try:
                    provider = self.provider_for(model)
                except Exception as error:
                    note = f"The AI model is not available ({error}); used the built-in rules instead."
            columns = body.get("columns") or []
            if not isinstance(columns, list):
                raise SessionError("columns must be a list of column names")
            answer = session.ask(str(body.get("text", "")), [str(c) for c in columns], provider)
            if note:
                answer["note"] = note
            h._json(HTTPStatus.OK, answer)
        elif rest == ["proposals"] and method == "POST":
            h._json(HTTPStatus.CREATED, session.propose(body.get("op"), source="page"))
        elif rest[:1] == ["proposals"] and len(rest) == 2 and method == "POST":
            h._json(HTTPStatus.OK, session.decide(rest[1], str(body.get("decision", "")), reviewer, str(body.get("note") or "")))
        elif rest == ["undo"] and method == "POST":
            h._json(HTTPStatus.OK, session.undo(reviewer or "page"))
        elif rest == ["query"] and method == "POST":
            h._json(HTTPStatus.OK, session.query(body.get("query")))
        elif rest == ["pins"] and method == "POST":
            h._json(HTTPStatus.OK, {"pinned": session.pin(body.get("query"), body.get("title"))})
        elif rest[:1] == ["pins"] and len(rest) == 3 and rest[2] == "remove" and method == "POST":
            try:
                index = int(rest[1])
            except ValueError:
                raise SessionError("no such dashboard item", 404) from None
            h._json(HTTPStatus.OK, {"pinned": session.unpin(index)})
        elif rest == ["history"]:
            h._json(HTTPStatus.OK, {"history": session.history()})
        elif rest == ["conversation"]:
            h._json(HTTPStatus.OK, {"conversation": session.conversation()})
        elif rest == ["report"]:
            h._send(HTTPStatus.OK, build_report(session).encode("utf-8"), "text/html; charset=utf-8")
        elif rest in (["dashboard"], ["dashboard.json"]):
            columns = {c["name"]: c for c in session.overview()["columns"]}
            where = where_from_query(columns, query.get("column"), query.get("value"))
            if rest == ["dashboard"]:
                h._send(HTTPStatus.OK, build_dashboard(session, where, query.get("token")).encode("utf-8"), "text/html; charset=utf-8")
            else:
                h._json(HTTPStatus.OK, session.dashboard(where))
        elif rest[:1] == ["export"] and len(rest) == 2:
            data, content_type, filename = session.export(rest[1])
            ascii_name = filename.encode("ascii", "replace").decode().replace("?", "_").replace('"', "")
            disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
            h._send(HTTPStatus.OK, data, content_type, {"Content-Disposition": disposition})
        else:
            raise SessionError(f"no route {method} /{'/'.join(parts)}", 404)
        return True

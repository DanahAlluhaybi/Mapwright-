"""A small local HTTP API, so other tools (an n8n workflow, a script) can
start a run, show it to a person and send back their decision.

    python -m mapwright serve                     http://127.0.0.1:8765

    GET  /                          a page to upload, review and download, for people
    GET  /health                    is the server up
    GET  /contracts                 contract names that can be used
    GET  /contracts/<name>          a schema's fields and rules
    POST /contracts                 JSON {"name", "base", "fields": {field: {"required", "unique",
                                    "allowed_values"}}}: save a copy with changed rules
    POST /runs?contract=customer&system=rules&filename=export.csv[&sheet=Sheet1]
                                    body: the raw CSV or Excel (.xlsx) file
    POST /runs                      JSON {"source_path", "contract", "system"}: a file on this machine
    GET  /runs                      recent runs
    GET  /runs/<id>                 summary: rows, held actions, items needing review
    GET  /runs/<id>/report          report.md
    GET  /runs/<id>/target.csv      the output; after approval, the approved output
    GET  /runs/<id>/audit           audit.jsonl
    POST /runs/<id>/review          JSON {"decision": "approve" | "reject",
                                          "approve": "all" | [ids] | "id1, id2",
                                          "reviewer": "name", "note": "..."}

Standard library only. It listens on 127.0.0.1 by default; set
MAPWRIGHT_API_TOKEN to require ``Authorization: Bearer <token>`` on every
request. Runs are synchronous: a request returns when the run is done.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from .contracts import load_contract
from .excel import ExcelError, excel_to_csv, is_excel
from .output import write_run, write_target
from .pipeline import replay, run

MAX_UPLOAD = 50 * 1024 * 1024
UI_PAGE = Path(__file__).with_name("ui.html")
RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
NAME = re.compile(r"^[A-Za-z0-9_-]+$")
SYSTEMS = ("rules", "hybrid", "llm")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _table_hash(rows: list[dict[str, str]], numbers: list[int]) -> str:
    payload = json.dumps(list(zip(numbers, rows)), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Settings:
    runs_dir: Path = Path("runs/api")
    contracts_dir: Path = Path("contracts")
    custom_dir: Path = Path("contracts/custom")  # rules saved from the page
    data_root: Path = Path(".")
    cache_dir: Path = Path("llm_cache")
    references: dict[str, frozenset[str]] = field(default_factory=dict)
    model: str | None = None
    token: str | None = None
    session_factory: object = None  # callable(system) -> LLMSession; tests replace it


def _default_session(settings: Settings):
    from .llm import CachedProvider
    from .llm.gemini import GeminiProvider
    from .llmplan import LLMSession

    return LLMSession(CachedProvider(settings.cache_dir, GeminiProvider(settings.model)))


class Service:
    """Everything the HTTP layer does, without HTTP, so it can be tested directly."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = threading.Lock()

    # Paths -----------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        if not RUN_ID.match(run_id):
            raise ApiError(HTTPStatus.NOT_FOUND, f"no run {run_id!r}")
        directory = self.settings.runs_dir / run_id
        if not (directory / "meta.json").is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, f"no run {run_id!r}")
        return directory

    def contract_path(self, name: str) -> Path:
        if not NAME.match(name or ""):
            raise ApiError(HTTPStatus.BAD_REQUEST, "contract must be a name such as 'customer'")
        for directory in (self.settings.contracts_dir, self.settings.custom_dir):
            path = directory / f"{name}.yaml"
            if path.is_file():
                return path
        raise ApiError(HTTPStatus.BAD_REQUEST, f"unknown contract {name!r}; known: {', '.join(self.contracts())}")

    def builtin_contracts(self) -> list[str]:
        return sorted(p.stem for p in self.settings.contracts_dir.glob("*.yaml"))

    def contracts(self) -> list[str]:
        custom = sorted(p.stem for p in self.settings.custom_dir.glob("*.yaml")) if self.settings.custom_dir.is_dir() else []
        return self.builtin_contracts() + [c for c in custom if c not in self.builtin_contracts()]

    # Rules -----------------------------------------------------------------

    def contract_detail(self, name: str) -> dict:
        path = self.contract_path(name)
        raw = yaml.safe_load(path.read_text("utf-8"))
        return {
            "name": name,
            "builtin": path.parent == self.settings.contracts_dir,
            "based_on": raw.get("based_on"),
            "entity": raw["entity"],
            "primary_key": raw["primary_key"],
            "fields": [
                {
                    "name": f["name"],
                    "type": f["type"],
                    "required": bool(f.get("required", False)),
                    "unique": bool(f.get("unique", False)),
                    "allowed_values": [str(v) for v in f["allowed_values"]] if f.get("allowed_values") else None,
                    "pattern": f.get("pattern"),
                    "min": f.get("min"),
                    "max": f.get("max"),
                    "description": " ".join(str(f["canonical"]).split()) if f.get("canonical") else None,
                }
                for f in raw["fields"]
            ],
        }

    def save_contract(self, body: dict) -> dict:
        """Save a copy of a contract with changed rules under a new name.

        Only ``required``, ``unique`` and ``allowed_values`` can change, and
        the primary key stays required and unique, so a saved contract is
        always one the pipeline can run.
        """
        name = str(body.get("name") or "").strip()
        if not NAME.match(name):
            raise ApiError(HTTPStatus.BAD_REQUEST, "name may use letters, digits, '-' and '_' only")
        if name in self.builtin_contracts():
            raise ApiError(HTTPStatus.BAD_REQUEST, f"{name!r} is a built-in schema; save under another name")
        base_path = self.contract_path(str(body.get("base") or ""))
        raw = yaml.safe_load(base_path.read_text("utf-8"))
        changes = body.get("fields") or {}
        if not isinstance(changes, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "fields must map field names to their rules")
        specs = {f["name"]: f for f in raw["fields"]}
        unknown = sorted(set(changes) - set(specs))
        if unknown:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"no such fields: {', '.join(unknown)}")
        for field_name, rules in changes.items():
            spec = specs[field_name]
            if not isinstance(rules, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, f"rules for {field_name} must be an object")
            for flag in ("required", "unique"):
                if flag in rules:
                    if field_name in raw["primary_key"] and not rules[flag]:
                        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field_name} is the key, so it stays {flag}")
                    spec[flag] = bool(rules[flag])
            if "allowed_values" in rules:
                values = rules["allowed_values"]
                if isinstance(values, str):
                    values = [v.strip() for v in values.split(",")]
                values = [str(v).strip() for v in values or [] if str(v).strip()]
                if values:
                    spec["allowed_values"] = values
                else:
                    spec.pop("allowed_values", None)
        # A contract finds its reference files relative to the folder above its own;
        # rewrite those paths so they still work from where the copy is saved.
        base_root = base_path.parent.parent
        new_root = self.settings.custom_dir.parent
        for spec in raw["fields"]:
            if spec.get("reference"):
                target = (base_root / spec["reference"]).resolve()
                spec["reference"] = Path(os.path.relpath(target, new_root.resolve())).as_posix()
        raw["based_on"] = raw.get("based_on") or base_path.stem
        raw["description"] = str(body.get("description") or raw.get("description") or "").strip()

        self.settings.custom_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.custom_dir / f"{name}.yaml"
        header = f"# Saved from the Mapwright page on {_now()}, based on {raw['based_on']}.\n"
        path.write_text(header + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        try:
            load_contract(path)
        except Exception as error:
            path.unlink()
            raise ApiError(HTTPStatus.BAD_REQUEST, f"those rules do not make a valid schema: {error}") from error
        return self.contract_detail(name)

    def local_source(self, source_path: str) -> Path:
        root = self.settings.data_root.resolve()
        path = (root / source_path).resolve()
        if root not in path.parents and path != root:
            raise ApiError(HTTPStatus.BAD_REQUEST, "source_path must be inside the server's data folder")
        if not path.is_file():
            raise ApiError(HTTPStatus.BAD_REQUEST, f"file not found: {source_path}")
        return path

    # Runs ------------------------------------------------------------------

    def start(
        self, data: bytes, contract_name: str, system: str = "rules", filename: str = "source.csv", sheet: str | None = None
    ) -> dict:
        if system not in SYSTEMS:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"system must be one of {', '.join(SYSTEMS)}")
        if not data:
            raise ApiError(HTTPStatus.BAD_REQUEST, "the request has no file")
        if is_excel(filename):
            try:
                data, sheet = excel_to_csv(data, sheet or None)
            except ExcelError as error:
                raise ApiError(HTTPStatus.BAD_REQUEST, str(error)) from error
        else:
            sheet = None
        contract_path = self.contract_path(contract_name)
        contract = load_contract(contract_path)

        session = None
        if system != "rules":
            factory = self.settings.session_factory or (lambda _system: _default_session(self.settings))
            try:
                session = factory(system)
            except Exception as error:  # missing key, unknown model
                raise ApiError(HTTPStatus.BAD_REQUEST, f"could not start the model: {error}") from error

        run_id = f"{dt.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        directory = self.settings.runs_dir / run_id
        directory.mkdir(parents=True)
        source = directory / "source.csv"
        source.write_bytes(data)

        references = {k: v for k, v in self.settings.references.items() if k in contract.field_names}
        try:
            outcome = run(source, contract, references, system=system, session=session)
        except Exception as error:
            if type(error).__name__ in ("LLMError", "CacheMiss"):
                raise ApiError(HTTPStatus.BAD_GATEWAY, f"the model call failed: {error}") from error
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, f"could not process the file: {error}") from error
        write_run(outcome, directory)

        meta = {
            "id": run_id,
            "created": _now(),
            "filename": Path(filename).name or "source.csv",
            "sheet": sheet,
            "contract": contract_name,
            "system": system,
            "status": "awaiting_review",
            "review": None,
        }
        if not any(s.status == "escalated" for s in outcome.steps) and not outcome.escalations:
            meta["status"] = "complete"
        self._write_meta(directory, meta)
        return self.summary(run_id)

    def _write_meta(self, directory: Path, meta: dict) -> None:
        (directory / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _meta(self, directory: Path) -> dict:
        return json.loads((directory / "meta.json").read_text("utf-8"))

    def held_actions(self, directory: Path) -> list[dict]:
        plan = json.loads((directory / "plan.json").read_text("utf-8"))
        held = []
        for step in plan["steps"]:
            if step["status"] != "escalated":
                continue
            action = step["action"]
            held.append(
                {
                    "id": action["id"],
                    "type": action["type"],
                    "targets": action.get("targets", []),
                    "origin": action.get("origin"),
                    "confidence": action.get("confidence"),
                    "why": action.get("rationale", ""),
                    "held_because": step.get("reason"),
                    "cells_changed": step["effect"].get("cells_changed", 0),
                    "rows_removed": step["effect"].get("rows_removed", 0),
                }
            )
        return held

    def summary(self, run_id: str) -> dict:
        directory = self.run_dir(run_id)
        meta = self._meta(directory)
        result = json.loads((directory / "result.json").read_text("utf-8"))
        held = self.held_actions(directory)
        rows = self._count_rows(directory / "target.csv")
        approved_rows = self._count_rows(directory / "approved.csv")
        review_items = [
            {"issue": e.get("issue"), "target": e.get("target"), "action": e.get("action")} for e in result["escalations"]
        ]
        final_rows = approved_rows if approved_rows is not None else rows
        text = f"{meta['filename']} ({meta['contract']}, {meta['system']}): {final_rows} rows, {meta['status'].replace('_', ' ')}. "
        review = meta.get("review")
        if review is None:
            text += f"{_count(len(held), 'action')} held for approval, {_count(len(review_items), 'item')} to check."
        elif review["decision"] == "reject":
            text += f"Rejected by {review['actor'].removeprefix('reviewer:')}."
        else:
            text += (
                f"Reviewed by {review['actor'].removeprefix('reviewer:')}: "
                f"{len(review['approved'])} of {_count(len(held), 'held action')} applied."
            )
        return {
            **meta,
            "rows": final_rows,
            "rows_before_review": rows,
            "held_actions": held,
            "held_ids": [h["id"] for h in held],
            "needs_review": review_items,
            "usage": result.get("usage", {}),
            "summary_text": text,
            "review_text": self.review_text(held, review_items),
        }

    @staticmethod
    def _count_rows(path: Path) -> int | None:
        if not path.is_file():
            return None
        with path.open(encoding="utf-8") as handle:
            return max(sum(1 for _ in handle) - 1, 0)

    @staticmethod
    def review_text(held: list[dict], items: list[dict]) -> str:
        """Plain lines a person can read in a form, chat message or email."""
        if not held and not items:
            return "Nothing needs a person."
        lines = []
        for h in held:
            effect = f"{h['cells_changed']} cells" + (f", {h['rows_removed']} rows removed" if h["rows_removed"] else "")
            lines.append(f"{h['id']}: {h['type']} on {', '.join(h['targets']) or 'the whole table'} ({effect}). {h['why']}".strip())
        for item in items:
            if item.get("action") in {h["id"] for h in held}:
                continue
            if item.get("action"):
                lines.append(f"Check: {item['issue']} on {item['target']}, applied by {item['action']}")
            else:
                lines.append(f"Review: {item['issue']} on {item['target']}")
        return "\n".join(lines)

    def review(self, run_id: str, body: dict) -> dict:
        directory = self.run_dir(run_id)
        with self.lock:
            meta = self._meta(directory)
            if meta["review"] is not None:
                raise ApiError(HTTPStatus.CONFLICT, f"run {run_id} was already reviewed ({meta['status']})")
            decision = body.get("decision", "approve")
            if decision not in ("approve", "reject"):
                raise ApiError(HTTPStatus.BAD_REQUEST, "decision must be 'approve' or 'reject'")
            reviewer = str(body.get("reviewer") or "").strip()
            if not reviewer:
                raise ApiError(HTTPStatus.BAD_REQUEST, "reviewer is required, so the audit log says who decided")
            held_ids = [h["id"] for h in self.held_actions(directory)]

            approved: list[str] = []
            if decision == "approve":
                wanted = body.get("approve", [])
                if wanted == "all":
                    approved = held_ids
                else:
                    if isinstance(wanted, str):
                        wanted = [part for part in re.split(r"[\s,]+", wanted) if part]
                    unknown = sorted(set(wanted) - set(held_ids))
                    if unknown:
                        raise ApiError(
                            HTTPStatus.BAD_REQUEST,
                            f"not held in this run: {', '.join(unknown)}; held: {', '.join(held_ids) or 'none'}",
                        )
                    approved = [i for i in held_ids if i in set(wanted)]

            event = {
                "event": "review",
                "at": _now(),
                "actor": f"reviewer:{reviewer}",
                "decision": decision,
                "approved": approved,
                "kept_held": [i for i in held_ids if i not in approved] if decision == "approve" else held_ids,
                "note": str(body.get("note") or ""),
            }
            if decision == "approve":
                contract = load_contract(self.contract_path(meta["contract"]))
                plan = json.loads((directory / "plan.json").read_text("utf-8"))
                rows, numbers = replay(plan, directory / "source.csv", contract, set(approved))
                write_target(directory / "approved.csv", contract.field_names, rows, numbers)
                event["output_rows"] = len(rows)
                event["target_sha256"] = _table_hash(rows, numbers)
            with (directory / "audit.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

            meta["status"] = "approved" if decision == "approve" else "rejected"
            meta["review"] = {k: event[k] for k in ("at", "actor", "decision", "approved", "note")}
            self._write_meta(directory, meta)
        return self.summary(run_id)

    def list_runs(self, limit: int = 50) -> list[dict]:
        if not self.settings.runs_dir.is_dir():
            return []
        found = sorted((p for p in self.settings.runs_dir.iterdir() if (p / "meta.json").is_file()), reverse=True)
        return [self._meta(p) for p in found[:limit]]

    def file(self, run_id: str, name: str) -> tuple[bytes, str]:
        directory = self.run_dir(run_id)
        if name == "target.csv":
            approved = directory / "approved.csv"
            path = approved if approved.is_file() else directory / "target.csv"
            return path.read_bytes(), "text/csv; charset=utf-8"
        if name == "report":
            return (directory / "report.md").read_bytes(), "text/markdown; charset=utf-8"
        if name == "audit":
            return (directory / "audit.jsonl").read_bytes(), "application/x-ndjson; charset=utf-8"
        raise ApiError(HTTPStatus.NOT_FOUND, f"no file {name!r}")


def make_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Mapwright"

        def log_message(self, format, *args):  # noqa: A002 - signature from the base class
            if os.environ.get("MAPWRIGHT_API_QUIET") != "1":
                super().log_message(format, *args)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"), "application/json")

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_UPLOAD:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "file is larger than 50 MB")
            return self.rfile.read(length) if length else b""

        def _json_body(self) -> dict:
            raw = self._body()
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError as error:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"body is not JSON: {error}") from error
            if not isinstance(data, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
            return data

        def _authorized(self) -> bool:
            token = service.settings.token
            if not token:
                return True
            return secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}")

        def _handle(self, method: str) -> None:
            try:
                url = urlparse(self.path)
                parts = [p for p in url.path.split("/") if p]
                if method == "GET" and not parts:
                    # The page holds no data; every call it makes still needs the token.
                    return self._send(HTTPStatus.OK, UI_PAGE.read_bytes(), "text/html; charset=utf-8")
                if not self._authorized():
                    raise ApiError(HTTPStatus.UNAUTHORIZED, "missing or wrong API token")
                query = {k: v[-1] for k, v in parse_qs(url.query).items()}
                self._route(method, parts, query)
            except ApiError as error:
                self._json(error.status, {"error": error.message})
            except Exception as error:  # keep the server up; say what failed
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(error).__name__}: {error}"})

        def _route(self, method: str, parts: list[str], query: dict) -> None:
            if method == "GET" and parts == ["health"]:
                return self._json(HTTPStatus.OK, {"ok": True})
            if parts == ["contracts"]:
                if method == "GET":
                    return self._json(HTTPStatus.OK, {"contracts": service.contracts()})
                return self._json(HTTPStatus.CREATED, service.save_contract(self._json_body()))
            if method == "GET" and len(parts) == 2 and parts[0] == "contracts":
                return self._json(HTTPStatus.OK, service.contract_detail(parts[1]))
            if parts == ["runs"]:
                if method == "GET":
                    return self._json(HTTPStatus.OK, {"runs": service.list_runs()})
                if self.headers.get("Content-Type", "").startswith("application/json"):
                    body = self._json_body()
                    path = service.local_source(str(body.get("source_path", "")))
                    summary = service.start(
                        path.read_bytes(), str(body.get("contract", "")), body.get("system", "rules"), path.name
                    )
                else:
                    summary = service.start(
                        self._body(),
                        query.get("contract", ""),
                        query.get("system", "rules"),
                        query.get("filename", "source.csv"),
                        query.get("sheet") or None,
                    )
                return self._json(HTTPStatus.CREATED, summary)
            if len(parts) == 2 and parts[0] == "runs" and method == "GET":
                return self._json(HTTPStatus.OK, service.summary(parts[1]))
            if len(parts) == 3 and parts[0] == "runs":
                if method == "POST" and parts[2] == "review":
                    return self._json(HTTPStatus.OK, service.review(parts[1], self._json_body()))
                if method == "GET":
                    body, content_type = service.file(parts[1], parts[2])
                    return self._send(HTTPStatus.OK, body, content_type)
            raise ApiError(HTTPStatus.NOT_FOUND, f"no route {method} /{'/'.join(parts)}")

        def do_GET(self):  # noqa: N802 - name from the base class
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

    return Handler


def make_server(settings: Settings, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(Service(settings)))


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None:
    server = make_server(settings, host, port)
    url = f"http://{host}:{server.server_address[1]}"
    print(f"Mapwright on {url}  (runs in {settings.runs_dir}). Press Ctrl+C to stop.")
    if open_browser:
        import webbrowser

        threading.Timer(0.5, webbrowser.open, [url]).start()
    if host not in ("127.0.0.1", "localhost") and not settings.token:
        print("Warning: listening beyond this machine without MAPWRIGHT_API_TOKEN set")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

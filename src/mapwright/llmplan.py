"""The LLM side of planning: column mapping, value actions and revisions.

The model only ever proposes. Every proposal is parsed into one of the
typed actions and checked here before it can run: unknown columns, values
that were not shown, targets outside the contract and date formats that
read nothing are rejected and counted as invalid. What passes goes through
the same executor, postconditions and approval policy as a rules action.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .actions import (
    Action,
    MapValues,
    NormalizePhone,
    NormalizeText,
    ParseDate,
    ParseNumber,
    SetNull,
    Workspace,
)
from .contracts import Contract, Field, check_value
from .dictionaries import unit_tokens, value_dictionary
from .ingest import SourceTable
from .llm import LLMError, LLMRequest, Provider, Usage, prompts
from .mapping import ColumnMapping, MappingResult, _mapping_issue
from .normalize import (
    DATE_FORMATS,
    FORMATS_BY_PATTERN,
    _swap_day_month,
    is_email,
    match_key,
    normalize_phone,
    parse_date,
    parse_number,
)
from .profile import ColumnProfile

FORMAT_RULES = {"type", "pattern", "allowed_values", "reference"}


@dataclass
class Invalid:
    """A proposal that could not become a valid action."""

    type: str
    target: str | None
    reason: str
    revision_of: str | None = None


@dataclass
class Chain:
    """One proposed action and its revisions, scored as a single action."""

    type: str
    target: str
    attempts: list = field(default_factory=list)  # Step or Invalid, in order
    revisions: int = 0
    resolved: bool = False

    def entry(self) -> dict:
        last = self.attempts[-1]
        ran = not isinstance(last, Invalid)
        return {
            "id": last.action.id if ran else None,
            "type": last.action.type if ran else self.type,
            "origin": "llm",
            "target": self.target,
            "valid": ran,
            "executed": ran,
            "succeeded": ran and last.status != "failed" and self.resolved,
            "revisions": self.revisions,
            "status": last.status if ran else "invalid",
        }


@dataclass
class LLMSession:
    provider: Provider
    sample: int = 0
    repair: bool = True
    max_revisions: int = 2
    usage: Usage = field(default_factory=Usage)
    invalid: list[Invalid] = field(default_factory=list)
    chains: list[Chain] = field(default_factory=list)

    @property
    def model(self) -> str:
        return self.provider.model

    def ask(self, prompt: str, version: str) -> dict | None:
        request = LLMRequest(
            prompt=prompt,
            system=prompts.SYSTEM,
            model=self.provider.model,
            prompt_version=version,
            sample=self.sample,
        )
        # A request that cannot be answered (bad key, retired model, rate limit
        # after retries, missing cache entry) stops the run: carrying on would
        # score a run in which the model never took part.
        response = self.provider.complete(request)
        self.usage.record(request, response)
        try:
            answer = response.json()
        except LLMError as error:
            self.invalid.append(Invalid("response", None, str(error)))
            return None
        return answer if isinstance(answer, dict) else None


def _confidence(item: dict) -> float:
    try:
        value = float(item.get("confidence", 0.5))
    except (TypeError, ValueError):
        return 0.0
    return min(max(value, 0.0), 1.0)


# Mapping -----------------------------------------------------------------


def llm_mapping(
    session: LLMSession,
    source: SourceTable,
    profiles: dict[str, ColumnProfile],
    contract: Contract,
    current: MappingResult,
) -> MappingResult:
    """Ask the model about the columns and fields the current mapping left open."""
    used = {c for m in current.mappings for c in m.sources}
    columns = [h for h in source.header if h not in used]
    open_fields = [f for f in contract.field_names if not current.for_target(f)]
    result = MappingResult(mappings=list(current.mappings))
    if columns and open_fields:
        views = [prompts.column_view(h, profiles[h], source.column(h)) for h in columns]
        taken = {m.target: m.sources for m in current.mappings}
        answer = session.ask(prompts.mapping_prompt(contract, views, open_fields, taken), prompts.MAPPING_VERSION)
        for item in (answer or {}).get("mappings", []) if isinstance(answer, dict) else []:
            mapped = _check_mapping(item, columns, open_fields, result)
            if isinstance(mapped, Invalid):
                session.invalid.append(mapped)
            else:
                result.mappings.append(mapped)

    order = {name: i for i, name in enumerate(contract.field_names)}
    result.mappings.sort(key=lambda m: order[m.target])
    for m in result.mappings:
        m.issue = _mapping_issue(m.sources, m.target)
    used = {c for m in result.mappings for c in m.sources}
    result.dropped = [h for h in source.header if h not in used]
    result.missing = [t for t in contract.field_names if not result.for_target(t)]
    return result


def _check_mapping(item: object, columns: list[str], open_fields: list[str], result: MappingResult):
    if not isinstance(item, dict):
        return Invalid("map_column", None, "mapping entry is not an object")
    sources, target = item.get("source"), item.get("target")
    if isinstance(sources, str):
        sources = [sources]
    if not isinstance(sources, list) or not sources or not all(isinstance(s, str) for s in sources):
        return Invalid("map_column", target, "source must be a list of column names")
    used = {c for m in result.mappings for c in m.sources}
    unknown = [s for s in sources if s not in columns]
    if unknown:
        return Invalid("map_column", target, f"unknown or unavailable columns {unknown}")
    if used & set(sources) or len(set(sources)) != len(sources):
        return Invalid("map_column", target, "a column is used twice")
    if target not in open_fields or result.for_target(target):
        return Invalid("map_column", target, f"{target!r} is not an open target field")
    return ColumnMapping(list(sources), target, "llm", round(_confidence(item), 2))


# Value actions -------------------------------------------------------------


def closed_values(contract: Contract, f: Field) -> set[str] | None:
    dictionary = value_dictionary(contract, f)
    return dictionary.canonical if dictionary else None


def _issue_for_map(contract: Contract, f: Field) -> str:
    dictionary = value_dictionary(contract, f)
    return dictionary.issue if dictionary else "V1"


def _date_action(f: Field, formats: list[str], values: list[str]) -> ParseDate | str:
    known = [p for p in formats if isinstance(p, str) and "%" in p]
    if not known:
        return "no usable date formats"
    for pattern in known:
        if not any(parse_date(v, pattern) for v in values):
            return f"format {pattern!r} reads none of the values"
    ambiguous = False
    numeric = False
    has_time = False
    for pattern in known:
        year = max(pattern.find("%Y"), pattern.find("%y"))
        day_month = "%d" in pattern and "%m" in pattern and "%b" not in pattern
        if day_month and year > max(pattern.find("%d"), pattern.find("%m")):
            numeric = True
            twin = _swap_day_month(pattern)
            if {v for v in values if parse_date(v, pattern)} == {v for v in values if parse_date(v, twin)}:
                ambiguous = True
        fmt = FORMATS_BY_PATTERN.get(pattern)
        has_time = has_time or (fmt.has_time if fmt else "%H" in pattern)
    if numeric:
        issue, scope = "V2", "column"
    elif has_time:
        issue, scope = "M4", "column"
    else:
        issue, scope = "S9", "cell"
    return ParseDate(target=f.name, formats=known, issue=issue, scope=scope, ambiguous=ambiguous)


def build_action(contract: Contract, f: Field, item: object, values: list[str], kinds: list[str]) -> Action | Invalid:
    """Turn one proposed action into a typed action, or say why it is invalid."""
    if not isinstance(item, dict):
        return Invalid("unknown", f.name, "action is not an object")
    kind = item.get("type")
    if kind not in kinds:
        return Invalid(str(kind), f.name, f"action type {kind!r} is not allowed for this field")
    shown = {v: v for v in values} | {v.strip(): v for v in values}
    meta = {"origin": "llm", "confidence": _confidence(item), "rationale": str(item.get("reason", ""))[:200]}

    if kind == "map_values":
        mapping = item.get("mapping")
        if not isinstance(mapping, dict) or not mapping:
            return Invalid(kind, f.name, "mapping must be a non-empty object")
        allowed = closed_values(contract, f) or set()
        clean = {}
        for raw, target in mapping.items():
            if raw not in shown:
                return Invalid(kind, f.name, f"value {raw!r} was not in the values shown")
            if str(target) not in allowed:
                return Invalid(kind, f.name, f"{target!r} is not an allowed value")
            clean[match_key(raw)] = str(target)
        return MapValues(target=f.name, mapping=clean, issue=_issue_for_map(contract, f), **meta)

    if kind == "set_null":
        tokens = item.get("tokens")
        if not isinstance(tokens, list) or not tokens:
            return Invalid(kind, f.name, "tokens must be a non-empty list")
        missing = [t for t in tokens if t not in shown]
        if missing:
            return Invalid(kind, f.name, f"tokens {missing} were not in the values shown")
        cleaned = [shown[t].strip() for t in tokens]
        # Blanking a value that reads as a phone, an email, a number or a date
        # loses data, so a person decides; placeholders like "N/A" are blanked.
        real = [t for t in cleaned if looks_like_data(t)]
        return SetNull(target=f.name, tokens=cleaned, ambiguous=bool(real), **meta)

    if kind == "parse_date":
        formats = item.get("formats")
        if isinstance(formats, str):
            formats = [formats]
        action = _date_action(f, formats if isinstance(formats, list) else [], values)
        if isinstance(action, str):
            return Invalid(kind, f.name, action)
        action.origin, action.confidence, action.rationale = meta["origin"], meta["confidence"], meta["rationale"]
        return action

    if kind == "parse_number":
        units = item.get("units") or {}
        if not isinstance(units, dict):
            return Invalid(kind, f.name, "units must be an object")
        codes = set(unit_tokens(contract).values())
        bad = {k: v for k, v in units.items() if str(v) not in codes}
        if bad:
            return Invalid(kind, f.name, f"unknown currency codes {bad}")
        currency = "currency" if "currency" in contract.field_names and f.name != "currency" else None
        return ParseNumber(
            target=f.name,
            units={str(k): str(v) for k, v in units.items()} | {c: c for c in codes},
            currency_field=currency,
            **meta,
        )

    if kind == "normalize_text":
        case = item.get("case", "keep")
        if case not in ("keep", "upper", "lower", "title_if_uniform"):
            return Invalid(kind, f.name, f"unknown case {case!r}")
        return NormalizeText(target=f.name, case=case, **meta)

    return NormalizePhone(target=f.name, **meta)


def looks_like_data(text: str) -> bool:
    return (
        is_email(text.lower())
        or normalize_phone(text) is not None
        or parse_number(text) is not None
        or any(parse_date(text, f.pattern) for f in DATE_FORMATS)
    )


def still_wrong(contract: Contract, f: Field, ws: Workspace, rows: list[int] | None) -> dict[int, str]:
    """Rows (among ``rows``, or all) whose value still breaks a format rule."""
    values = ws.values(f.name)
    numbers = rows if rows is not None else list(values)
    return {
        n: values[n]
        for n in numbers
        if n in values and values[n].strip() and set(check_value(contract, f, values[n])) & FORMAT_RULES
    }


def _coverage_gaps(action: Action, wrong: dict[int, str], values: list[str]) -> list[str]:
    """Values a parsing action was shown but still could not read."""
    if action.type not in ("parse_date", "parse_number"):
        return []
    shown = set(values)
    left = sorted({v for v in wrong.values() if v in shown})
    return [f"still unreadable: {v!r}" for v in left[:20]]


def _needs_revision(action: Action, step, wrong: dict[int, str], values: list[str]) -> list[str]:
    """Why a run action should be revised, or nothing if it did its job.

    A held action is not a failure: it waits for a person, and asking the
    model again would only propose it again.
    """
    if step.status == "failed":
        return step.problems
    if step.status == "escalated":
        return []
    return _coverage_gaps(action, wrong, values)


@dataclass
class FieldJob:
    """One field to ask the model about, with the values it will be shown."""

    field: Field
    rows: list[int] | None
    values: list[str]
    kinds: list[str]
    request: dict


def field_job(executor, contract: Contract, f: Field, rows: list[int] | None, context: str) -> FieldJob | None:
    """``rows`` limits the values to those rows (the hybrid sends only what the
    rules left); None shows a bounded sample of the whole column."""
    current = executor.ws.values(f.name)
    if rows is None:
        values = prompts.value_sample(list(current.values()), f.name)
    else:
        values = sorted({current[n] for n in rows if n in current and current[n].strip()})[: prompts.MAX_FREQUENT]
    if not values:
        return None
    kinds = prompts.allowed_action_types(f, closed_values(contract, f) is not None)
    return FieldJob(f, rows, values, kinds, prompts.field_request(contract, f, values, kinds, context))


def fix_fields(session: LLMSession, executor, contract: Contract, jobs: list[FieldJob]) -> None:
    """Ask once about every field in ``jobs``, then run each field's actions, revising failures."""
    jobs = [job for job in jobs if job is not None]
    if not jobs:
        return
    answer = session.ask(prompts.values_prompt(contract, [job.request for job in jobs]), prompts.VALUES_VERSION)
    per_field = answer.get("fields", {}) if isinstance(answer, dict) else {}
    if not isinstance(per_field, dict):
        per_field = {}
    for job in jobs:
        entry = per_field.get(job.field.name, {})
        proposals = entry.get("actions", []) if isinstance(entry, dict) else []
        _run_proposals(session, executor, contract, job, proposals if isinstance(proposals, list) else [])


def _run_proposals(session: LLMSession, executor, contract: Contract, job: FieldJob, proposals: list) -> None:
    f, rows, values, kinds, ws = job.field, job.rows, job.values, job.kinds, executor.ws
    for item in proposals:
        action = build_action(contract, f, item, values, kinds)
        chain = Chain(str(item.get("type")) if isinstance(item, dict) else "unknown", f.name)
        while True:
            if isinstance(action, Invalid):
                chain.attempts.append(action)
                problems = [action.reason]
            else:
                step = executor.run(action)
                wrong = still_wrong(contract, f, ws, rows)
                if step is None:  # the action would change nothing
                    problems = _coverage_gaps(action, wrong, values)
                    if problems:
                        chain.attempts.append(Invalid(action.type, f.name, "changed nothing"))
                else:
                    chain.attempts.append(step)
                    problems = _needs_revision(action, step, wrong, values)
            if not problems or not session.repair or chain.revisions >= session.max_revisions:
                break
            chain.revisions += 1
            wrong = sorted(set(still_wrong(contract, f, ws, rows).values()))
            revised = session.ask(
                prompts.revise_prompt(job.request, item if isinstance(item, dict) else {}, problems, wrong),
                prompts.REVISE_VERSION,
            )
            items = revised.get("actions", []) if isinstance(revised, dict) else []
            if not items:
                break
            item = items[0]
            action = build_action(contract, f, item, values, kinds)
        chain.resolved = not problems
        if chain.attempts:
            session.chains.append(chain)


def describe_mapping(mapping: MappingResult, f: Field) -> str:
    m = mapping.for_target(f.name)
    return f"source column(s): {json.dumps(m.sources, ensure_ascii=False)}" if m else ""

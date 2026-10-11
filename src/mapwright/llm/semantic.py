"""Turning model answers into typed actions, with a bounded repair loop.

The layer asks the two questions in ``tasks``, converts the answers into
``MapColumn``, ``MapValues`` and ``SetNull`` actions, and runs them
through the pipeline's executor. When an action fails its postconditions,
the rejected replacements and the reasons go back to the model for a
corrected answer, at most ``max_revisions`` times. Whatever is still
rejected after that is left unchanged and sent to a person.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from ..actions import Action, MapValues, SetNull
from ..contracts import Contract, Field, check_value
from ..dictionaries import value_dictionary
from ..mapping import ColumnMapping, MappingResult, _mapping_issue
from ..normalize import match_key
from ..planner import Context, Finding
from ..profile import ColumnProfile
from ..validate import FORMAT_RULES
from . import tasks
from .provider import Provider, Request, Usage

RETRY_NOTE = (
    "\n\nYour previous answer could not be used: it was not one JSON object in the required shape. "
    "Answer again with only that JSON object."
)
# Findings about whole rows or whole columns are never cleared by a value fix.
ROW_LEVEL_ISSUES = {"S2", "S3", "S5", "S7", "S8", "M6"}


def broken_rules(contract: Contract, f: Field, value: str) -> set[str]:
    return set(check_value(contract, f, value)) & FORMAT_RULES


@dataclass
class LLMSettings:
    provider: Provider
    repair: bool = True
    max_revisions: int = 2
    max_values_per_field: int = 60
    min_confidence: float = 0.8


@dataclass
class SemanticLayer:
    settings: LLMSettings
    usage: Usage = field(default_factory=Usage)

    @property
    def model(self) -> str:
        return self.settings.provider.model

    def _ask(self, request: Request, parse):
        """Ask once, retry once if the answer cannot be parsed, else give up."""
        for attempt in range(2):
            sent = (
                request
                if attempt == 0
                else Request(request.task, request.system, request.prompt + RETRY_NOTE, request.prompt_version)
            )
            response = self.settings.provider.complete(sent)
            self.usage.add(response)
            try:
                return parse(response.text)
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        self.usage.invalid_responses += 1
        return None

    # Mapping -------------------------------------------------------------

    def extend_mapping(
        self,
        contract: Contract,
        profiles: dict[str, ColumnProfile],
        mapping: MappingResult,
    ) -> tuple[list[Finding], int]:
        """Ask about columns nobody mapped and fields nobody filled.

        Confident answers join the mapping; the rest are reported for a
        person. Returns those findings and the number of invalid entries.
        """
        if not mapping.dropped or not mapping.missing:
            return [], 0
        fields = [contract.field(name) for name in mapping.missing]
        request = tasks.mapping_request(contract, [profiles[h] for h in mapping.dropped], fields)
        parsed = self._ask(request, lambda text: tasks.parse_mapping(text, mapping.dropped, mapping.missing))
        if parsed is None:
            return [], 1
        proposals, invalid = parsed

        findings = []
        for p in proposals:
            if p.column not in mapping.dropped or p.target not in mapping.missing:
                continue
            issue = _mapping_issue([p.column], p.target) or "M1"
            if p.confidence < self.settings.min_confidence:
                findings.append(
                    Finding(issue, p.target, note=f"suggested mapping {p.column} -> {p.target} ({p.confidence:.2f})")
                )
                continue
            mapping.mappings.append(ColumnMapping([p.column], p.target, "llm", round(p.confidence, 2), issue))
            mapping.dropped.remove(p.column)
            mapping.missing.remove(p.target)

        order = {name: i for i, name in enumerate(contract.field_names)}
        mapping.mappings.sort(key=lambda m: order[m.target])
        return findings, invalid

    # Values --------------------------------------------------------------

    def _invalid_values(self, ctx: Context, skip: set[str]) -> dict[str, list[tuple[str, int]]]:
        values = {}
        for f in ctx.contract.fields:
            if f.name in ctx.missing or f.name in skip:
                continue
            counts = Counter(
                v for v in ctx.ws.values(f.name).values() if v.strip() and broken_rules(ctx.contract, f, v)
            )
            if counts:
                values[f.name] = counts.most_common(self.settings.max_values_per_field)
        return values

    def _issue(self, contract: Contract, f: Field, kind: str) -> str:
        if kind == "missing":
            return "V3"
        if kind == "synonym":
            dictionary = value_dictionary(contract, f)
            return dictionary.issue if dictionary else "V1"
        return "V4" if f.type == "decimal" else "S9"

    def _actions(self, contract: Contract, proposals: list[tasks.ValueProposal]) -> list[Action]:
        groups: dict[tuple, list[tasks.ValueProposal]] = defaultdict(list)
        for p in proposals:
            if p.kind == "unknown":
                continue
            confident = p.confidence >= self.settings.min_confidence
            groups[
                (p.field, p.kind == "missing", self._issue(contract, contract.field(p.field), p.kind), confident)
            ].append(p)

        actions: list[Action] = []
        for (name, is_missing, issue, _), items in sorted(groups.items()):
            confidence = min(p.confidence for p in items)
            if is_missing:
                actions.append(
                    SetNull(
                        target=name,
                        tokens=sorted(p.value for p in items),
                        issue=issue,
                        origin="llm",
                        confidence=confidence,
                        rationale="the model read these as placeholders for no data",
                    )
                )
            else:
                actions.append(
                    MapValues(
                        target=name,
                        mapping={match_key(p.value): p.replacement for p in items},
                        issue=issue,
                        origin="llm",
                        confidence=confidence,
                        rationale="the model's reading of values the rules did not know",
                    )
                )
        return actions

    def _rows_with(self, ctx: Context, name: str, keys: set[str]) -> list[int]:
        return sorted(n for n, v in ctx.ws.values(name).items() if v and match_key(v) in keys)

    def _revise(self, ctx: Context, action: MapValues, bad: dict[str, str], raw: dict[str, str]) -> MapValues | None:
        f = ctx.contract.field(action.target)
        rejected = [
            (raw.get(key, key), replacement, sorted(broken_rules(ctx.contract, f, replacement)))
            for key, replacement in bad.items()
        ]
        asked = {action.target: {value for value, _, _ in rejected}}
        parsed = self._ask(
            tasks.revision_request(ctx.contract, action.target, rejected), lambda text: tasks.parse_values(text, asked)
        )
        mapping = {k: v for k, v in action.mapping.items() if k not in bad}
        confidence = action.confidence
        if parsed is not None:
            for p in parsed[0]:
                if p.kind in ("synonym", "format") and p.replacement:
                    mapping[match_key(p.value)] = p.replacement
                    confidence = min(confidence or 1.0, p.confidence)
        if not mapping:
            return None
        return MapValues(
            target=action.target,
            mapping=mapping,
            issue=action.issue,
            origin="llm",
            confidence=confidence,
            rationale=action.rationale,
        )

    def _run_with_repair(
        self, ctx: Context, executor, action: Action, raw: dict[str, str], findings: list[Finding]
    ) -> None:
        revisions = 0
        while True:
            step = executor.run(action)
            if step is None or step.status != "failed" or not isinstance(action, MapValues):
                if step is not None:
                    step.revisions = revisions
                return
            f = ctx.contract.field(action.target)
            bad = {k: v for k, v in action.mapping.items() if broken_rules(ctx.contract, f, v)}
            if not bad:
                step.revisions = revisions
                return

            if self.settings.repair and revisions < self.settings.max_revisions:
                executor.steps.pop()
                revised = self._revise(ctx, action, bad, raw)
                revisions += 1
                if revised is None:
                    findings.append(
                        Finding(
                            action.issue,
                            action.target,
                            self._rows_with(ctx, action.target, set(bad)),
                            note="no valid replacement after revision",
                        )
                    )
                    return
                action = revised
                continue

            # Out of revisions, or no repair loop: keep the failure on record,
            # apply what was valid, and send the rest to a person.
            step.revisions = revisions
            trimmed = {k: v for k, v in action.mapping.items() if k not in bad}
            if trimmed:
                executor.run(
                    MapValues(
                        target=action.target,
                        mapping=trimmed,
                        issue=action.issue,
                        origin="llm",
                        confidence=action.confidence,
                        rationale=action.rationale,
                    )
                )
            findings.append(
                Finding(
                    action.issue,
                    action.target,
                    self._rows_with(ctx, action.target, set(bad)),
                    note="the model's replacement breaks the contract",
                )
            )
            return

    def repair_values(self, ctx: Context, executor, skip: set[str]) -> tuple[list[Finding], int]:
        values = self._invalid_values(ctx, skip)
        if not values:
            return [], 0
        asked = {name: {v for v, _ in items} for name, items in values.items()}
        parsed = self._ask(tasks.values_request(ctx.contract, values), lambda text: tasks.parse_values(text, asked))
        if parsed is None:
            return [], 1
        proposals, invalid = parsed
        raw = {match_key(p.value): p.value for p in proposals}

        findings: list[Finding] = []
        for action in self._actions(ctx.contract, proposals):
            self._run_with_repair(ctx, executor, action, raw, findings)
        return findings, invalid


def refresh_findings(ctx: Context, findings: list[Finding]) -> list[Finding]:
    """Drop rows from value findings whose cells are now valid."""
    kept = []
    for finding in findings:
        if finding.rows is None or finding.target is None or finding.issue in ROW_LEVEL_ISSUES:
            kept.append(finding)
            continue
        f = ctx.contract.field(finding.target)
        rows = [n for n in finding.rows if n in ctx.ws.rows and broken_rules(ctx.contract, f, ctx.ws.rows[n][f.name])]
        if rows:
            finding.rows = rows
            kept.append(finding)
    return kept


def remaining_invalid(ctx: Context, findings: list[Finding], escalated_columns: set[str]) -> list[Finding]:
    """Cells that still break the contract and nobody has flagged yet.

    They are sent to a person without being counted as a detected issue:
    the system knows the value is wrong, not what kind of problem it is.
    """
    covered = defaultdict(set)
    for finding in findings:
        if finding.target and finding.rows is not None:
            covered[finding.target].update(finding.rows)
    extra = []
    for f in ctx.contract.fields:
        if f.name in ctx.missing or f.name in escalated_columns:
            continue
        rows = [
            n
            for n, v in ctx.ws.values(f.name).items()
            if v and broken_rules(ctx.contract, f, v) and n not in covered[f.name]
        ]
        if rows:
            extra.append(Finding("", f.name, rows, note="still breaks the contract", report=False))
    return extra

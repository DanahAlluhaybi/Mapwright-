"""The fixed pipeline: ingest, profile, map, plan and execute, decide.

Every action is computed, checked against its postconditions and passed
through the approval policy before anything is committed. The run keeps
a step for every action, so the plan can be replayed exactly.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .actions import (
    Action,
    CombineColumns,
    DropColumn,
    Effect,
    MapColumn,
    Workspace,
    action_from_dict,
)
from .contracts import Contract
from .ingest import SourceTable, read_source
from .mapping import MappingResult, map_columns
from .planner import Context, Finding, plan_field, plan_missing_columns, plan_rows
from .policy import Decision, decide
from .profile import ColumnProfile, profile_table
from .validate import postconditions

PLAN_VERSION = 1
STRUCTURAL = {"map_column", "combine_columns", "drop_column"}


@dataclass
class Step:
    action: Action
    effect: Effect
    decision: Decision | None
    status: str  # applied | escalated | failed
    problems: list[str] = field(default_factory=list)

    @property
    def committed(self) -> bool:
        return self.status == "applied"

    def to_dict(self) -> dict:
        return {
            "action": self.action.to_dict(),
            "status": self.status,
            "decision": self.decision.kind if self.decision else None,
            "reason": self.decision.reason if self.decision else None,
            "effect": self.effect.summary(),
            "problems": self.problems,
        }


@dataclass
class RunOutcome:
    contract: Contract
    source: SourceTable
    profiles: dict[str, ColumnProfile]
    mapping: MappingResult
    steps: list[Step]
    findings: list[Finding]
    rows: list[dict[str, str]]
    source_rows: list[int]
    seconds: float

    @property
    def escalations(self) -> list[dict]:
        items = [item for step in self.steps if step.decision for item in step.decision.escalations]
        for finding in self.findings:
            if finding.escalate:
                item = {"issue": finding.issue, "target": finding.target}
                if finding.groups is not None:
                    item["groups"] = finding.groups
                elif finding.rows is not None:
                    item["rows"] = finding.rows
                items.append(item)
        return items

    def detections(self) -> list[dict]:
        return collect_detections(self)

    def result(self) -> dict:
        """The run result in the format every evaluated system writes."""
        return {
            "mapping": [{"source": m.sources, "target": m.target} for m in self.mapping.mappings],
            "dropped_columns": self.mapping.dropped,
            "detections": self.detections(),
            "actions": [
                {
                    "id": s.action.id,
                    "type": s.action.type,
                    "origin": s.action.origin,
                    "valid": True,
                    "executed": True,
                    "succeeded": s.status != "failed",
                    "revisions": 0,
                    "status": s.status,
                }
                for s in self.steps
            ],
            "escalations": self.escalations,
            "usage": {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": round(self.seconds, 3)},
        }

    def plan(self) -> dict:
        return {
            "version": PLAN_VERSION,
            "entity": self.contract.entity,
            "ingest": self.source.settings(),
            "mapping": [m.to_dict() for m in self.mapping.mappings],
            "dropped_columns": self.mapping.dropped,
            "missing_columns": self.mapping.missing,
            "steps": [s.to_dict() for s in self.steps],
            "findings": [f.to_dict() for f in self.findings],
            "profiles": {name: p.to_dict() for name, p in self.profiles.items()},
        }


class Executor:
    def __init__(self, contract: Contract, ws: Workspace, policy_config: dict | None = None):
        self.contract = contract
        self.ws = ws
        self.policy_config = policy_config
        self.steps: list[Step] = []

    def run(self, action: Action) -> Step | None:
        """Compute, check, decide and maybe commit one action.

        Value actions that would change nothing are not recorded.
        """
        action.id = f"a{len(self.steps) + 1}"
        try:
            effect = action.compute(self.ws)
        except Exception as error:  # an action that crashes is a failed action, not a failed run
            step = Step(action, Effect(), None, "failed", [f"{type(error).__name__}: {error}"])
            self.steps.append(step)
            return step
        if not effect.changes and not effect.removed and action.type not in STRUCTURAL:
            return None

        problems = postconditions(action, effect, self.ws, self.contract)
        if problems:
            step = Step(action, effect, None, "failed", problems)
        else:
            decision = decide(action, effect, self.ws, self.policy_config)
            if decision.committed:
                self.ws.commit(effect)
            step = Step(action, effect, decision, "applied" if decision.committed else "escalated")
        self.steps.append(step)
        return step


def _mapping_actions(mapping: MappingResult) -> list[Action]:
    actions: list[Action] = []
    for m in mapping.mappings:
        rationale = f"header matched by {m.method}"
        if len(m.sources) == 1:
            actions.append(MapColumn(source=m.sources[0], target=m.target, issue=m.issue, rationale=rationale))
        else:
            actions.append(CombineColumns(sources=m.sources, target=m.target, issue=m.issue, rationale=rationale))
    for column in mapping.dropped:
        actions.append(DropColumn(column=column, issue="M5", rationale="matches no target field"))
    return actions


def run(
    source_path: str | Path,
    contract: Contract,
    references: dict[str, frozenset[str]] | None = None,
    policy_config: dict | None = None,
) -> RunOutcome:
    started = time.perf_counter()
    source = read_source(source_path)
    profiles = profile_table(source)
    mapping = map_columns(source, contract)

    ws = Workspace(source, contract.field_names)
    executor = Executor(contract, ws, policy_config)
    findings: list[Finding] = list(plan_missing_columns(mapping.missing))
    for action in _mapping_actions(mapping):
        executor.run(action)

    ctx = Context(contract, ws, references or {}, frozenset(mapping.missing))
    proposals = [plan_field(ctx, f) for f in contract.fields if f.name not in mapping.missing]
    proposals.append(plan_rows(ctx))
    for planner in proposals:
        for proposal in planner:
            if isinstance(proposal, Finding):
                findings.append(proposal)
            else:
                executor.run(proposal)

    rows, numbers = ws.table()
    return RunOutcome(
        contract, source, profiles, mapping, executor.steps, findings, rows, numbers, time.perf_counter() - started
    )


def replay(
    plan: dict, source_path: str | Path, contract: Contract, approved: set[str] = frozenset()
) -> tuple[list[dict[str, str]], list[int]]:
    """Apply a saved plan to a source file.

    Applied steps are re-applied; escalated steps only when their id is in
    ``approved``. Failed steps are skipped.
    """
    ingest = plan["ingest"]
    source = read_source(source_path, **ingest)
    ws = Workspace(source, contract.field_names)
    for step in plan["steps"]:
        action = action_from_dict(step["action"])
        if step["status"] == "applied" or (step["status"] == "escalated" and action.id in approved):
            ws.commit(action.compute(ws))
    return ws.table()


# Detections --------------------------------------------------------------


def collect_detections(outcome: RunOutcome) -> list[dict]:
    """Every issue the run found, in the scorer's detection format.

    A cell changed by several actions is reported once, under the first
    label it got; whitespace and case noise (S4) gives way to any later,
    more specific label.
    """
    detections: list[dict] = []
    column_level: set[tuple[str, str]] = set()
    cell_label: dict[tuple[str, int], str] = {}

    for step in outcome.steps:
        action = step.action
        if step.status == "failed":
            continue
        if action.type in ("map_column", "combine_columns"):
            continue
        if action.type == "drop_column":
            detections.append({"issue": "M5", "columns": [action.column]})
            continue
        if action.type == "deduplicate":
            detections.append({"issue": action.issue, "target": action.keys[0], "groups": action.groups})
            continue
        if action.type == "drop_rows":
            detections.append({"issue": action.issue, "target": action.target, "rows": sorted(step.effect.removed)})
            continue
        if action.scope == "column":
            if step.effect.changes and action.issue:
                column_level.add((action.issue, action.target))
            continue
        for change in step.effect.changes:
            key = (change.field, change.row)
            if change.issue and (key not in cell_label or cell_label[key] == "S4"):
                cell_label[key] = change.issue

    for m in outcome.mapping.mappings:
        if m.issue and ("M4", m.target) not in column_level:
            detections.append({"issue": m.issue, "target": m.target, "columns": m.sources})

    for issue, target in sorted(column_level):
        detections.append({"issue": issue, "target": target})

    by_issue: dict[tuple[str, str], set[int]] = defaultdict(set)
    for (target, row), issue in cell_label.items():
        by_issue[(issue, target)].add(row)
    for finding in outcome.findings:
        if finding.groups is not None:
            detections.append({"issue": finding.issue, "target": finding.target, "groups": finding.groups})
        elif finding.rows is None:
            detections.append({"issue": finding.issue, "target": finding.target})
        else:
            by_issue[(finding.issue, finding.target)].update(finding.rows)
    for (issue, target), rows in sorted(by_issue.items()):
        detections.append({"issue": issue, "target": target, "rows": sorted(rows)})
    return detections

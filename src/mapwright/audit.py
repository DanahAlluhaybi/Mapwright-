"""The audit log: one JSON event per line, in the order things happened.

Who did what (rules, the model, or a named reviewer later), to which
field, with what decision, and the hash of the staged table before and
after, so any step can be checked against a replay.
"""

from __future__ import annotations

import hashlib
import json

from .pipeline import RunOutcome, _now


def _target_hash(outcome: RunOutcome) -> str:
    payload = json.dumps(list(zip(outcome.source_rows, outcome.rows)), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def events(outcome: RunOutcome) -> list[dict]:
    session = outcome.session
    model = session.model if session is not None else None
    log: list[dict] = [
        {
            "event": "run_started",
            "at": outcome.started_at,
            "system": outcome.system,
            "model": model,
            "entity": outcome.contract.entity,
            "contract_version": outcome.contract.version,
            "source_sha256": outcome.source_sha256,
            "ingest": outcome.source.settings(),
            "source_rows": len(outcome.source.rows),
        }
    ]
    for step in outcome.steps:
        action = step.action
        log.append(
            {
                "event": "action",
                "at": step.at,
                "actor": action.origin,
                "id": action.id,
                "type": action.type,
                "targets": action.targets,
                "issue": action.issue,
                "confidence": action.confidence,
                "status": step.status,
                "decision": step.decision.kind if step.decision else None,
                "reason": step.decision.reason if step.decision else "; ".join(step.problems),
                "cells_changed": len(step.effect.changes),
                "rows_removed": len(step.effect.removed),
                "input_sha256": step.input_hash,
                "output_sha256": step.output_hash,
            }
        )
    if session is not None:
        for call in session.usage.calls:
            log.append({"event": "llm_call", **call})
        for invalid in session.invalid:
            log.append(
                {"event": "invalid_proposal", "type": invalid.type, "target": invalid.target, "reason": invalid.reason}
            )
    for item in outcome.escalations:
        log.append({"event": "escalated", **item})
    log.append(
        {
            "event": "run_finished",
            "at": _now(),
            "output_rows": len(outcome.rows),
            "target_sha256": _target_hash(outcome),
            "escalations": len(outcome.escalations),
            "seconds": round(outcome.seconds, 3),
        }
    )
    return log


def write_audit(outcome: RunOutcome, path) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for event in events(outcome):
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

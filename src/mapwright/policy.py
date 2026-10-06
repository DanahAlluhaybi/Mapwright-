"""Deterministic approval policy.

Every computed action gets one of three decisions:

* ``apply``: committed, nothing to review.
* ``flag``: committed, and the touched cells are listed for a person to
  confirm. Used when the rule is certain but the issue type requires
  sign-off (an amount written as "12.5K", day-first dates settled by
  evidence).
* ``hold``: not committed. The proposed change waits for approval, and the
  data keeps its previous value until then.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from .actions import Action, Deduplicate, Effect, Workspace

CONFIG = Path(__file__).resolve().parent / "policy.yaml"


@lru_cache(maxsize=None)
def load_config(path: Path = CONFIG) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@dataclass
class Decision:
    kind: str
    reason: str
    escalations: list[dict] = field(default_factory=list)

    @property
    def committed(self) -> bool:
        return self.kind in ("apply", "flag")


def _escalations(action: Action, effect: Effect, issues: set[str] | None) -> list[dict]:
    """Escalation items for the changes whose issue is in ``issues`` (all if None)."""
    if isinstance(action, Deduplicate) or effect.removed:
        if issues is not None and action.issue not in issues:
            return []
        if isinstance(action, Deduplicate):
            return [{"action": action.id, "issue": action.issue, "target": action.keys[0], "groups": action.groups}]
        return [
            {"action": action.id, "issue": action.issue, "target": action.targets[0], "rows": sorted(effect.removed)}
        ]

    by_field: dict[tuple[str, str | None], set[int]] = defaultdict(set)
    for change in effect.changes:
        if issues is None or change.issue in issues:
            by_field[(change.field, change.issue)].add(change.row)
    items = []
    for (target, issue), rows in sorted(by_field.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        item = {"action": action.id, "issue": issue, "target": target}
        if action.scope == "cell":
            item["rows"] = sorted(rows)
        items.append(item)
    return items


def _changed_share(effect: Effect, ws: Workspace) -> float:
    shares = []
    for target in effect.fields():
        filled = sum(1 for v in ws.values(target).values() if v)
        changed = len(effect.rows(target))
        shares.append(changed / filled if filled else 0.0)
    return max(shares, default=0.0)


def decide(action: Action, effect: Effect, ws: Workspace, config: dict | None = None) -> Decision:
    config = config or load_config()
    review = set(config["review_issues"])

    if action.type in config["hold_actions"]:
        return Decision("hold", f"{action.type} always needs approval", _escalations(action, effect, None))
    if isinstance(action, Deduplicate) and action.conflicting:
        return Decision("hold", "duplicate keys with conflicting values", _escalations(action, effect, None))
    if getattr(action, "ambiguous", False):
        return Decision("hold", "the rule could not settle how to read the values", _escalations(action, effect, None))
    if action.origin == "llm" and (action.confidence or 0.0) < config["min_llm_confidence"]:
        return Decision("hold", "low model confidence", _escalations(action, effect, None))
    if action.origin in config["large_change_origins"] and _changed_share(effect, ws) > config["large_change_share"]:
        return Decision("hold", "changes a large share of the column", _escalations(action, effect, None))

    flagged = _escalations(action, effect, review)
    if flagged:
        return Decision("flag", "applied; the issue type needs sign-off", flagged)
    return Decision("apply", "")

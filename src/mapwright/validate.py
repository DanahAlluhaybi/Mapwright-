"""Action postconditions.

Contract checks on the finished table live in ``contracts.check_table``.
These checks run on each action's effect before it is committed: an
action that claims to produce canonical values must actually produce them.
"""

from __future__ import annotations

from .actions import Action, Effect, Workspace
from .contracts import Contract, check_value

# Actions whose output must be a valid value for the field. Range and
# required checks are left out: those are findings, not action failures.
PRODUCES_CANONICAL = {"map_values", "parse_date", "parse_number", "normalize_phone"}
FORMAT_RULES = {"type", "pattern", "allowed_values", "reference"}


def postconditions(action: Action, effect: Effect, ws: Workspace, contract: Contract) -> list[str]:
    problems = []
    if action.type in PRODUCES_CANONICAL:
        for change in effect.changes:
            f = contract.field(change.field)
            broken = set(check_value(contract, f, change.after)) & FORMAT_RULES
            if broken:
                problems.append(f"row {change.row}: {change.after!r} breaks {', '.join(sorted(broken))}")

    if action.type == "set_null" and any(c.after != "" for c in effect.changes):
        problems.append("set_null produced a non-null value")

    declared = getattr(action, "rows", None)
    if action.type == "drop_rows" and declared is not None:
        expected = [n for n in declared if n in ws.rows]
        if sorted(effect.removed) != sorted(expected):
            problems.append("rows removed differ from rows declared")
    if action.type == "deduplicate":
        expected = [n for group in action.groups for n in group[1:] if n in ws.rows]
        if sorted(effect.removed) != sorted(expected):
            problems.append("rows removed differ from the duplicate groups")
    return problems

import unittest
from pathlib import Path

import yaml

from mapwright.actions import (
    Deduplicate,
    DropRows,
    MapColumn,
    MapValues,
    ParseDate,
    ParseNumber,
    Workspace,
)
from mapwright.ingest import parse_text
from mapwright.policy import decide, load_config

ROOT = Path(__file__).resolve().parents[1]


def workspace(text):
    source = parse_text(text)
    ws = Workspace(source, source.header)
    for name in source.header:
        ws.commit(MapColumn(source=name, target=name).compute(ws))
    return ws


class PolicyTest(unittest.TestCase):
    def test_review_issues_match_the_taxonomy(self):
        taxonomy = yaml.safe_load((ROOT / "benchmark" / "issue_types.yaml").read_text("utf-8"))["issues"]
        review = {i["id"] for i in taxonomy if i["risk"] == "review" and not i.get("stretch")}
        self.assertEqual(set(load_config()["review_issues"]), review)

    def test_auto_issue_is_applied(self):
        ws = workspace("c\nKSA\nSA\n")
        action = MapValues(id="a1", target="c", mapping={"ksa": "SA"}, issue="V1")
        decision = decide(action, action.compute(ws), ws)
        self.assertEqual(decision.kind, "apply")
        self.assertEqual(decision.escalations, [])

    def test_review_issue_is_applied_and_flagged_by_row(self):
        ws = workspace('amount\n12.5K\n"1,000.00"\n')
        action = ParseNumber(id="a1", target="amount")
        decision = decide(action, action.compute(ws), ws)
        self.assertEqual(decision.kind, "flag")
        self.assertEqual(decision.escalations, [{"action": "a1", "issue": "V4", "target": "amount", "rows": [1]}])

    def test_column_level_issue_is_flagged_for_the_whole_column(self):
        ws = workspace("d\n25/06/2010\n05/03/2023\n")
        action = ParseDate(id="a1", target="d", formats=["%d/%m/%Y"], issue="V2", scope="column")
        decision = decide(action, action.compute(ws), ws)
        self.assertEqual(decision.kind, "flag")
        self.assertEqual(decision.escalations, [{"action": "a1", "issue": "V2", "target": "d"}])

    def test_ambiguous_dates_are_held(self):
        ws = workspace("d\n05/03/2023\n")
        action = ParseDate(id="a1", target="d", formats=["%d/%m/%Y"], issue="V2", scope="column", ambiguous=True)
        self.assertEqual(decide(action, action.compute(ws), ws).kind, "hold")

    def test_row_removal_is_held(self):
        ws = workspace("id\nA\nB\n")
        action = DropRows(id="a1", target="id", rows=[2], issue="S7")
        decision = decide(action, action.compute(ws), ws)
        self.assertEqual(decision.kind, "hold")
        self.assertEqual(decision.escalations, [{"action": "a1", "issue": "S7", "target": "id", "rows": [2]}])

    def test_exact_duplicates_applied_conflicting_held(self):
        ws = workspace("id\nA\nA\n")
        exact = Deduplicate(id="a1", keys=["id"], groups=[[1, 2]])
        self.assertEqual(decide(exact, exact.compute(ws), ws).kind, "apply")
        conflicting = Deduplicate(id="a2", keys=["id"], groups=[[1, 2]], conflicting=True, issue="S8")
        decision = decide(conflicting, conflicting.compute(ws), ws)
        self.assertEqual(decision.kind, "hold")
        self.assertEqual(decision.escalations[0]["groups"], [[1, 2]])

    def test_llm_thresholds(self):
        ws = workspace("c\nKSA\nKSA\nSA\n")
        low = MapValues(id="a1", target="c", mapping={"ksa": "SA"}, issue="V1", origin="llm", confidence=0.5)
        self.assertEqual(decide(low, low.compute(ws), ws).kind, "hold")
        broad = MapValues(id="a2", target="c", mapping={"ksa": "SA"}, issue="V1", origin="llm", confidence=0.95)
        self.assertEqual(decide(broad, broad.compute(ws), ws).kind, "hold")
        rules = MapValues(id="a3", target="c", mapping={"ksa": "SA"}, issue="V1")
        self.assertEqual(decide(rules, rules.compute(ws), ws).kind, "apply")


if __name__ == "__main__":
    unittest.main()

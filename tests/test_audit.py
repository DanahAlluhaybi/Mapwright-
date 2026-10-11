import json
import tempfile
import unittest
from pathlib import Path

from mapwright.audit import events
from mapwright.contracts import load_contracts
from mapwright.output import write_run
from mapwright.pipeline import replay, run

ROOT = Path(__file__).resolve().parents[1]
CUSTOMER = load_contracts(ROOT / "contracts")["customer"]
SOURCE = ROOT / "benchmark" / "datasets" / "D02" / "source.csv"


class AuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outcome = run(SOURCE, CUSTOMER)
        cls.log = events(cls.outcome)

    def test_starts_and_finishes(self):
        self.assertEqual(self.log[0]["event"], "run_started")
        self.assertEqual(len(self.log[0]["source_sha256"]), 64)
        self.assertEqual(self.log[-1]["event"], "run_finished")
        self.assertEqual(self.log[-1]["output_rows"], len(self.outcome.rows))

    def test_hashes_chain_from_step_to_step(self):
        actions = [e for e in self.log if e["event"] == "action"]
        self.assertTrue(actions)
        for previous, current in zip(actions, actions[1:]):
            self.assertEqual(previous["output_sha256"], current["input_sha256"])
        held = [e for e in actions if e["decision"] == "hold"]
        for event in held:
            self.assertEqual(event["input_sha256"], event["output_sha256"])

    def test_every_escalation_is_logged(self):
        self.assertEqual(sum(e["event"] == "escalated" for e in self.log), len(self.outcome.escalations))

    def test_written_with_the_run_and_replay_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = write_run(self.outcome, tmp)
            lines = (out / "audit.jsonl").read_text("utf-8").splitlines()
            plan = json.loads((out / "plan.json").read_text("utf-8"))
        self.assertEqual(json.loads(lines[0])["event"], "run_started")
        rows, numbers = replay(plan, SOURCE, CUSTOMER)
        self.assertEqual((rows, numbers), (self.outcome.rows, self.outcome.source_rows))


if __name__ == "__main__":
    unittest.main()

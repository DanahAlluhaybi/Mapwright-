import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from evaluation.check import check

ROOT = Path(__file__).resolve().parents[1]
TRUTH = ROOT / "benchmark" / "datasets" / "D05" / "ground_truth.csv"


class CheckTest(unittest.TestCase):
    def run_check(self, path):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = check(path, "D05")
        return result, out.getvalue()

    def test_ground_truth_matches_itself(self):
        result, text = self.run_check(TRUTH)
        self.assertEqual(result["cells_different"], 0)
        self.assertIn("matches the ground truth exactly", text)

    def test_differences_are_reported(self):
        lines = TRUTH.read_text("utf-8").splitlines()
        lines[1] = lines[1].replace("Mohammed", "Mohamed", 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
            result, text = self.run_check(path)
        self.assertEqual(result["cells_different"], 1)
        self.assertEqual(result["missing_rows"], 1)
        self.assertIn("expected 'Mohammed Alshammari'", text)


if __name__ == "__main__":
    unittest.main()

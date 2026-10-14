"""Compare one cleaned file with a benchmark case's ground truth.

    python -m evaluation.check runs/api/<id>/approved.csv D05
    python -m evaluation.check Downloads/source-clean.csv D05

Rows are matched on the contract's primary key. Prints how many rows and
cells match, and the first differences, so a person can check a result
from the upload page (or the command line) by hand.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from .loading import DATASETS, load_case


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def check(cleaned: Path, case_id: str, show: int = 10) -> dict:
    case = DATASETS / case_id
    key_fields = list(load_case(case).contract.primary_key)

    truth = _rows(case / "ground_truth.csv")
    output = _rows(cleaned)
    fields = list(truth[0].keys()) if truth else []

    def key(row: dict[str, str]) -> tuple:
        return tuple(row.get(f, "") for f in key_fields)

    truth_by_key = {key(r): r for r in truth}
    output_by_key = {key(r): r for r in output}
    shared = [k for k in truth_by_key if k in output_by_key]
    differences = [
        {"key": "/".join(k), "field": f, "output": output_by_key[k].get(f, ""), "truth": truth_by_key[k][f]}
        for k in shared
        for f in fields
        if output_by_key[k].get(f, "") != truth_by_key[k][f]
    ]
    cells = len(shared) * len(fields)
    result = {
        "truth_rows": len(truth),
        "output_rows": len(output),
        "matched_rows": len(shared),
        "missing_rows": len(truth_by_key) - len(shared),
        "extra_rows": len(output_by_key) - len(shared),
        "cells_compared": cells,
        "cells_different": len(differences),
        "cell_accuracy": round(1 - len(differences) / cells, 4) if cells else None,
    }

    print(f"Rows: {result['output_rows']} in the file, {result['truth_rows']} in the ground truth, "
          f"{result['matched_rows']} matched on {', '.join(key_fields)}")
    if result["missing_rows"] or result["extra_rows"]:
        print(f"  {result['missing_rows']} rows missing, {result['extra_rows']} rows that should not be there")
    print(f"Cells: {cells - len(differences)} of {cells} identical ({result['cell_accuracy']:.1%})" if cells else "Cells: none compared")
    for d in differences[:show]:
        print(f"  {d['key']} {d['field']}: got {d['output']!r}, expected {d['truth']!r}")
    if len(differences) > show:
        print(f"  ... and {len(differences) - show} more")
    if not differences and not result["missing_rows"] and not result["extra_rows"]:
        print("The file matches the ground truth exactly.")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation.check")
    parser.add_argument("cleaned", type=Path, help="the cleaned CSV (approved.csv, target.csv or a download)")
    parser.add_argument("case", help="benchmark case id, e.g. D05")
    parser.add_argument("--show", type=int, default=10, help="how many differences to print")
    args = parser.parse_args(argv)
    if not args.cleaned.is_file():
        print(f"File not found: {args.cleaned}")
        return 2
    if not (DATASETS / args.case / "ground_truth.csv").is_file():
        print(f"No benchmark case {args.case!r}")
        return 2
    check(args.cleaned, args.case, args.show)
    return 0


if __name__ == "__main__":
    sys.exit(main())

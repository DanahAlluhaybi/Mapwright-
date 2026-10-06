"""Command line for the benchmark generator.

    python -m benchmark.generator generate    write every case and the reference table
    python -m benchmark.generator freeze      record hashes of the held-out files
    python -m benchmark.generator verify      regenerate and compare with what is on disk

H04 is not generated: it is a real export, sampled and mapped by
``benchmark.real.companies_house``. ``verify`` rebuilds its ground truth
from its source and checks it against the frozen hashes like the rest.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from benchmark.real import companies_house

from .build import ROOT, build_case, sha256, write_case, write_reference_customers
from .cases import CASES

DATASETS = ROOT / "benchmark" / "datasets"
REFERENCE = ROOT / "benchmark" / "reference_customers.csv"
FROZEN = ROOT / "benchmark" / "FROZEN"
CASE_FILES = ("source.csv", "ground_truth.csv", "manifest.json")


def generate(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    write_reference_customers(out.parent / REFERENCE.name)
    for case in CASES:
        built = build_case(case)
        write_case(built, out / case.id)
        print(f"{case.id}  {built.manifest['source_rows']:>4} source rows  "
              f"{built.manifest['target_rows']:>4} target rows  {len(built.manifest['issues']):>3} issue entries")


REAL_CASES = ("H04",)


def _heldout_ids() -> list[str]:
    ids = [case.id for case in CASES if case.split == "heldout"]
    return ids + [case_id for case_id in REAL_CASES if (DATASETS / case_id / "source.csv").exists()]


def _heldout_hashes(root: Path) -> dict[str, str]:
    hashes = {}
    for case_id in _heldout_ids():
        for name in CASE_FILES:
            relative = f"{case_id}/{name}"
            hashes[relative] = sha256(root / relative)
    return hashes


def freeze() -> None:
    lines = [f"{digest}  {relative}" for relative, digest in sorted(_heldout_hashes(DATASETS).items())]
    FROZEN.write_text(
        "# Held-out files, frozen before any prompt was written. Do not regenerate.\n"
        + "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    print(f"Froze {len(lines)} held-out files in {FROZEN.relative_to(ROOT)}")


def verify() -> int:
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "datasets"
        generate(fresh)
        for case in CASES:
            for name in CASE_FILES:
                relative = f"{case.id}/{name}"
                on_disk = DATASETS / relative
                if not on_disk.exists():
                    problems.append(f"missing {relative}")
                elif sha256(on_disk) != sha256(fresh / relative):
                    problems.append(f"differs from generator output: {relative}")
        if not REFERENCE.exists() or sha256(REFERENCE) != sha256(fresh.parent / REFERENCE.name):
            problems.append("reference_customers.csv differs from generator output")

        real = DATASETS / "H04"
        if (real / "source.csv").exists():
            rebuilt = Path(tmp) / "H04"
            rebuilt.mkdir()
            (rebuilt / "source.csv").write_bytes((real / "source.csv").read_bytes())
            companies_house.build(rebuilt)
            for name in ("ground_truth.csv", "manifest.json"):
                if not (real / name).exists():
                    problems.append(f"missing H04/{name}")
                elif sha256(real / name) != sha256(rebuilt / name):
                    problems.append(f"differs from companies_house build: H04/{name}")

    if FROZEN.exists():
        recorded = {}
        for line in FROZEN.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                digest, relative = line.split("  ", 1)
                recorded[relative] = digest
        if recorded != _heldout_hashes(DATASETS):
            problems.append("held-out files no longer match benchmark/FROZEN")
    else:
        problems.append("benchmark/FROZEN does not exist yet")

    for problem in problems:
        print(problem)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m benchmark.generator")
    parser.add_argument("command", choices=["generate", "freeze", "verify"])
    args = parser.parse_args(argv)
    if args.command == "generate":
        generate(DATASETS)
        return 0
    if args.command == "freeze":
        freeze()
        return 0
    return verify()


if __name__ == "__main__":
    sys.exit(main())

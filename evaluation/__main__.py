"""Command line for scoring.

    python -m evaluation baseline perfect runs/perfect      write a reference system's run
    python -m evaluation baseline rules runs/rules --split dev
    python -m evaluation baseline hybrid runs/hybrid --record      calls Gemini, fills llm_cache/
    python -m evaluation baseline hybrid runs/hybrid               replays llm_cache/, no key needed
    python -m evaluation score runs/<run-id>                 score a run into results/<run-id>/
    python -m evaluation compare results/a results/b         systems side by side
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from .aggregate import summarize
from .baselines import LLM_SYSTEMS, SYSTEMS, LLMSettings
from .loading import (
    DATASETS,
    ROOT,
    load_case,
    load_cases,
    load_reference_customer_ids,
    load_run_case,
)
from .report import comparison_markdown, failures_markdown, fmt, summary_markdown
from .scorer import score_case


def write_baseline(name: str, run_dir: Path, split: str | None = None, cases: list[str] | None = None) -> None:
    system = SYSTEMS[name]
    info = {"system": name, "date": dt.date.today().isoformat()}
    if name in LLM_SYSTEMS:
        info |= {"model": LLMSettings.provider().model, "sample": LLMSettings.sample}
        if LLMSettings.policy is not None:
            info["policy"] = LLMSettings.policy
    for case in load_cases():
        if (split is None or case.split == split) and (not cases or case.id in cases):
            print(f"{name}: {case.id}", flush=True)
            system(case, run_dir / case.id)
    (run_dir / "run.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {name} run to {run_dir}")


def score(run_dir: Path, out_dir: Path) -> dict:
    run_info_path = run_dir / "run.json"
    run_info = json.loads(run_info_path.read_text("utf-8")) if run_info_path.exists() else {"system": run_dir.name}
    reference_ids = load_reference_customer_ids()

    scores = []
    for case_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        benchmark = DATASETS / case_dir.name
        if not (benchmark / "manifest.json").exists():
            print(f"Skipping {case_dir.name}: not a benchmark case")
            continue
        case = load_case(benchmark)
        scores.append(score_case(case, load_run_case(case_dir, case.contract), reference_ids))

    summary = summarize(scores)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "scores.json").write_text(
        json.dumps({"run": run_info, "summary": summary, "cases": scores}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "summary.md").write_text(summary_markdown(run_info, summary), encoding="utf-8")
    (out_dir / "failures.md").write_text(failures_markdown(scores), encoding="utf-8")

    for split, data in summary.items():
        headline = data["headline"]
        print(
            f"{split:8}  dirty-cell accuracy {fmt(headline['Dirty-cell accuracy'])}  "
            f"clean preservation {fmt(headline['Clean-cell preservation'])}  "
            f"silent errors/1000 {fmt(headline['Silent errors per 1,000 cells'])}  "
            f"missed reviews {fmt(headline['Missed reviews'])}"
        )
    print(f"Wrote {out_dir / 'summary.md'}")
    return summary


def compare(result_dirs: list[Path]) -> str:
    runs = []
    for directory in result_dirs:
        data = json.loads((directory / "scores.json").read_text("utf-8"))
        runs.append((data["run"].get("system", directory.name), data["summary"]))
    return comparison_markdown(runs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation")
    commands = parser.add_subparsers(dest="command", required=True)

    baseline = commands.add_parser("baseline", help="write a reference system's run")
    baseline.add_argument("name", choices=sorted(SYSTEMS))
    baseline.add_argument("run_dir", type=Path)
    baseline.add_argument("--split", choices=["dev", "heldout"], help="only run cases from one split")
    baseline.add_argument("--cases", nargs="*", help="only these case ids")
    baseline.add_argument("--record", action="store_true", help="LLM systems: call Gemini for uncached requests")
    baseline.add_argument("--model", default="", help="LLM systems: model id (default gemini-3.8-flash)")
    baseline.add_argument("--sample", type=int, default=0, help="LLM systems: repeat number for repeated runs")
    baseline.add_argument("--policy", type=Path, help="LLM systems: a policy file to use instead of policy.yaml")

    score_cmd = commands.add_parser("score", help="score a run")
    score_cmd.add_argument("run_dir", type=Path)
    score_cmd.add_argument("--out", type=Path, help="defaults to results/<run name>")

    compare_cmd = commands.add_parser("compare", help="compare scored runs")
    compare_cmd.add_argument("result_dirs", type=Path, nargs="+")

    args = parser.parse_args(argv)
    if args.command == "baseline":
        LLMSettings.record, LLMSettings.model, LLMSettings.sample = args.record, args.model, args.sample
        if args.policy:
            import yaml

            LLMSettings.policy = yaml.safe_load(args.policy.read_text("utf-8"))
        write_baseline(args.name, args.run_dir, args.split, args.cases)
    elif args.command == "score":
        score(args.run_dir, args.out or ROOT / "results" / args.run_dir.name)
    else:
        print(compare(args.result_dirs))
    return 0


if __name__ == "__main__":
    sys.exit(main())

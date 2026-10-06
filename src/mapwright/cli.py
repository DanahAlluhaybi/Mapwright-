"""Command line.

    python -m mapwright run SOURCE --contract contracts/customer.yaml --out runs/demo
    python -m mapwright run SOURCE --contract contracts/sales_order.yaml \
        --reference customer_id=benchmark/reference_customers.csv --out runs/demo
    python -m mapwright replay runs/demo/plan.json SOURCE --contract ... --out replayed.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .contracts import load_contract
from .output import write_run, write_target
from .pipeline import replay, run


def load_reference(spec: str) -> tuple[str, frozenset[str]]:
    """``field=path.csv``: the allowed values are the column named ``field`` in the file."""
    name, _, path = spec.partition("=")
    if not path:
        raise argparse.ArgumentTypeError("expected FIELD=PATH")
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return name, frozenset(row[name] for row in csv.DictReader(handle))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mapwright")
    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser("run", help="onboard a source file")
    run_cmd.add_argument("source", type=Path)
    run_cmd.add_argument("--contract", type=Path, required=True)
    run_cmd.add_argument("--out", type=Path, required=True)
    run_cmd.add_argument("--reference", type=load_reference, action="append", default=[])

    replay_cmd = commands.add_parser("replay", help="apply a saved plan to a source file")
    replay_cmd.add_argument("plan", type=Path)
    replay_cmd.add_argument("source", type=Path)
    replay_cmd.add_argument("--contract", type=Path, required=True)
    replay_cmd.add_argument("--out", type=Path, required=True)
    replay_cmd.add_argument("--approve", nargs="*", default=[], help="ids of escalated actions to apply")

    args = parser.parse_args(argv)
    contract = load_contract(args.contract)
    if args.command == "run":
        outcome = run(args.source, contract, dict(args.reference))
        write_run(outcome, args.out)
        held = sum(1 for s in outcome.steps if s.status == "escalated")
        print(
            f"{len(outcome.rows)} rows written to {args.out / 'target.csv'}; "
            f"{held} actions held, {len(outcome.escalations)} items need review"
        )
    else:
        plan = json.loads(args.plan.read_text("utf-8"))
        rows, numbers = replay(plan, args.source, contract, set(args.approve))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        write_target(args.out, contract.field_names, rows, numbers)
        print(f"{len(rows)} rows written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

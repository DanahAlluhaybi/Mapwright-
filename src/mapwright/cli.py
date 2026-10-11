"""Command line.

    python -m mapwright run SOURCE --contract contracts/customer.yaml --out runs/demo
    python -m mapwright run SOURCE --contract contracts/sales_order.yaml \
        --reference customer_id=benchmark/reference_customers.csv --out runs/demo
    python -m mapwright replay runs/demo/plan.json SOURCE --contract ... --out replayed.csv
    python -m mapwright llm-check          one tiny Gemini request, to check the API key works
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


def llm_check(model: str | None) -> int:
    from .llm import LLMError, LLMRequest
    from .llm.gemini import GeminiProvider

    try:
        provider = GeminiProvider(model)
        response = provider.complete(
            LLMRequest(prompt='Reply with the JSON object {"ok": true} and nothing else.', model=provider.model)
        )
        answer = response.json()
    except LLMError as error:
        print(f"Gemini check failed: {error}")
        return 1
    print(
        f"Gemini check passed: {response.model} answered {answer} "
        f"({response.input_tokens} input tokens, {response.output_tokens} output tokens)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mapwright")
    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser("run", help="onboard a source file")
    run_cmd.add_argument("source", type=Path)
    run_cmd.add_argument("--contract", type=Path, required=True)
    run_cmd.add_argument("--out", type=Path, required=True)
    run_cmd.add_argument("--reference", type=load_reference, action="append", default=[])
    run_cmd.add_argument(
        "--system",
        choices=["rules", "hybrid", "llm"],
        default="rules",
        help="rules only (default), rules then the model for what they leave (hybrid), or the model only (llm)",
    )
    run_cmd.add_argument("--model", help="model for hybrid/llm; defaults to $MAPWRIGHT_MODEL or gemini-3.8-flash")

    replay_cmd = commands.add_parser("replay", help="apply a saved plan to a source file")
    replay_cmd.add_argument("plan", type=Path)
    replay_cmd.add_argument("source", type=Path)
    replay_cmd.add_argument("--contract", type=Path, required=True)
    replay_cmd.add_argument("--out", type=Path, required=True)
    replay_cmd.add_argument("--approve", nargs="*", default=[], help="ids of escalated actions to apply")

    check_cmd = commands.add_parser("llm-check", help="send one tiny request to Gemini")
    check_cmd.add_argument("--model", help="defaults to $MAPWRIGHT_MODEL or gemini-3.8-flash")

    args = parser.parse_args(argv)
    if args.command == "llm-check":
        return llm_check(args.model)
    for path in [getattr(args, "source", None), getattr(args, "plan", None), args.contract]:
        if path is not None and not Path(path).is_file():
            print(f"File not found: {path}")
            return 2
    contract = load_contract(args.contract)
    if args.command == "run":
        session = None
        if args.system != "rules":
            from .llm import CachedProvider, LLMError
            from .llm.gemini import GeminiProvider
            from .llmplan import LLMSession

            try:
                session = LLMSession(CachedProvider(Path("llm_cache"), GeminiProvider(args.model)))
            except LLMError as error:
                print(error)
                return 1
        try:
            outcome = run(args.source, contract, dict(args.reference), system=args.system, session=session)
        except Exception as error:
            if type(error).__name__ not in ("LLMError", "CacheMiss"):
                raise
            print(f"Stopped: {error}")
            return 1
        write_run(outcome, args.out)
        held = sum(1 for s in outcome.steps if s.status == "escalated")
        if session is not None:
            print(f"{session.usage.llm_calls} model calls ({session.usage.cached_calls} from the cache)")
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

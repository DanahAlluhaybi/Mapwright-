"""Every evaluated system, written in one place so all run the same way.

Two reference systems bracket every real result.

``perfect`` outputs the ground truth and reports exactly the manifest's
issues; every metric should come out at its best value. ``passthrough``
copies source columns whose header already matches a target field and
changes nothing; it is the floor any real system must beat.

They also test the scorer itself: if ``perfect`` does not score perfectly,
the scorer is wrong.

``rules`` is system R, the rules-only pipeline. ``llm``, ``hybrid`` and
``hybrid-norepair`` are systems L, H and H−.
"""

from __future__ import annotations

from pathlib import Path

from mapwright.llm import CachedProvider
from mapwright.llmplan import LLMSession
from mapwright.output import write_run
from mapwright.pipeline import run

from .loading import (
    DATASETS,
    ROOT,
    BenchmarkCase,
    load_reference_customer_ids,
    write_run_case,
)

LLM_CACHE = ROOT / "llm_cache"


def _manifest_detections(case: BenchmarkCase) -> list[dict]:
    keep = ("issue", "target", "columns", "rows", "groups")
    return [{k: e[k] for k in keep if k in e} for e in case.manifest["issues"]]


def _manifest_escalations(case: BenchmarkCase) -> list[dict]:
    escalations = []
    for entry in case.manifest["issues"]:
        if entry["risk"] != "review":
            continue
        item = {"issue": entry["issue"], "target": entry["target"]}
        if "groups" in entry:
            item["groups"] = entry["groups"]
        else:
            item["rows"] = entry.get("rows")
        escalations.append(item)
    return escalations


def perfect(case: BenchmarkCase, directory: Path) -> None:
    lineage = case.manifest["row_lineage"]
    key_to_source = {key: int(n) for n, key in lineage.items() if key != "drop"}
    result = {
        "mapping": case.manifest["column_mapping"],
        "dropped_columns": case.manifest["dropped_columns"],
        "detections": _manifest_detections(case),
        "actions": [],
        "escalations": _manifest_escalations(case),
        "usage": {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0},
    }
    write_run_case(
        directory,
        case.contract,
        case.truth,
        [key_to_source[row[case.key_field]] for row in case.truth],
        result,
    )


def _normalize_header(header: str) -> str:
    return header.strip().lower().replace(" ", "_")


def passthrough(case: BenchmarkCase, directory: Path) -> None:
    fields = set(case.contract.field_names)
    mapping = {}
    for header in case.source_header:
        name = _normalize_header(header)
        if name in fields and name not in mapping:
            mapping[name] = header

    rows = [{name: source[header] for name, header in mapping.items()} for source in case.source_rows]
    result = {
        "mapping": [{"source": [header], "target": name} for name, header in mapping.items()],
        "dropped_columns": [],
        "detections": [],
        "actions": [],
        "escalations": [],
        "usage": {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0},
    }
    write_run_case(directory, case.contract, rows, list(range(1, len(rows) + 1)), result)


def _references(case: BenchmarkCase) -> dict:
    if case.contract.entity == "sales_order":
        return {"customer_id": load_reference_customer_ids()}
    return {}


def rules(case: BenchmarkCase, directory: Path) -> None:
    """System R: the rules-only pipeline. It sees the source file and nothing else."""
    write_run(run(DATASETS / case.id / "source.csv", case.contract, _references(case)), directory)


class LLMSettings:
    """How LLM systems reach the model for this process.

    By default answers are replayed from ``llm_cache/`` and a missing answer
    is an error, so scoring never needs a key. ``record`` calls Gemini for
    anything not cached yet and stores the answer.
    """

    record = False
    model = ""
    sample = 0
    _provider = None

    @classmethod
    def provider(cls):
        """One provider per process, so request spacing holds across cases."""
        if cls._provider is None:
            from mapwright.llm.gemini import GeminiProvider, default_model

            if cls.record:
                cls._provider = CachedProvider(LLM_CACHE, GeminiProvider(cls.model or None))
            else:
                cls._provider = CachedProvider(LLM_CACHE, model=cls.model or default_model())
        return cls._provider


def _llm_system(system: str, repair: bool):
    def run_case(case: BenchmarkCase, directory: Path) -> None:
        session = LLMSession(LLMSettings.provider(), sample=LLMSettings.sample, repair=repair)
        source = DATASETS / case.id / "source.csv"
        outcome = run(source, case.contract, _references(case), system=system, session=session)
        write_run(outcome, directory)

    return run_case


SYSTEMS = {
    "perfect": perfect,
    "passthrough": passthrough,
    "rules": rules,
    "llm": _llm_system("llm", repair=False),
    "hybrid": _llm_system("hybrid", repair=True),
    "hybrid-norepair": _llm_system("hybrid", repair=False),
}
LLM_SYSTEMS = {"llm", "hybrid", "hybrid-norepair"}

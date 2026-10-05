"""Reading benchmark cases and system runs from disk.

A run directory looks like this:

    runs/<run-id>/
      run.json              {"system": "...", "model": "...", "date": "..."}
      D01/
        target.csv          contract fields plus _source_row
        result.json         mapping, detections, actions, escalations, usage

``_source_row`` is the 1-based source row each output row came from. The
scorer uses it with the manifest's row lineage to align output rows with
ground-truth rows, so a wrong primary key does not hide every other cell.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

from mapwright.contracts import Contract, load_contracts

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = load_contracts(ROOT / "contracts")
DATASETS = ROOT / "benchmark" / "datasets"
REFERENCE_CUSTOMERS = ROOT / "benchmark" / "reference_customers.csv"
SOURCE_ROW = "_source_row"


@dataclass
class BenchmarkCase:
    id: str
    manifest: dict
    source_header: list[str]
    source_rows: list[dict[str, str]]
    truth: list[dict[str, str]]
    contract: Contract

    @property
    def split(self) -> str:
        return self.manifest["split"]

    @property
    def key_field(self) -> str:
        return self.contract.primary_key[0]


@dataclass
class RunCase:
    rows: list[dict[str, str]]
    source_rows: list[int | None]
    result: dict = field(default_factory=dict)


def load_case(directory: str | Path) -> BenchmarkCase:
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    text = (directory / "source.csv").read_bytes().decode(manifest["encoding"])
    lines = list(csv.reader(io.StringIO(text)))
    header = lines[manifest["header_line"] - 1]
    source = [dict(zip(header, row)) for row in lines[manifest["header_line"]:]]
    with (directory / "ground_truth.csv").open(encoding="utf-8", newline="") as handle:
        truth = list(csv.DictReader(handle))
    return BenchmarkCase(
        id=manifest["dataset_id"],
        manifest=manifest,
        source_header=header,
        source_rows=source,
        truth=truth,
        contract=CONTRACTS[manifest["entity"]],
    )


def load_cases(directory: str | Path = DATASETS) -> list[BenchmarkCase]:
    return [load_case(path) for path in sorted(Path(directory).iterdir()) if (path / "manifest.json").exists()]


def load_run_case(directory: str | Path, contract: Contract) -> RunCase:
    directory = Path(directory)
    rows: list[dict[str, str]] = []
    source_rows: list[int | None] = []
    target = directory / "target.csv"
    if target.exists():
        with target.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or SOURCE_ROW not in reader.fieldnames:
                raise ValueError(f"{target} has no {SOURCE_ROW} column")
            for record in reader:
                raw = (record.get(SOURCE_ROW) or "").strip()
                source_rows.append(int(raw) if raw.isdigit() else None)
                rows.append({name: record.get(name) or "" for name in contract.field_names})

    result_path = directory / "result.json"
    result = json.loads(result_path.read_text("utf-8")) if result_path.exists() else {}
    return RunCase(rows=rows, source_rows=source_rows, result=result)


def load_reference_customer_ids() -> frozenset[str]:
    with REFERENCE_CUSTOMERS.open(encoding="utf-8", newline="") as handle:
        return frozenset(row["customer_id"] for row in csv.DictReader(handle))


def write_run_case(
    directory: str | Path,
    contract: Contract,
    rows: list[dict[str, str]],
    source_rows: list[int],
    result: dict,
) -> None:
    """Write one case of a run in the format the scorer reads."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "target.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow([SOURCE_ROW, *contract.field_names])
        for number, row in zip(source_rows, rows):
            writer.writerow([number, *(row.get(name, "") for name in contract.field_names)])
    (directory / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

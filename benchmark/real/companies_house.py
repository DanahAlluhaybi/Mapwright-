"""H04: a held-out case built from a real public export.

Source: the Companies House "Free Company Data Product" (BasicCompanyData),
a monthly CSV snapshot of companies registered in the UK. Download one part
from https://download.companieshouse.gov.uk/en_output.html and unzip it.

    python -m benchmark.real.companies_house extract BasicCompanyData-....csv

``extract`` takes a seeded random sample of companies registered in London
and incorporated within the contract's date range, and writes it to
benchmark/datasets/H04/source.csv exactly as the export has it: every
column, original headers, original values. Nothing is cleaned or
corrupted; the messiness is what Companies House publishes.

    python -m benchmark.real.companies_house build

``build`` writes ground_truth.csv and manifest.json from source.csv. The
mapping onto the customer contract was decided by hand, once, by reading
the export's documentation and the sampled values; the decisions are the
constants and functions below, so the ground truth is reproducible and
every choice can be inspected:

* customer_id   <- CompanyNumber, unchanged.
* full_name     <- CompanyName, in the contract's title case: each run of
  Latin letters starts upper case, the rest lower; a letter after an
  apostrophe stays lower ("ALLEN'S" -> "Allen's"). Acronyms follow the
  same rule ("(UK)" -> "(Uk)"); the contract defines no exception list.
* country_code  <- CountryOfOrigin. RegAddress.Country ("ENGLAND",
  "UNITED KINGDOM", blank) is the address country and is dropped.
* city          <- RegAddress.PostTown. Every sampled town is in London.
* customer_type <- CompanyCategory. Every category is a legal entity, so
  BUSINESS.
* signup_date   <- IncorporationDate, day first.
* is_active     <- CompanyStatus. "Liquidation" is false, "Active" and
  "Active - Proposal to Strike off" are true.
* email, phone, credit_limit: the export has no such columns.

Known limitation: part 1 of the export is sorted by name, so the sample
holds only names starting with a digit, A or B.

Contains public sector information licensed under the Open Government
Licence v3.0 (Companies House).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import random
import re
import sys
from pathlib import Path

from mapwright.canonical import format_date

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "benchmark" / "datasets" / "H04"
SEED = 204
ROWS = 300
FIRST_DATE = dt.date(2000, 1, 1)
LAST_DATE = dt.date(2026, 12, 31)


def _column(header: list[str], name: str) -> int:
    for index, value in enumerate(header):
        if value.strip() == name:
            return index
    raise KeyError(f"no {name} column in the export")


def _incorporated(text: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(text.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def eligible(row: list[str], town: int, incorporated: int) -> bool:
    """Registered in London and incorporated inside the contract's range."""
    if "london" not in row[town].casefold():
        return False
    date = _incorporated(row[incorporated])
    return date is not None and FIRST_DATE <= date <= LAST_DATE


def extract(export: Path, out: Path = OUT, rows: int = ROWS, seed: int = SEED) -> int:
    """Reservoir-sample eligible rows in one pass, then keep export order."""
    rng = random.Random(seed)
    sample: list[tuple[int, list[str]]] = []
    seen = 0
    with export.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        town = _column(header, "RegAddress.PostTown")
        incorporated = _column(header, "IncorporationDate")
        for position, row in enumerate(reader):
            if len(row) != len(header) or not eligible(row, town, incorporated):
                continue
            seen += 1
            if len(sample) < rows:
                sample.append((position, row))
            else:
                slot = rng.randrange(seen)
                if slot < rows:
                    sample[slot] = (position, row)

    sample.sort()
    out.mkdir(parents=True, exist_ok=True)
    with (out / "source.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(row for _, row in sample)
    print(f"{seen} eligible companies; wrote {len(sample)} to {out / 'source.csv'}")
    return len(sample)


# Ground truth --------------------------------------------------------------

MAPPING = {
    "customer_id": "CompanyNumber",
    "full_name": "CompanyName",
    "country_code": "CountryOfOrigin",
    "city": "RegAddress.PostTown",
    "customer_type": "CompanyCategory",
    "signup_date": "IncorporationDate",
    "is_active": "CompanyStatus",
}
MISSING = ["email", "phone", "credit_limit"]
FIELDS = [
    "customer_id",
    "full_name",
    "email",
    "phone",
    "country_code",
    "city",
    "customer_type",
    "signup_date",
    "credit_limit",
    "is_active",
]
COUNTRIES = {"united kingdom": "GB"}
INACTIVE_STATUSES = {"liquidation"}
LETTER_RUN = re.compile(r"(?<!['\u2019])[A-Za-z]+|(?<=['\u2019])[A-Za-z]+")


def title_case(text: str) -> str:
    def fix(match: re.Match) -> str:
        word = match.group(0)
        after_apostrophe = match.start() > 0 and text[match.start() - 1] in "'\u2019"
        return word.lower() if after_apostrophe else word[0].upper() + word[1:].lower()

    return LETTER_RUN.sub(fix, " ".join(text.split()))


def city(text: str) -> str:
    if "london" in text.casefold():
        return "London"
    raise ValueError(f"post town outside the sample rule: {text!r}")


def truth_row(row: dict[str, str]) -> dict[str, str]:
    status = row["CompanyStatus"].strip().casefold()
    incorporated = dt.datetime.strptime(row["IncorporationDate"].strip(), "%d/%m/%Y").date()
    return {
        "customer_id": row["CompanyNumber"].strip(),
        "full_name": title_case(row["CompanyName"]),
        "email": "",
        "phone": "",
        "country_code": COUNTRIES[row["CountryOfOrigin"].strip().casefold()],
        "city": city(row["RegAddress.PostTown"]),
        "customer_type": "BUSINESS",
        "signup_date": format_date(incorporated),
        "credit_limit": "",
        "is_active": "false" if status in INACTIVE_STATUSES else "true",
    }


def _cell_issue(target: str, source: str, truth: str) -> str:
    """Label for a cell whose source text differs from the ground truth."""
    if target in ("full_name", "customer_id"):
        return "S4"
    if target == "city" and source.strip().casefold() == truth.casefold():
        return "S4"
    if target == "is_active":
        return "V5"
    return "V1"


def _entry(issue: str, target: str | None, columns: list[str], unit: str, risk: str, **extra) -> dict:
    entry = {"issue": issue, "target": target, "columns": columns, "unit": unit, "risk": risk}
    entry.update(extra)
    return entry


def build(out: Path = OUT) -> dict:
    with (out / "source.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        lines = list(reader)
    by_name = {h.strip(): h for h in header}
    rows = [{name.strip(): value for name, value in zip(header, line)} for line in lines]
    truth = [truth_row(row) for row in rows]

    mapping = [{"source": [by_name[source]], "target": target} for target, source in MAPPING.items()]
    mapped = {by_name[s] for s in MAPPING.values()}
    dropped = [h for h in header if h not in mapped]

    issues = [_entry("M1", m["target"], m["source"], "column", "auto") for m in mapping]
    issues += [_entry("M5", None, [column], "column", "auto") for column in dropped]
    issues += [_entry("M6", target, [], "column", "review", unrecoverable=True) for target in MISSING]
    issues.append(_entry("V2", "signup_date", [by_name["IncorporationDate"]], "column", "review"))

    cells: dict[tuple[str, str], list[int]] = {}
    for number, (row, target_row) in enumerate(zip(rows, truth), start=1):
        for target, source in MAPPING.items():
            if target == "signup_date":
                continue
            if row[source] != target_row[target]:
                issue = _cell_issue(target, row[source], target_row[target])
                cells.setdefault((issue, target), []).append(number)
    for (issue, target), numbers in sorted(cells.items()):
        risk = "review" if issue in ("V4", "V7", "S5", "S3") else "auto"
        issues.append(_entry(issue, target, [by_name[MAPPING[target]]], "cell", risk, rows=numbers))

    manifest = {
        "dataset_id": "H04",
        "entity": "customer",
        "split": "heldout",
        "seed": SEED,
        "style": "companies_house_real",
        "pool": "real",
        "description": "Real export: Companies House basic company data, London companies incorporated since 2000.",
        "encoding": "utf-8",
        "header_line": 1,
        "source_rows": len(rows),
        "target_rows": len(truth),
        "column_mapping": mapping,
        "dropped_columns": dropped,
        "missing_target_columns": MISSING,
        "issues": issues,
        "row_lineage": {str(n): t["customer_id"] for n, t in enumerate(truth, start=1)},
    }

    with (out / "ground_truth.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(truth)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m benchmark.real.companies_house")
    commands = parser.add_subparsers(dest="command", required=True)
    extract_cmd = commands.add_parser("extract", help="sample the export into H04/source.csv")
    extract_cmd.add_argument("export", type=Path)
    commands.add_parser("build", help="write ground_truth.csv and manifest.json from source.csv")
    args = parser.parse_args(argv)
    if args.command == "extract":
        extract(args.export)
    else:
        manifest = build()
        print(f"wrote ground truth and manifest for {manifest['target_rows']} rows to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

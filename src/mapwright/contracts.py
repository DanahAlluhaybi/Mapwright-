"""Target contracts: loading the YAML definitions and checking tables against them.

Checks operate on canonical text (see ``mapwright.canonical``), where an
empty string means null.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

DECIMAL_TEXT = re.compile(r"^-?\d+\.\d{2}$")
DATE_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BOOLEAN_TEXT = {"true", "false"}


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    unique: bool = False
    pattern: str | None = None
    allowed_values: tuple[str, ...] | None = None
    min: str | int | float | None = None
    max: str | int | float | None = None
    max_length: int | None = None
    precision: int | None = None
    reference: str | None = None
    foreign_key: str | None = None


@dataclass(frozen=True)
class Contract:
    entity: str
    version: int
    primary_key: tuple[str, ...]
    fields: tuple[Field, ...]
    reference_values: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(f"{self.entity} has no field {name!r}")


@dataclass(frozen=True)
class Violation:
    row: int
    field: str
    rule: str
    value: str


def _load_reference(path: Path) -> frozenset[str]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        return frozenset(row[0] for row in reader if row)


def load_contract(path: str | Path) -> Contract:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    project_root = path.parent.parent

    fields = []
    references: dict[str, frozenset[str]] = {}
    for spec in raw["fields"]:
        allowed = spec.get("allowed_values")
        f = Field(
            name=spec["name"],
            type=spec["type"],
            required=spec.get("required", False),
            unique=spec.get("unique", False),
            pattern=spec.get("pattern"),
            allowed_values=tuple(str(v) for v in allowed) if allowed else None,
            min=spec.get("min"),
            max=spec.get("max"),
            max_length=spec.get("max_length"),
            precision=spec.get("precision"),
            reference=spec.get("reference"),
            foreign_key=spec.get("foreign_key"),
        )
        fields.append(f)
        if f.reference:
            references[f.name] = _load_reference(project_root / f.reference)

    return Contract(
        entity=raw["entity"],
        version=raw["version"],
        primary_key=tuple(raw["primary_key"]),
        fields=tuple(fields),
        reference_values=references,
    )


def load_contracts(directory: str | Path) -> dict[str, Contract]:
    contracts = {}
    for path in sorted(Path(directory).glob("*.yaml")):
        contract = load_contract(path)
        contracts[contract.entity] = contract
    return contracts


def _type_ok(f: Field, text: str) -> bool:
    if f.type == "string":
        return True
    if f.type == "boolean":
        return text in BOOLEAN_TEXT
    if f.type == "date":
        if not DATE_TEXT.match(text):
            return False
        try:
            dt.date.fromisoformat(text)
        except ValueError:
            return False
        return True
    if f.type == "decimal":
        if not DECIMAL_TEXT.match(text):
            return False
        if f.precision is not None:
            digits = len(text.replace("-", "").replace(".", "").lstrip("0") or "0")
            return digits <= f.precision
        return True
    raise ValueError(f"unknown field type {f.type!r}")


def _range_ok(f: Field, text: str) -> bool:
    if f.type == "decimal":
        try:
            value = Decimal(text)
        except InvalidOperation:
            return False
        if f.min is not None and value < Decimal(str(f.min)):
            return False
        if f.max is not None and value > Decimal(str(f.max)):
            return False
    elif f.type == "date":
        if f.min is not None and text < str(f.min):
            return False
        if f.max is not None and text > str(f.max):
            return False
    return True


def check_value(contract: Contract, f: Field, text: str) -> list[str]:
    """Return the names of the rules a single canonical value breaks."""
    if text == "":
        return ["required"] if f.required else []
    if not _type_ok(f, text):
        return ["type"]

    failed = []
    if f.pattern and not re.match(f.pattern, text):
        failed.append("pattern")
    if f.allowed_values is not None and text not in f.allowed_values:
        failed.append("allowed_values")
    if f.max_length is not None and len(text) > f.max_length:
        failed.append("max_length")
    if not _range_ok(f, text):
        failed.append("range")
    if f.name in contract.reference_values and text not in contract.reference_values[f.name]:
        failed.append("reference")
    return failed


def check_table(
    contract: Contract,
    rows: list[dict[str, str]],
    foreign_keys: dict[str, frozenset[str]] | None = None,
) -> list[Violation]:
    """Check every row against the contract.

    ``foreign_keys`` maps a field name to the set of values it may reference.
    Rows are numbered from 1 in the returned violations.
    """
    violations: list[Violation] = []
    seen: dict[str, dict[str, int]] = {f.name: {} for f in contract.fields if f.unique}

    for number, row in enumerate(rows, start=1):
        for f in contract.fields:
            text = row.get(f.name, "")
            for rule in check_value(contract, f, text):
                violations.append(Violation(number, f.name, rule, text))

            if text and f.unique:
                if text in seen[f.name]:
                    violations.append(Violation(number, f.name, "unique", text))
                else:
                    seen[f.name][text] = number

            if text and f.foreign_key and foreign_keys and f.name in foreign_keys:
                if text not in foreign_keys[f.name]:
                    violations.append(Violation(number, f.name, "foreign_key", text))

    return violations

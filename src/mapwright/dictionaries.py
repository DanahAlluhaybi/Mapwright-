"""Rule dictionaries: header synonyms, value variants and placeholder tokens.

The dictionaries live as YAML in ``mapwright/rules/`` so they can be read
and extended without touching code.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from .contracts import Contract, Field
from .normalize import match_key

RULES = Path(__file__).resolve().parent / "rules"


@lru_cache(maxsize=None)
def _load(name: str) -> dict:
    return yaml.safe_load((RULES / name).read_text(encoding="utf-8"))


def _text(value: object) -> str:
    if isinstance(value, bool):
        raise ValueError(f"unquoted YAML boolean {value!r} in a rule dictionary")
    return str(value)


@dataclass(frozen=True)
class ValueDictionary:
    """Maps source spellings to canonical values, ignoring case and spacing."""

    issue: str
    lookup: dict[str, str]

    def get(self, text: str) -> str | None:
        return self.lookup.get(match_key(text))

    @property
    def canonical(self) -> set[str]:
        return set(self.lookup.values())


def _build_lookup(canonical: list[str], variants: dict) -> dict[str, str]:
    lookup = {match_key(value): value for value in canonical}
    for value, spellings in (variants or {}).items():
        for spelling in spellings:
            lookup.setdefault(match_key(_text(spelling)), _text(value))
    return lookup


def value_dictionary(contract: Contract, field: Field) -> ValueDictionary | None:
    """The dictionary for a field with a closed set of values, if it has one."""
    rules = _load("values.yaml")
    if field.type == "boolean":
        spec = rules["boolean"]
        return ValueDictionary(spec["issue"], _build_lookup(["true", "false"], spec["values"]))

    canonical = list(field.allowed_values or contract.reference_values.get(field.name, ()))
    if not canonical:
        return None
    spec = rules["fields"].get(field.name, {})
    return ValueDictionary(spec.get("issue", "V1"), _build_lookup(canonical, spec.get("values")))


def missing_tokens() -> frozenset[str]:
    return frozenset(match_key(_text(t)) for t in _load("values.yaml")["missing_tokens"])


def unit_tokens(contract: Contract) -> dict[str, str]:
    """Currency spellings that may sit inside an amount, mapped to their code."""
    tokens: dict[str, str] = {}
    if "currency" in contract.field_names:
        dictionary = value_dictionary(contract, contract.field("currency"))
        codes = dictionary.canonical
    else:
        codes = {"SAR"}
    spec = _load("values.yaml")["fields"]["currency"]["values"]
    for code in codes:
        tokens[code] = code
        for spelling in spec.get(code, []):
            tokens[_text(spelling)] = code
    return tokens


def header_synonyms(entity: str) -> dict[str, list[str]]:
    return {field: [_text(s) for s in names] for field, names in _load("headers.yaml").get(entity, {}).items()}


def part_synonyms() -> dict[str, list[list[str]]]:
    return {
        field: [[_text(s) for s in part] for part in parts] for field, parts in _load("headers.yaml")["parts"].items()
    }

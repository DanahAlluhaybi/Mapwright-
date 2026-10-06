"""Column mapping by rules: header names first, then column contents.

Each source column maps to at most one target field. Columns that match
nothing are dropped as irrelevant; target fields that nothing maps to are
reported as missing.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

from .contracts import Contract
from .dictionaries import header_synonyms, part_synonyms, value_dictionary
from .ingest import SourceTable
from .normalize import DATE_FORMATS, is_email, looks_like_phone, parse_date

SIMILARITY = 0.85
CONTENT_SHARE = 0.8
NON_LATIN = re.compile(r"[^\x00-\x7F]")


def normalize_header(header: str) -> str:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", header.strip())
    return re.sub(r"[\W_]+", " ", spaced.lower()).strip()


@dataclass
class ColumnMapping:
    sources: list[str]
    target: str
    method: str
    confidence: float
    issue: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MappingResult:
    mappings: list[ColumnMapping] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def for_target(self, target: str) -> ColumnMapping | None:
        return next((m for m in self.mappings if m.target == target), None)


def _mapping_issue(sources: list[str], target: str) -> str | None:
    if len(sources) > 1:
        return "M3"
    header = sources[0]
    if header.strip() == target:
        return None
    return "M2" if NON_LATIN.search(header) else "M1"


def _name_score(header: str, names: list[str]) -> tuple[float, str]:
    """1.0 for an exact synonym, else the best similarity ratio."""
    norm = normalize_header(header)
    compact = norm.replace(" ", "")
    best = 0.0
    method = "similarity"
    for name in names:
        candidate = normalize_header(name)
        if norm == candidate or compact == candidate.replace(" ", ""):
            return 1.0, "synonym"
        best = max(best, SequenceMatcher(None, norm, candidate).ratio())
    return best, method


def _content_share(contract: Contract, target: str, values: list[str]) -> float:
    filled = [v.strip() for v in values if v.strip()]
    if not filled:
        return 0.0
    f = contract.field(target)
    if target == "email":
        test = lambda v: is_email(v.lower())  # noqa: E731
    elif target == "phone":
        test = looks_like_phone
    elif f.type == "date":
        test = lambda v: any(parse_date(v, d.pattern) for d in DATE_FORMATS)  # noqa: E731
    else:
        dictionary = value_dictionary(contract, f)
        if dictionary is None:
            return 0.0
        test = lambda v: dictionary.get(v) is not None  # noqa: E731
    return sum(1 for v in filled if test(v)) / len(filled)


def map_columns(table: SourceTable, contract: Contract) -> MappingResult:
    synonyms = header_synonyms(contract.entity)
    candidates: list[tuple[float, str, str, str]] = []
    for header in table.header:
        for target in contract.field_names:
            names = [target, *synonyms.get(target, [])]
            score, method = _name_score(header, names)
            if score >= SIMILARITY:
                candidates.append((score, header, target, method))

    result = MappingResult()
    used_headers: set[str] = set()
    for score, header, target, method in sorted(candidates, key=lambda c: -c[0]):
        if header in used_headers or result.for_target(target):
            continue
        used_headers.add(header)
        result.mappings.append(ColumnMapping([header], target, method, round(score, 2)))

    for target, parts in part_synonyms().items():
        if target not in contract.field_names or result.for_target(target):
            continue
        sources = []
        for names in parts:
            match = next((h for h in table.header if h not in used_headers and _name_score(h, names)[0] == 1.0), None)
            sources.append(match)
        if all(sources):
            used_headers.update(sources)
            result.mappings.append(ColumnMapping(sources, target, "parts", 1.0))

    for header in table.header:
        if header in used_headers:
            continue
        open_targets = [t for t in contract.field_names if not result.for_target(t)]
        scored = [(t, _content_share(contract, t, table.column(header))) for t in open_targets]
        scored = [(t, s) for t, s in scored if s >= CONTENT_SHARE]
        if len(scored) == 1:
            target, share = scored[0]
            used_headers.add(header)
            result.mappings.append(ColumnMapping([header], target, "content", round(share, 2)))

    order = {name: i for i, name in enumerate(contract.field_names)}
    result.mappings.sort(key=lambda m: order[m.target])
    for m in result.mappings:
        m.issue = _mapping_issue(m.sources, m.target)
    result.dropped = [h for h in table.header if h not in used_headers]
    result.missing = [t for t in contract.field_names if not result.for_target(t)]
    return result

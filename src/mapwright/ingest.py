"""Reading a legacy export into a table of text values.

Every value stays text at this stage. Nothing is parsed or cleaned, so the
raw table is an exact record of what the source file said.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ENCODINGS = ("utf-8", "cp1256")
DELIMITERS = ",;\t|"


@dataclass(frozen=True)
class SourceTable:
    """A source file as read: header, rows of text, and how it was read.

    ``row_numbers`` are 1-based and count data lines after the header, so
    they stay stable when blank lines are skipped.
    """

    header: list[str]
    rows: list[dict[str, str]]
    row_numbers: list[int]
    encoding: str
    delimiter: str
    header_line: int

    def column(self, name: str) -> list[str]:
        return [row[name] for row in self.rows]

    def settings(self) -> dict:
        return {"encoding": self.encoding, "delimiter": self.delimiter, "header_line": self.header_line}


def detect_encoding(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for encoding in ENCODINGS:
        try:
            data.decode(encoding)
        except UnicodeDecodeError:
            continue
        return encoding
    raise ValueError("could not decode the file as UTF-8 or Windows-1256")


def detect_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:50])
    try:
        return csv.Sniffer().sniff(sample, delimiters=DELIMITERS).delimiter
    except csv.Error:
        return ","


def detect_header_line(lines: list[list[str]]) -> int:
    """Return the 1-based line holding the header.

    Title rows above a header ("Sales Orders Export") are short. The header
    is the first line as wide as the data and filled in across that width.
    """
    widths = Counter(len(line) for line in lines if any(cell.strip() for cell in line))
    if not widths:
        raise ValueError("the file has no data")
    width = widths.most_common(1)[0][0]
    for number, line in enumerate(lines, start=1):
        filled = sum(1 for cell in line if cell.strip())
        if len(line) == width and filled == width:
            return number
    return 1


def _unique_headers(header: list[str]) -> list[str]:
    seen: Counter[str] = Counter()
    unique = []
    for name in header:
        name = name if name.strip() else "column"
        seen[name] += 1
        unique.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return unique


def parse_text(
    text: str, encoding: str = "utf-8", delimiter: str | None = None, header_line: int | None = None
) -> SourceTable:
    delimiter = delimiter or detect_delimiter(text)
    lines = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    header_line = header_line or detect_header_line(lines)
    header = _unique_headers(lines[header_line - 1])

    rows, numbers = [], []
    for number, line in enumerate(lines[header_line:], start=1):
        if not any(cell.strip() for cell in line):
            continue
        cells = (line + [""] * len(header))[: len(header)]
        rows.append(dict(zip(header, cells)))
        numbers.append(number)
    return SourceTable(header, rows, numbers, encoding, delimiter, header_line)


def read_source(path: str | Path, **settings) -> SourceTable:
    """Read a CSV export. Settings not given are detected from the file."""
    data = Path(path).read_bytes()
    encoding = settings.pop("encoding", None) or detect_encoding(data)
    return parse_text(data.decode(encoding), encoding=encoding, **settings)

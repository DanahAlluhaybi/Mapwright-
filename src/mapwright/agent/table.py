"""A plain table of text: what the agent works on.

Every cell is a string, as it was read. ``row_ids`` keep each row's line
number in the original file, so any change can be traced back to it.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from ..excel import excel_to_csv, is_excel
from ..ingest import detect_encoding, parse_text


@dataclass
class Table:
    columns: list[str]
    rows: list[list[str]]
    row_ids: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.row_ids:
            self.row_ids = list(range(1, len(self.rows) + 1))

    @property
    def size(self) -> int:
        return len(self.rows)

    def index(self, name: str) -> int:
        try:
            return self.columns.index(name)
        except ValueError:
            raise KeyError(f"no column {name!r}") from None

    def column(self, name: str) -> list[str]:
        i = self.index(name)
        return [row[i] for row in self.rows]

    def copy(self) -> Table:
        return Table(list(self.columns), [list(r) for r in self.rows], list(self.row_ids))

    def records(self, limit: int | None = None) -> list[dict[str, str]]:
        rows = self.rows if limit is None else self.rows[:limit]
        return [dict(zip(self.columns, r)) for r in rows]

    def to_csv(self, with_row_ids: bool = False) -> bytes:
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow((["_row"] if with_row_ids else []) + self.columns)
        for rid, row in zip(self.row_ids, self.rows):
            writer.writerow(([rid] if with_row_ids else []) + row)
        return out.getvalue().encode("utf-8")

    def to_xlsx(self) -> bytes:
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Data"
        sheet.append(self.columns)
        for row in self.rows:
            sheet.append(row)
        out = io.BytesIO()
        workbook.save(out)
        return out.getvalue()


def read_table(data: bytes, filename: str = "data.csv", sheet: str | None = None) -> tuple[Table, dict]:
    """Read an upload. Returns the table and how it was read."""
    info: dict = {"filename": filename}
    if is_excel(filename):
        data, info["sheet"] = excel_to_csv(data, sheet)
    encoding = detect_encoding(data)
    source = parse_text(data.decode(encoding), encoding=encoding)
    info.update(source.settings())
    table = Table(list(source.header), [[r[c] for c in source.header] for r in source.rows], list(source.row_numbers))
    return table, info


def load_csv(data: bytes) -> Table:
    """Read a table this module wrote (UTF-8, with or without ``_row``)."""
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    header, body = rows[0], rows[1:]
    if header and header[0] == "_row":
        return Table(header[1:], [r[1:] for r in body], [int(r[0]) for r in body])
    return Table(header, body)

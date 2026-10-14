"""Reading Excel workbooks as the CSV text the pipeline expects.

One sheet becomes one table. Cell values are written as the text a person
sees in Excel, without fixing anything: cleaning is the pipeline's job,
and a value that is wrong in the workbook should be wrong in the source
so the pipeline can find it.

Needs the ``openpyxl`` package (``pip install openpyxl``).
"""

from __future__ import annotations

import csv
import datetime as dt
import io

EXCEL_SUFFIXES = (".xlsx", ".xlsm")


class ExcelError(ValueError):
    """The workbook could not be read, or the sheet does not exist."""


def is_excel(filename: str) -> bool:
    return filename.lower().endswith(EXCEL_SUFFIXES)


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def sheet_names(data: bytes) -> list[str]:
    workbook = _open(data)
    try:
        return list(workbook.sheetnames)
    finally:
        workbook.close()


def _open(data: bytes):
    try:
        import openpyxl
    except ImportError as error:
        raise ExcelError("reading Excel files needs openpyxl: run  pip install openpyxl") from error
    try:
        return openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as error:
        raise ExcelError(f"not a readable Excel workbook ({type(error).__name__})") from error


def excel_to_csv(data: bytes, sheet: str | None = None) -> tuple[bytes, str]:
    """Return one sheet as UTF-8 CSV bytes, and the sheet's name.

    Without ``sheet`` the first sheet is used. Fully empty trailing rows
    and columns are dropped, since Excel often reports a used range larger
    than the data.
    """
    workbook = _open(data)
    try:
        names = list(workbook.sheetnames)
        name = sheet or names[0]
        if name not in names:
            raise ExcelError(f"no sheet {name!r}; the workbook has: {', '.join(names)}")
        rows = [[_text(v) for v in row] for row in workbook[name].iter_rows(values_only=True)]
    finally:
        workbook.close()

    while rows and not any(rows[-1]):
        rows.pop()
    width = max((max((i + 1 for i, v in enumerate(r) if v), default=0) for r in rows), default=0)
    if not rows or width == 0:
        raise ExcelError(f"sheet {name!r} is empty")

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    for row in rows:
        writer.writerow((row + [""] * width)[:width])
    return out.getvalue().encode("utf-8"), name

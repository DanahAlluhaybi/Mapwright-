import datetime as dt
import importlib.util
import io
import unittest

from mapwright.excel import ExcelError, excel_to_csv, is_excel


@unittest.skipUnless(importlib.util.find_spec("openpyxl"), "openpyxl not installed")
class ExcelTest(unittest.TestCase):
    def book(self, rows, name="Data"):
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = name
        for row in rows:
            sheet.append(row)
        out = io.BytesIO()
        workbook.save(out)
        return out.getvalue()

    def test_values_keep_what_excel_shows(self):
        data = self.book(
            [
                ["id", "joined", "limit", "active", "note"],
                ["C-1", dt.datetime(2024, 3, 5), 2500.0, True, None],
                ["C-2", dt.datetime(2024, 3, 5, 14, 30), 12.5, False, "  spaces kept  "],
                [None, None, None, None, None],
            ]
        )
        text, sheet = excel_to_csv(data)
        self.assertEqual(sheet, "Data")
        self.assertEqual(
            text.decode("utf-8").splitlines(),
            [
                "id,joined,limit,active,note",
                "C-1,2024-03-05,2500,TRUE,",
                "C-2,2024-03-05 14:30:00,12.5,FALSE,  spaces kept  ",
            ],
        )

    def test_sheet_choice_and_errors(self):
        data = self.book([["a"], ["1"]], name="Customers")
        self.assertEqual(excel_to_csv(data, "Customers")[1], "Customers")
        with self.assertRaisesRegex(ExcelError, "Customers"):
            excel_to_csv(data, "Orders")
        with self.assertRaises(ExcelError):
            excel_to_csv(b"not a workbook")
        with self.assertRaisesRegex(ExcelError, "empty"):
            excel_to_csv(self.book([]))

    def test_is_excel(self):
        self.assertTrue(is_excel("Export.XLSX"))
        self.assertFalse(is_excel("export.csv"))


if __name__ == "__main__":
    unittest.main()

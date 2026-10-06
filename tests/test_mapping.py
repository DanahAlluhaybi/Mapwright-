import unittest
from pathlib import Path

from mapwright.contracts import load_contracts
from mapwright.ingest import parse_text
from mapwright.mapping import map_columns, normalize_header

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = load_contracts(ROOT / "contracts")


def mapped(result):
    return {m.target: (m.sources, m.issue) for m in result.mappings}


class NormalizeHeaderTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(normalize_header("CUST_NO"), "cust no")
        self.assertEqual(normalize_header("Order #"), "order")
        self.assertEqual(normalize_header("signupDate"), "signup date")
        self.assertEqual(normalize_header("رقم العميل"), "رقم العميل")


class MapColumnsTest(unittest.TestCase):
    def test_renamed_and_exact_headers(self):
        table = parse_text("Customer ID,email,Phone Number\nC-1,a@b.co,0501234567\n")
        result = map_columns(table, CONTRACTS["customer"])
        self.assertEqual(mapped(result)["customer_id"], (["Customer ID"], "M1"))
        self.assertEqual(mapped(result)["email"], (["email"], None))
        self.assertEqual(mapped(result)["phone"], (["Phone Number"], "M1"))

    def test_arabic_headers_are_m2(self):
        table = parse_text("رقم العميل,اسم العميل\nC-1,سارة\n")
        result = map_columns(table, CONTRACTS["customer"])
        self.assertEqual(mapped(result)["full_name"], (["اسم العميل"], "M2"))

    def test_split_name_columns_combine(self):
        table = parse_text("customer_id,first_name,last_name\nC-1,Sara,Ali\n")
        result = map_columns(table, CONTRACTS["customer"])
        self.assertEqual(mapped(result)["full_name"], (["first_name", "last_name"], "M3"))

    def test_unmatched_columns_dropped_and_missing_fields_listed(self):
        table = parse_text("Order #,ROW_HASH\nSO-1,2df2aa0b\n")
        result = map_columns(table, CONTRACTS["sales_order"])
        self.assertEqual(result.dropped, ["ROW_HASH"])
        self.assertIn("channel", result.missing)
        self.assertNotIn("order_id", result.missing)

    def test_unknown_header_mapped_by_content(self):
        table = parse_text("customer_id,contact\nC-1,sara@example.com\nC-2,ali@example.com\n")
        result = map_columns(table, CONTRACTS["customer"])
        self.assertEqual(result.for_target("email").method, "content")

    def test_one_column_per_field(self):
        table = parse_text("email,Email Address\na@b.co,c@d.co\n")
        result = map_columns(table, CONTRACTS["customer"])
        self.assertEqual(mapped(result)["email"], (["email"], None))
        self.assertEqual(result.dropped, ["Email Address"])


if __name__ == "__main__":
    unittest.main()

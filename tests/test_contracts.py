import unittest
from pathlib import Path

from mapwright.contracts import check_table, check_value, load_contracts

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = load_contracts(ROOT / "contracts")


def valid_customer(**overrides):
    row = {
        "customer_id": "C-100001",
        "full_name": "Sara Alharbi",
        "email": "sara@example.com",
        "phone": "+966501234567",
        "country_code": "SA",
        "city": "Jeddah",
        "customer_type": "INDIVIDUAL",
        "signup_date": "2024-03-05",
        "credit_limit": "12500.00",
        "is_active": "true",
    }
    row.update(overrides)
    return row


class LoadContractsTest(unittest.TestCase):
    def test_both_entities_load(self):
        self.assertEqual(set(CONTRACTS), {"customer", "sales_order"})
        self.assertEqual(CONTRACTS["customer"].primary_key, ("customer_id",))

    def test_city_reference_list_is_loaded(self):
        cities = CONTRACTS["customer"].reference_values["city"]
        self.assertIn("Jeddah", cities)
        self.assertNotIn("city", cities)


class CheckValueTest(unittest.TestCase):
    def setUp(self):
        self.contract = CONTRACTS["customer"]

    def rules(self, field, text):
        return check_value(self.contract, self.contract.field(field), text)

    def test_valid_values_pass(self):
        for field, text in valid_customer().items():
            self.assertEqual(self.rules(field, text), [], field)

    def test_null_only_fails_when_required(self):
        self.assertEqual(self.rules("email", ""), [])
        self.assertEqual(self.rules("customer_type", ""), ["required"])

    def test_type_failures(self):
        self.assertEqual(self.rules("signup_date", "05/03/2024"), ["type"])
        self.assertEqual(self.rules("signup_date", "2024-02-30"), ["type"])
        self.assertEqual(self.rules("credit_limit", "12,500.00"), ["type"])
        self.assertEqual(self.rules("credit_limit", "12500"), ["type"])
        self.assertEqual(self.rules("is_active", "Y"), ["type"])

    def test_rule_failures(self):
        self.assertEqual(self.rules("email", "Sara@Example.com"), ["pattern"])
        self.assertEqual(self.rules("phone", "0501234567"), ["pattern"])
        self.assertEqual(self.rules("country_code", "KSA"), ["allowed_values"])
        self.assertEqual(self.rules("city", "Jedda"), ["reference"])
        self.assertEqual(self.rules("credit_limit", "-5.00"), ["range"])
        self.assertEqual(self.rules("signup_date", "1999-12-31"), ["range"])


class CheckTableTest(unittest.TestCase):
    def test_duplicate_keys_are_reported_once_per_repeat(self):
        rows = [valid_customer(), valid_customer(), valid_customer(customer_id="C-100002")]
        violations = check_table(CONTRACTS["customer"], rows)
        self.assertEqual([(v.row, v.field, v.rule) for v in violations], [(2, "customer_id", "unique")])

    def test_foreign_keys(self):
        order = {
            "order_id": "SO-1", "customer_id": "C-404", "order_date": "2024-01-01",
            "currency": "SAR", "amount": "10.00", "status": "PENDING", "channel": "ONLINE",
        }
        violations = check_table(
            CONTRACTS["sales_order"], [order], foreign_keys={"customer_id": frozenset({"C-1"})}
        )
        self.assertEqual([(v.field, v.rule) for v in violations], [("customer_id", "foreign_key")])


if __name__ == "__main__":
    unittest.main()

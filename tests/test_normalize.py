import unittest
from decimal import Decimal

from mapwright.normalize import (
    clean_spaces,
    infer_date_formats,
    normalize_phone,
    parse_date,
    parse_number,
    title_if_uniform,
)

UNITS = {"SAR": "SAR", "SR": "SAR", "ريال": "SAR", "USD": "USD", "$": "USD", "US$": "USD"}


class TextTest(unittest.TestCase):
    def test_clean_spaces(self):
        self.assertEqual(clean_spaces("  Fahad   Alomari "), "Fahad Alomari")

    def test_title_case_only_for_uniform_latin_text(self):
        self.assertEqual(title_if_uniform("RANA ALMUTAIRI"), "Rana Almutairi")
        self.assertEqual(title_if_uniform("gulf star foods"), "Gulf Star Foods")
        self.assertEqual(title_if_uniform("al-harbi"), "Al-Harbi")
        self.assertEqual(title_if_uniform("McKenzie Trading"), "McKenzie Trading")
        self.assertEqual(title_if_uniform("عبير نصر"), "عبير نصر")


class PhoneTest(unittest.TestCase):
    def test_saudi_local_shapes(self):
        for text in ["0501234567", "501234567", "966501234567", "00966 50 123 4567", "+966 50 123 4567"]:
            with self.subTest(text=text):
                self.assertEqual(normalize_phone(text), "+966501234567")

    def test_international_prefixes(self):
        self.assertEqual(normalize_phone("00447211550394"), "+447211550394")
        self.assertEqual(normalize_phone("+971512486515"), "+971512486515")

    def test_unreadable_numbers(self):
        for text in ["123456", "0123", "phone", "+0501234567"]:
            with self.subTest(text=text):
                self.assertIsNone(normalize_phone(text))


class DateTest(unittest.TestCase):
    def test_parse_drops_time(self):
        self.assertEqual(parse_date("2024-12-13 09:51:18", "%Y-%m-%d %H:%M:%S").isoformat(), "2024-12-13")

    def test_day_first_settled_by_evidence(self):
        inference = infer_date_formats(["05/03/2023", "25/06/2010", "06/11/2023"])
        self.assertEqual(inference.formats, ["%d/%m/%Y"])
        self.assertEqual(inference.ambiguous, [])
        self.assertEqual(inference.evidence, {"%d/%m/%Y": 1})

    def test_month_first_settled_by_evidence(self):
        inference = infer_date_formats(["05/17/2023", "09/19/2018", "03/04/2021"])
        self.assertEqual(inference.formats, ["%m/%d/%Y"])
        self.assertEqual(inference.ambiguous, [])

    def test_no_evidence_is_ambiguous(self):
        inference = infer_date_formats(["05/03/2023", "01/12/2010"])
        self.assertEqual(len(inference.ambiguous), 1)

    def test_timestamps_and_month_names(self):
        self.assertTrue(infer_date_formats(["2024-12-13 09:51:18"]).has_time)
        self.assertEqual(infer_date_formats(["08-Jan-2026", "13-Mar-2020"]).formats, ["%d-%b-%Y"])

    def test_mixed_formats_and_leftovers(self):
        inference = infer_date_formats(["2024-01-02", "2024-01-03", "2024/02/05", "soon", ""])
        self.assertEqual(inference.formats, ["%Y-%m-%d", "%Y/%m/%d"])
        self.assertEqual(inference.unparsed, [3])


class NumberTest(unittest.TestCase):
    def test_plain(self):
        parsed = parse_number("19766.49")
        self.assertEqual(parsed.text, "19766.49")
        self.assertFalse(parsed.separators or parsed.embedded_unit)

    def test_thousands_separators(self):
        parsed = parse_number("12,500.00")
        self.assertEqual(parsed.value, Decimal("12500.00"))
        self.assertTrue(parsed.separators)
        self.assertFalse(parsed.embedded_unit)

    def test_currency_prefix_and_suffix(self):
        self.assertEqual(parse_number("SAR 1,234.50", UNITS).unit, "SAR")
        self.assertEqual(parse_number("1234.5 SR", UNITS).text, "1234.50")
        self.assertEqual(parse_number("US$ 10", UNITS).unit, "USD")

    def test_scale_suffix(self):
        parsed = parse_number("12.5K")
        self.assertEqual(parsed.text, "12500.00")
        self.assertTrue(parsed.scaled)

    def test_arabic_indic_digits_and_word(self):
        parsed = parse_number("٤٥٠٠ ريال", UNITS)
        self.assertEqual(parsed.text, "4500.00")
        self.assertTrue(parsed.native_digits)
        self.assertEqual(parsed.unit, "SAR")

    def test_negative_kept(self):
        self.assertEqual(parse_number("-19026.53").text, "-19026.53")

    def test_rejected(self):
        for text in ["", "abc", "12,50", "SAR 10 USD", "1.2.3"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_number(text, UNITS))

    def test_unit_tokens_need_word_boundaries(self):
        self.assertIsNone(parse_number("SARA 10", UNITS))


if __name__ == "__main__":
    unittest.main()

from datetime import date
from pathlib import Path
import unittest

from eluno_pos.tax import TaxRateTable, calculate_inclusive_gst, calculate_line_tax


ROOT = Path(__file__).resolve().parents[1]


class InclusiveGstTests(unittest.TestCase):
    def test_reference_invoice_lens_line_matches_seed_rounding(self) -> None:
        # Rs 9,499.00 less Rs 1,424.85 at 5% GST: sample invoice line 2.
        result = calculate_line_tax(949900, 1, 142485, 500)
        self.assertEqual(807415, result.gross_paise)
        self.assertEqual(768967, result.taxable_paise)
        self.assertEqual(19224, result.cgst_paise)
        self.assertEqual(19224, result.sgst_paise)
        self.assertEqual(38448, result.tax_paise)
        self.assertTrue(result.reconciles())

    def test_odd_tax_paise_assigns_half_up_to_cgst_and_remainder_to_sgst(self) -> None:
        result = calculate_inclusive_gst(100, 1800)
        self.assertEqual(85, result.taxable_paise)
        self.assertEqual(15, result.tax_paise)
        self.assertEqual(8, result.cgst_paise)
        self.assertEqual(7, result.sgst_paise)
        self.assertTrue(result.reconciles())

    def test_discount_is_applied_before_tax_is_backed_out(self) -> None:
        result = calculate_line_tax(10000, 2, 1000, 1800)
        self.assertEqual(19000, result.gross_paise)
        self.assertEqual(16102, result.taxable_paise)
        self.assertEqual(2898, result.tax_paise)
        self.assertTrue(result.reconciles())

    def test_effective_dated_fixture_rates_are_loaded_from_data(self) -> None:
        rates = TaxRateTable.from_file(ROOT / "starter-kit" / "data" / "tax_rates.json")
        self.assertEqual(1200, rates.rate_for("90015000", date(2025, 9, 21)).rate_bps)
        self.assertEqual(500, rates.rate_for("90015000", date(2025, 9, 22)).rate_bps)


if __name__ == "__main__":
    unittest.main()


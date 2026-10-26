from datetime import date
import json
from pathlib import Path
import unittest

from eluno_pos.services import CustomerPrescriptionService


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "starter-kit" / "data"
CUSTOMERS = json.loads((DATA / "customers.json").read_text(encoding="utf-8"))
PRESCRIPTIONS = json.loads((DATA / "prescriptions.json").read_text(encoding="utf-8"))


class FakeCustomers:
    def __init__(self, rows):
        self.rows = rows

    def search_customers_by_phone(self, phone):
        return [row for row in self.rows if row["phone"] == phone]


class FakePrescriptions:
    def __init__(self, rows):
        self.rows = rows

    def prescriptions_by_phone(self, phone):
        return [row for row in self.rows if row["phone"] == phone]


class CustomerPrescriptionServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = CustomerPrescriptionService(FakeCustomers(CUSTOMERS), FakePrescriptions(PRESCRIPTIONS))

    def test_selects_latest_valid_prescription(self):
        result = self.service.lookup("+919000000001", date(2026, 10, 26))
        self.assertTrue(result.is_ready)
        self.assertEqual("80001001", result.customer.customer_id)
        self.assertEqual("RX5002", result.prescription.rx_id)
        self.assertEqual(
            frozenset(tag.strip() for tag in CUSTOMERS[1].get("tags", "").split(",") if tag.strip()),
            result.customer.tags,
        )

    def test_skips_an_expired_newer_prescription_for_an_older_valid_one(self):
        customer = CUSTOMERS[0]
        older_valid = dict(PRESCRIPTIONS[0], rx_id="OLD", date="2026-01-01", valid_until="2027-01-01")
        newer_expired = dict(PRESCRIPTIONS[0], rx_id="NEW", date="2026-02-01", valid_until="2026-02-15")
        service = CustomerPrescriptionService(FakeCustomers([customer]), FakePrescriptions([newer_expired, older_valid]))
        result = service.lookup(customer["phone"], date(2026, 3, 1))
        self.assertTrue(result.is_ready)
        self.assertEqual("OLD", result.prescription.rx_id)

    def test_does_not_select_a_prescription_dated_after_business_date(self):
        customer = CUSTOMERS[0]
        future = dict(PRESCRIPTIONS[0], rx_id="FUTURE", date="2026-10-27", valid_until="2027-10-27")
        past_valid = dict(PRESCRIPTIONS[0], rx_id="CURRENT", date="2026-10-20", valid_until="2027-10-20")
        service = CustomerPrescriptionService(
            FakeCustomers([customer]), FakePrescriptions([future, past_valid])
        )

        result = service.lookup(customer["phone"], date(2026, 10, 26))

        self.assertTrue(result.is_ready)
        self.assertEqual("CURRENT", result.prescription.rx_id)

    def test_no_customer_is_explicit(self):
        result = self.service.lookup("+919000009999", date(2026, 10, 26))
        self.assertEqual("customer_not_found", result.code)
        self.assertIsNone(result.customer)

    def test_multiple_customer_matches_are_not_silently_selected(self):
        first = CUSTOMERS[0]
        second = dict(first, id=99999999)
        service = CustomerPrescriptionService(FakeCustomers([first, second]), FakePrescriptions(PRESCRIPTIONS))
        result = service.lookup(first["phone"], date(2026, 1, 1))
        self.assertEqual("customer_ambiguous", result.code)
        self.assertIn("More than one", result.message)

    def test_no_valid_prescription_does_not_fall_back_to_expired(self):
        result = self.service.lookup("+919000000008", date(2026, 10, 26))
        self.assertEqual("no_valid_prescription", result.code)
        self.assertIsNone(result.prescription)

    def test_mismatched_prescription_is_rejected(self):
        customer = CUSTOMERS[0]
        mismatched = dict(PRESCRIPTIONS[0], customer_id=80001001)
        service = CustomerPrescriptionService(FakeCustomers([customer]), FakePrescriptions([mismatched]))
        result = service.lookup(customer["phone"], date(2026, 1, 1))
        self.assertEqual("prescription_mismatch", result.code)


if __name__ == "__main__":
    unittest.main()

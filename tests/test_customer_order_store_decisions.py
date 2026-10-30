import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from eluno_pos.customer_accounts import (
    CustomerAccountStore,
    CustomerOrderConflict,
    CustomerOrderStore,
)


class CustomerOrderStoreDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "orders.sqlite3"
        self.accounts = CustomerAccountStore(self.database)
        self.customer = self.accounts.create_account(
            "7401",
            "+919876543210",
            "Mira Rao",
            "SecureCustomer@2026",
        )
        self.orders = CustomerOrderStore(self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def create_order(self, key, store="HSR", payment_status="paid"):
        order, duplicate = self.orders.create(
            self.customer.customer_id,
            key,
            {
                "store_code": store,
                "payment_status": payment_status,
                "fulfillment_status": "not_submitted",
                "items": [{
                    "variant_id": "lens-1",
                    "prescription_assistance_requested": True,
                    "shopify_line_item_id": "11",
                    "quantity": 1,
                }],
                "total_paise": 25000,
                "invoice_no": "ELU/HSR/FY26/00001",
                "status_message": "Order request saved; payment and store confirmation are pending.",
            },
            {"idempotency_key": key},
        )
        self.assertFalse(duplicate)
        return order

    def test_pending_queue_is_scoped_to_store(self):
        hsr = self.create_order("hsr-request")
        self.create_order("kmg-request", store="KMG")

        pending = self.orders.pending_for_store("HSR")

        self.assertEqual([hsr["order_id"]], [order["order_id"] for order in pending])
        self.assertEqual("pending", pending[0]["store_confirmation_status"])

    def test_confirm_marks_paid_order_sold_without_claiming_fulfillment(self):
        order = self.create_order("confirm-request")

        confirmed = self.orders.decide_for_store(
            order["order_id"], "HSR", "staff-cashier-1", "confirm"
        )

        self.assertEqual("confirmed", confirmed["store_confirmation_status"])
        self.assertEqual("staff-cashier-1", confirmed["store_decision_by"])
        self.assertTrue(confirmed["store_decision_at"])
        self.assertEqual("paid", confirmed["payment_status"])
        self.assertEqual("sold", confirmed["status"])
        self.assertEqual("not_submitted", confirmed["fulfillment_status"])
        self.assertIn("Optometrist review", confirmed["status_message"])
        self.assertEqual([], self.orders.pending_for_store("HSR"))

    def test_paid_order_denial_requires_verified_assisted_lens_refund(self):
        order = self.create_order("deny-request")

        with self.assertRaisesRegex(CustomerOrderConflict, "reason is required"):
            self.orders.decide_for_store(order["order_id"], "HSR", "staff-cashier-1", "deny")

        with self.assertRaisesRegex(CustomerOrderConflict, "verified before denying"):
            self.orders.decide_for_store(
                order["order_id"], "HSR", "staff-cashier-1", "deny", "Unable to fulfill"
            )

        denied = self.orders.decide_for_store(
            order["order_id"], "HSR", "staff-cashier-1", "deny", "Unable to fulfill",
            {
                "refund_id": "refund-1",
                "status": "COMPLETED",
                "amount_paise": 5000,
                "credit_note": "CN-1",
            },
        )

        self.assertEqual("denied", denied["store_confirmation_status"])
        self.assertEqual("partially_refunded", denied["status"])
        self.assertEqual("paid", denied["payment_status"])
        self.assertEqual("refund-1", denied["refund_id"])
        self.assertEqual("Unable to fulfill", denied["store_denial_reason"])
        self.assertIn("remaining items stay sold", denied["status_message"])

    def test_decision_is_atomic_store_scoped_and_single_use(self):
        order = self.create_order("single-decision")

        with self.assertRaisesRegex(CustomerOrderConflict, "different store"):
            self.orders.decide_for_store(order["order_id"], "KMG", "staff-cashier-1", "confirm")
        self.orders.decide_for_store(order["order_id"], "HSR", "staff-cashier-1", "confirm")
        with self.assertRaisesRegex(CustomerOrderConflict, "already been recorded"):
            self.orders.decide_for_store(order["order_id"], "HSR", "staff-cashier-2", "confirm")

        connection = sqlite3.connect(self.database)
        try:
            events = connection.execute(
                "SELECT action, actor_staff_id FROM customer_order_events WHERE order_id = ?",
                (order["order_id"],),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([("STORE_CONFIRMED", "staff-cashier-1")], events)

    def test_captured_payment_cannot_be_denied_without_supported_refund(self):
        order = self.create_order("paid-request", payment_status="paid")

        with self.assertRaisesRegex(CustomerOrderConflict, "verified before denying"):
            self.orders.decide_for_store(
                order["order_id"], "HSR", "staff-cashier-1", "deny", "Unavailable"
            )

        self.assertEqual("pending", self.orders.pending_for_store("HSR")[0]["store_confirmation_status"])

    def test_existing_unpaid_order_schema_is_migrated_without_losing_the_order(self):
        database = Path(self.temporary.name) / "legacy.sqlite3"
        accounts = CustomerAccountStore(database)
        customer = accounts.create_account(
            "7402",
            "+919876543211",
            "Legacy Customer",
            "SecureCustomer@2026",
        )
        legacy_order = {
            "order_id": "web_legacy",
            "status": "awaiting_payment",
            "store_code": "HSR",
            "payment_status": "awaiting_payment",
            "total_paise": 25000,
        }
        connection = sqlite3.connect(database)
        try:
            connection.execute("""CREATE TABLE customer_online_orders (
                order_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL REFERENCES customer_accounts(customer_id),
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_payment','cancelled')),
                created_at TEXT NOT NULL,
                order_json TEXT NOT NULL,
                UNIQUE(customer_id, idempotency_key)
            )""")
            connection.execute(
                """INSERT INTO customer_online_orders
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "web_legacy", customer.customer_id, "legacy-key", "hash",
                    "awaiting_payment", "2026-10-29T12:00:00+00:00",
                    json.dumps(legacy_order),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        migrated = CustomerOrderStore(database)

        self.assertEqual([], migrated.pending_for_store("HSR"))
        legacy = migrated.get_for_store("web_legacy", "HSR")
        self.assertEqual("pending", legacy["store_confirmation_status"])
        self.assertIsNone(legacy["store_decision_by"])


if __name__ == "__main__":
    unittest.main()

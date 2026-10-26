from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace

from eluno_pos.adapters.http import HttpRequestError
from eluno_pos.cart import CartService
from eluno_pos.checkout import CheckoutError, CheckoutRequest, CheckoutService, IdempotencyConflict, SaleStore
from eluno_pos.models import Cart, CartLine, CatalogVariant, LensLimits, LensOption, Prescription, PrescriptionDataError
from eluno_pos.offers import OfferBook, OfferDefinition, OfferEngine
from eluno_pos.tax import TaxRate, TaxRateTable
from eluno_pos.operations import OperationsStore, OperationsError


FRAME = CatalogVariant("1", "p1", "frame", "90031900", 100000, True)
LENS = CatalogVariant("2", "p2", "lens", "90015000", 50000, True, "single_vision", Decimal("1.60"), "standard")
RX1 = Prescription.from_data({
    "rx_id": "RX1", "customer_id": "42", "phone": "+919000000042",
    "date": "2026-01-01", "valid_until": "2027-01-01",
    "od": {"sph": "-1.00", "cyl": "-0.50", "axis": 90, "add": None},
    "os": {"sph": "-1.00", "cyl": "-0.50", "axis": 90, "add": None},
})
OPTION_2 = LensOption(
    "2", "single_vision", Decimal("1.60"), "standard",
    LensLimits(Decimal("-6.00"), Decimal("6.00"), Decimal("2.00"), False),
)


class LensOptions:
    def list_lens_options(self):
        return (OPTION_2,)


class PrescriptionLookup:
    def lookup(self, phone, business_date):
        if phone != RX1.phone or not (RX1.prescribed_on <= business_date <= RX1.valid_until):
            return SimpleNamespace(code="no_valid_prescription", customer=None, prescription=None)
        return SimpleNamespace(
            code="ready",
            customer=SimpleNamespace(customer_id=RX1.customer_id),
            prescription=RX1,
        )


class Catalog:
    def list_catalog_variants(self):
        return (FRAME, LENS)


class Locations:
    def list_locations(self):
        return [{"id": 7, "active": True, "eluno": {"store_code": "HSR", "kind": "store", "invoice_prefix": "ELU/HSR/FY26/"}}]


class Inventory:
    def __init__(self, available=5, item_id=101, location_id=7):
        self.available = available
        self.item_id = item_id
        self.location_id = location_id
        self.variant_item_id = 101
        self.variant_calls = []
        self.level_calls = []

    def get_variant(self, variant_id):
        self.variant_calls.append(variant_id)
        return {"id": variant_id, "inventory_item_id": self.variant_item_id}

    def list_inventory_levels(self, inventory_item_ids, location_id):
        self.level_calls.append((inventory_item_ids, location_id))
        if self.item_id is None:
            return []
        return [{
            "inventory_item_id": self.item_id,
            "location_id": self.location_id,
            "available": self.available,
        }]


class Terminal:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [{"charge_id": "ch_1", "status": "captured"}])
        self.calls = []

    def charge(self, amount_paise, method, order_ref):
        self.calls.append((amount_paise, method, order_ref))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Orders:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [{"id": 101}])
        self.calls = []

    def create_order(self, order):
        self.calls.append(order)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Customers:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def search_customers_by_phone(self, phone):
        self.calls.append(phone)
        return [row for row in self.rows if row.get("phone") == phone]


class Tills:
    def __init__(self, status="OPEN", store="HSR", location="7"):
        self.till = {"session_id": "till_1", "status": status, "store_code": store, "location_id": location}
        self.refs = set(); self.movements = []
    def till_get(self, session_id):
        if session_id != "till_1": raise ValueError("missing")
        return self.till
    def movement_exists(self, ref): return ref in self.refs
    def movement(self, session_id, kind, amount, operator, reason, ref):
        if self.till["status"] != "OPEN": raise ValueError("closed")
        self.refs.add(ref); self.movements.append((session_id, kind, amount, ref))


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        carts = CartService(
            Catalog(), OfferEngine(OfferBook(())),
            TaxRateTable((TaxRate("90031900", 1800, date(2025, 1, 1)), TaxRate("90015000", 500, date(2025, 1, 1)))),
        )
        self.terminal = Terminal()
        self.orders = Orders()
        self.inventory = Inventory()
        self.store = SaleStore(Path(self.tmp.name) / "sales.sqlite3")
        self.service = self.make_service(carts=carts, sales=self.store)

    def make_service(self, carts=None, sales=None, customers=None, tills=None,
                     prescription_lookup=None, lens_catalog=None):
        return CheckoutService(
            carts or self.service._carts,
            self.terminal,
            self.orders,
            Locations(),
            sales or self.store,
            customers=customers,
            tills=tills,
            prescription_lookup=prescription_lookup or PrescriptionLookup(),
            lens_catalog=lens_catalog or LensOptions(),
            inventory=self.inventory,
        )

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def request(key="key-1", staff_id="S1", customer_id="42"):
        return CheckoutRequest(
            key, Cart("HSR", "7", (CartLine("1", 1, "pair-a"), CartLine("2", 1, "pair-a", "RX1"))),
            date(2026, 10, 20), "upi", "walk_in", staff_id, customer_id,
            customer_phone=RX1.phone,
        )

    def test_lens_checkout_rejects_mismatched_prescription_before_payment(self):
        request = self.request()
        request = CheckoutRequest(
            request.idempotency_key,
            Cart("HSR", "7", (
                CartLine("1", 1, "pair-a"),
                CartLine("2", 1, "pair-a", "RX-OTHER"),
            )),
            request.business_date,
            request.payment_method,
            request.channel,
            request.staff_id,
            request.customer_id,
            customer_phone=request.customer_phone,
        )

        with self.assertRaisesRegex(CheckoutError, "does not match"):
            self.service.checkout(request)

        self.assertEqual([], self.terminal.calls)
        self.assertEqual([], self.orders.calls)

    def test_frame_checkout_accepts_sufficient_inventory(self):
        result = self.service.checkout(self.request())
        self.assertEqual("COMPLETED", result.status)
        self.assertEqual([([101], 7)], self.inventory.level_calls)
        self.assertEqual(1, len(self.terminal.calls))
        self.assertEqual(1, len(self.orders.calls))

    def test_ineligible_manual_offer_is_rejected_before_payment_or_sale_persistence(self):
        request=self.request(key="manual-ineligible-offer")
        request=CheckoutRequest(
            request.idempotency_key,request.cart,request.business_date,
            request.payment_method,request.channel,request.staff_id,
            request.customer_id,customer_phone=request.customer_phone,
            selected_offer_ids=("UNKNOWN-OFFER",),
        )
        with self.assertRaisesRegex(CheckoutError,"not eligible"):
            self.service.checkout(request)
        self.assertEqual([],self.terminal.calls)
        self.assertEqual([],self.orders.calls)
        self.assertIsNone(self.store.get_by_idempotency_key(request.idempotency_key))

    def test_checkout_retry_uses_existing_sale_without_rechecking_changed_stock(self):
        request = self.request(key="stock-idempotency")
        first = self.service.checkout(request)
        self.inventory.available = 0

        retry = self.service.checkout(request)

        self.assertEqual(first.sale_id, retry.sale_id)
        self.assertEqual("COMPLETED", retry.status)
        self.assertEqual(1, len(self.terminal.calls))
        self.assertEqual(1, len(self.orders.calls))

    def test_frame_checkout_rejects_zero_or_insufficient_inventory_before_payment(self):
        for available, quantity in ((0, 1), (1, 2)):
            with self.subTest(available=available, quantity=quantity):
                self.inventory.available = available
                request = self.request(key="stock-{}-{}".format(available, quantity))
                request = CheckoutRequest(
                    request.idempotency_key,
                    Cart("HSR", "7", (CartLine("1", quantity, "pair-a"), CartLine("2", quantity, "pair-a", "RX1"))),
                    request.business_date, request.payment_method, request.channel,
                    request.staff_id, request.customer_id, customer_phone=request.customer_phone,
                )
                before_charges = len(self.terminal.calls)
                before_orders = len(self.orders.calls)
                with self.assertRaisesRegex(CheckoutError, "Insufficient frame stock"):
                    self.service.checkout(request)
                self.assertEqual(before_charges, len(self.terminal.calls))
                self.assertEqual(before_orders, len(self.orders.calls))
                with self.store._session() as connection:
                    self.assertEqual(0, connection.execute(
                        "SELECT COUNT(*) FROM sales WHERE idempotency_key = ?",
                        (request.idempotency_key,),
                    ).fetchone()[0])

    def test_frame_checkout_rejects_missing_and_unknown_inventory_before_payment(self):
        self.inventory.item_id = None
        with self.assertRaisesRegex(CheckoutError, "not recorded"):
            self.service.checkout(self.request(key="missing-level"))
        self.inventory.item_id = 101
        self.inventory.variant_item_id = None
        with self.assertRaisesRegex(CheckoutError, "could not be verified"):
            self.service.checkout(self.request(key="unknown-item"))
        self.assertEqual([], self.terminal.calls)
        self.assertEqual([], self.orders.calls)

    def test_frame_checkout_rejects_wrong_store_location_before_inventory_or_payment(self):
        request = self.request(key="wrong-location")
        request = CheckoutRequest(
            request.idempotency_key,
            Cart("OTHER", "8", request.cart.lines),
            request.business_date, request.payment_method, request.channel,
            request.staff_id, request.customer_id, customer_phone=request.customer_phone,
        )
        with self.assertRaisesRegex(CheckoutError, "store context"):
            self.service.checkout(request)
        self.assertEqual([], self.inventory.level_calls)
        self.assertEqual([], self.terminal.calls)
        self.assertEqual([], self.orders.calls)

    def test_inventory_level_from_another_location_is_treated_as_missing(self):
        self.inventory.location_id = 8
        with self.assertRaisesRegex(CheckoutError, "not recorded"):
            self.service.checkout(self.request(key="wrong-stock-location"))
        self.assertEqual([], self.terminal.calls)
        self.assertEqual([], self.orders.calls)

    def test_lens_only_checkout_does_not_query_frame_inventory(self):
        request = self.request(key="lens-only")
        request = CheckoutRequest(
            request.idempotency_key,
            Cart("HSR", "7", (CartLine("2", 1, "pair-a", "RX1"),)),
            request.business_date, request.payment_method, request.channel,
            request.staff_id, request.customer_id, customer_phone=request.customer_phone,
        )
        result = self.service.checkout(request)
        self.assertEqual("COMPLETED", result.status)
        self.assertEqual([], self.inventory.variant_calls)
        self.assertEqual([], self.inventory.level_calls)

    def test_lens_checkout_rejects_unavailable_or_invalid_rx_before_payment(self):
        missing_lookup = PrescriptionLookup()
        def no_valid_lookup(phone, day):
            self.assertEqual(RX1.phone, phone)
            self.assertEqual(date(2026, 10, 20), day)
            return SimpleNamespace(code="no_valid_prescription", customer=None, prescription=None)

        missing_lookup.lookup = no_valid_lookup
        service = self.make_service(prescription_lookup=missing_lookup)

        with self.assertRaisesRegex(CheckoutError, "current valid prescription"):
            service.checkout(self.request())

        self.assertEqual([], self.terminal.calls)
        self.assertEqual([], self.orders.calls)

    def test_off_grid_axis_in_either_eye_blocks_checkout_before_side_effects(self):
        cases = (
            ("od", 91, "axis 91 is invalid"),
            ("os", 91, "axis 91 is invalid"),
            ("od", None, "Right eye axis is required"),
        )
        for eye_name, axis, message in cases:
            with self.subTest(eye=eye_name, axis=axis):
                rx_data = RX1.as_dict()
                rx_data[eye_name] = dict(rx_data[eye_name], axis=axis)
                invalid_rx = Prescription.from_data(rx_data)
                lookup = PrescriptionLookup()
                def invalid_axis_lookup(phone, day, rx=invalid_rx):
                    if phone != rx.phone or not (rx.prescribed_on <= day <= rx.valid_until):
                        return SimpleNamespace(
                            code="no_valid_prescription", customer=None, prescription=None,
                        )
                    return SimpleNamespace(
                        code="ready",
                        customer=SimpleNamespace(customer_id=rx.customer_id),
                        prescription=rx,
                    )

                lookup.lookup = invalid_axis_lookup
                service = self.make_service(prescription_lookup=lookup)
                request = self.request(key="off-grid-{}".format(eye_name))

                with self.assertRaisesRegex(CheckoutError, message):
                    service.checkout(request)

                self.assertEqual([], self.terminal.calls)
                self.assertEqual([], self.orders.calls)
                self.assertIsNone(self.store.get_by_idempotency_key(request.idempotency_key))

    def test_malformed_prescription_axis_blocks_checkout_before_side_effects(self):
        lookup = PrescriptionLookup()
        def malformed_lookup(phone, day):
            self.assertEqual(RX1.phone, phone)
            self.assertEqual(date(2026, 10, 20), day)
            raise PrescriptionDataError("Right eye axis must be an integer degree value")

        lookup.lookup = malformed_lookup
        service = self.make_service(prescription_lookup=lookup)
        request = self.request(key="malformed-axis")

        with self.assertRaisesRegex(CheckoutError, "no payment was taken"):
            service.checkout(request)

        self.assertEqual([], self.terminal.calls)
        self.assertEqual([], self.orders.calls)
        self.assertIsNone(self.store.get_by_idempotency_key(request.idempotency_key))

    def test_lens_checkout_rejects_unmapped_variant_before_payment(self):
        options = LensOptions()
        options.list_lens_options = lambda: (
            LensOption(
                "different-variant", "single_vision", Decimal("1.60"), "standard",
                OPTION_2.limits,
            ),
        )
        service = self.make_service(lens_catalog=options)

        with self.assertRaisesRegex(CheckoutError, "not present in the validated lens matrix"):
            service.checkout(self.request())

        self.assertEqual([], self.terminal.calls)
        self.assertEqual([], self.orders.calls)

    def test_success_creates_one_payment_and_one_server_priced_order(self):
        result = self.service.checkout(self.request())
        self.assertEqual("COMPLETED", result.status)
        self.assertEqual("101", result.order_id)
        self.assertEqual("ELU/HSR/FY26/00001", result.invoice_no)
        self.assertEqual(1, len(self.terminal.calls))
        self.assertEqual(1, len(self.orders.calls))
        order = self.orders.calls[0]
        self.assertEqual("1500.00", order["transactions"][0]["amount"])
        self.assertEqual("ch_1", order["transactions"][0]["authorization"])
        self.assertEqual("1000.00", order["line_items"][0]["price"])
        self.assertEqual("76.27", order["line_items"][0]["tax_lines"][0]["price"])
        self.assertEqual("RX1", order["line_items"][1]["properties"][-2]["value"])
        self.assertIn({"name": "invoice_no", "value": "ELU/HSR/FY26/00001"}, order["note_attributes"])
        sale = self.store.get(result.sale_id)
        self.assertEqual(101, json.loads(sale["order_snapshot_json"])["id"])
        taxable = json.loads(sale["taxable_lines_json"])
        self.assertEqual([84746, 47619], [line["taxable_paise"] for line in taxable])

    def test_completed_sale_persists_returned_shopify_reference_snapshot(self):
        self.orders.outcomes = [{
            "id": 101, "order_number": 5001, "name": "#5001", "processed_at": "2026-10-20T12:00:00+05:30",
            "location_id": 7, "customer": {"id": 42, "first_name": "Ada", "last_name": "Retail", "email": "private@example.test"},
            "line_items": [{"id": 8, "variant_id": 1, "title": "Frame", "quantity": 1, "price": "1000.00"}],
            "note_attributes": [{"name": "offer_codes", "value": ""}],
            "transactions": [{"kind": "sale", "status": "success", "authorization": "ch_1"}],
            "total_price": "1500.00",
        }]
        result = self.service.checkout(self.request())
        saved = self.store.get(result.sale_id)
        snapshot = json.loads(saved["order_snapshot_json"])
        self.assertEqual("#5001", snapshot["name"])
        self.assertEqual(101, snapshot["id"])
        self.assertEqual(
            {"id": 42, "first_name": "Ada", "last_name": "Retail"},
            {key: snapshot["customer"][key] for key in ("id", "first_name", "last_name")},
        )
        self.assertNotIn("email", snapshot["customer"])

    def test_existing_database_is_migrated_without_losing_sales(self):
        path = Path(self.tmp.name) / "legacy.sqlite3"
        connection = sqlite3.connect(str(path))
        connection.execute("""CREATE TABLE sales (
            sale_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL,
            status TEXT NOT NULL, invoice_no TEXT NOT NULL, invoice_state TEXT NOT NULL,
            order_payload TEXT NOT NULL, payment_method TEXT NOT NULL, amount_paise INTEGER NOT NULL,
            payment_id TEXT, order_id TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        connection.execute(
            "INSERT INTO sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sale_old", "key_old", "hash", "COMPLETED", "INV1", "ISSUED", "{}", "upi",
             100, "charge_old", "42", None, "2026-01-01", "2026-01-01"),
        )
        connection.commit()
        connection.close()
        migrated = SaleStore(path)
        record = migrated.get("sale_old")
        self.assertEqual("charge_old", record["payment_id"])
        self.assertIsNone(record["order_snapshot_json"])
        self.assertEqual("[]", record["taxable_lines_json"])

    def test_decline_creates_no_order_and_is_only_retried_explicitly(self):
        self.terminal.outcomes = [{"status": "declined", "charge_id": "ch_declined"}]
        result = self.service.checkout(self.request())
        self.assertEqual("PAYMENT_DECLINED", result.status)
        self.assertIsNone(result.invoice_no)
        self.assertEqual([], self.orders.calls)

    def test_duplicate_key_does_not_charge_twice_and_payload_change_is_rejected(self):
        first = self.service.checkout(self.request())
        second = self.service.checkout(self.request())
        self.assertEqual(first.sale_id, second.sale_id)
        self.assertEqual(1, len(self.terminal.calls))
        with self.assertRaises(IdempotencyConflict):
            self.service.checkout(self.request(staff_id="S2"))

    def test_concurrent_duplicate_requests_create_one_sale_and_payment(self):
        results, errors = [], []
        def run():
            try:
                results.append(self.service.checkout(self.request("concurrent")))
            except Exception as exc:
                errors.append(exc)
        workers = [threading.Thread(target=run) for _ in range(5)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertEqual([], errors)
        self.assertEqual(1, len(self.terminal.calls))
        self.assertEqual(1, len({result.sale_id for result in results}))

    def test_payment_timeout_is_ambiguous_and_never_blindly_recharged(self):
        self.terminal.outcomes = [HttpRequestError(None, "timeout")]
        first = self.service.checkout(self.request())
        second = self.service.checkout(self.request())
        self.assertEqual("PAYMENT_UNKNOWN", first.status)
        self.assertEqual(first.sale_id, second.sale_id)
        self.assertEqual(1, len(self.terminal.calls))
        self.assertEqual([], self.orders.calls)

    def test_shopify_failure_preserves_payment_and_timeout_is_not_retried(self):
        self.orders.outcomes = [HttpRequestError(None, "response lost")]
        result = self.service.checkout(self.request())
        sale = self.store.get(result.sale_id)
        self.assertEqual("ORDER_UNKNOWN", result.status)
        self.assertEqual("ch_1", sale["payment_id"])
        self.assertEqual(1, len(self.terminal.calls))
        self.assertEqual(1, len(self.orders.calls))
        recovery = self.service.recover(result.sale_id)
        self.assertEqual("ORDER_UNKNOWN", recovery.status)
        self.assertEqual(1, len(self.orders.calls))

    def test_restart_keeps_external_references_and_invoice_numbers_are_not_reused(self):
        first = self.service.checkout(self.request("first"))
        restarted = SaleStore(Path(self.tmp.name) / "sales.sqlite3")
        self.assertEqual("ch_1", restarted.get(first.sale_id)["payment_id"])
        self.terminal.outcomes = [{"charge_id": "ch_2", "status": "captured"}]
        self.orders.outcomes = [{"id": 102}]
        second_service = self.make_service(sales=restarted)
        second = second_service.checkout(self.request("second"))
        self.assertEqual("ELU/HSR/FY26/00002", second.invoice_no)

    def test_concurrent_invoice_allocations_are_unique_and_sequential(self):
        invoices, errors = [], []
        def allocate(key):
            try:
                sale, _ = self.store.create_or_get(
                    "hash-" + key, key, "HSR", "ELU/HSR/FY26/",
                    {"note_attributes":[{"name":"invoice_no","value":"placeholder"}]},
                    "upi", 500,
                )
                invoices.append(sale["invoice_no"])
            except Exception as exc:
                errors.append(exc)
        workers=[threading.Thread(target=allocate,args=("invoice-{}".format(i),)) for i in range(8)]
        for worker in workers:worker.start()
        for worker in workers:worker.join()
        self.assertEqual([],errors)
        self.assertEqual(8,len(set(invoices)))
        self.assertEqual(
            {"ELU/HSR/FY26/{:05d}".format(number) for number in range(1,9)},
            set(invoices),
        )

    def test_invalid_request_never_initiates_payment(self):
        with self.assertRaises(CheckoutError):
            self.service.checkout(self.request(staff_id=""))
        self.assertEqual([], self.terminal.calls)

    def test_all_supported_channels_are_persisted_in_order_and_ledger(self):
        for number, channel in enumerate(("walk_in", "whatsapp_booked", "online_pickup")):
            self.terminal.outcomes = [{"charge_id": "ch_{}".format(number), "status": "captured"}]
            self.orders.outcomes = [{"id": 200 + number}]
            result = self.service.checkout(CheckoutRequest(
                "channel-" + channel, self.request().cart, date(2026, 10, 20), "upi", channel, "S1", customer_id="42", customer_phone=RX1.phone))
            self.assertEqual(channel, self.store.get(result.sale_id)["channel"])
            attributes = {x["name"]: x["value"] for x in self.orders.calls[-1]["note_attributes"]}
            self.assertEqual(channel, attributes["channel"])
        with self.assertRaisesRegex(CheckoutError, "not supported"):
            self.service.checkout(CheckoutRequest("bad-channel", self.request().cart, date(2026,10,20), "upi", "marketplace", "S1", customer_id="42", customer_phone=RX1.phone))

    def test_channel_and_business_date_survive_restart_and_idempotent_retry(self):
        first = self.service.checkout(CheckoutRequest("persist-channel", self.request().cart, date(2026,10,20), "upi", "online_pickup", "S1", customer_id="42", customer_phone=RX1.phone))
        restarted = SaleStore(Path(self.tmp.name) / "sales.sqlite3")
        row = restarted.get(first.sale_id)
        self.assertEqual("online_pickup", row["channel"])
        self.assertEqual("2026-10-20", row["business_date"])
        service = self.make_service(sales=restarted)
        retry = service.checkout(CheckoutRequest("persist-channel", self.request().cart, date(2026,10,20), "upi", "online_pickup", "S1", customer_id="42", customer_phone=RX1.phone))
        self.assertEqual(first.sale_id, retry.sale_id)

    def test_cash_checkout_posts_once_to_matching_open_till(self):
        tills = Tills()
        service = self.make_service(tills=tills)
        request = CheckoutRequest("cash-1", self.request().cart, date(2026, 10, 20), "cash", "walk_in", "S1", customer_id="42", customer_phone=RX1.phone, till_session_id="till_1")
        first = service.checkout(request); second = service.checkout(request)
        self.assertEqual("COMPLETED", first.status); self.assertEqual(first.sale_id, second.sale_id)
        self.assertEqual(1, len(tills.movements)); self.assertEqual("cash_sale", tills.movements[0][1])

    def test_cash_checkout_rejects_wrong_or_closed_till_before_payment(self):
        tills = Tills(status="CLOSED")
        service = self.make_service(tills=tills)
        request = CheckoutRequest("cash-2", self.request().cart, date(2026, 10, 20), "cash", "walk_in", "S1", customer_id="42", customer_phone=RX1.phone, till_session_id="till_1")
        with self.assertRaises(CheckoutError): service.checkout(request)
        self.assertEqual([], self.terminal.calls)

    def test_checkout_audit_events_persist_when_using_durable_till_store(self):
        operations = OperationsStore(Path(self.tmp.name) / "sales.sqlite3")
        till = operations.open_till("HSR", "7", "S1", 0)
        service = self.make_service(tills=operations)
        result = service.checkout(CheckoutRequest("audit-cash", self.request().cart, date(2026,10,20), "cash", "walk_in", "S1", customer_id="42", customer_phone=RX1.phone, till_session_id=till["session_id"]))
        actions=[event["action"] for event in operations.audit_events(result.sale_id)]
        self.assertIn("CHECKOUT_SUBMITTED", actions); self.assertIn("CHECKOUT_COMPLETED", actions)

    def test_audit_failure_prevents_payment_before_external_work(self):
        class FailingAudit(Tills):
            def audit(self, *args, **kwargs): raise OperationsError("audit storage unavailable")
        tills=FailingAudit()
        service=self.make_service(tills=tills)
        request=CheckoutRequest("audit-fail",self.request().cart,date(2026,10,20),"cash","walk_in","S1",customer_id="42",customer_phone=RX1.phone,till_session_id="till_1")
        with self.assertRaises(OperationsError): service.checkout(request)
        self.assertEqual([],self.terminal.calls)

    def test_caller_supplied_tags_cannot_unlock_an_offer(self):
        offer = OfferDefinition(
            "VIP", True, date(2026, 1, 1), date(2026, 12, 31), False, 1,
            {"applies_to": "frame", "condition": {"customer_tag": "priority"},
             "discount": {"type": "flat", "value_paise": 10000}},
        )
        carts = CartService(
            Catalog(), OfferEngine(OfferBook((offer,))),
            TaxRateTable((TaxRate("90031900", 1800, date(2025, 1, 1)), TaxRate("90015000", 500, date(2025, 1, 1)))),
        )
        customers = Customers([{"id": 42, "phone": "+919000000042", "tags": "family"}])
        service = self.make_service(carts=carts, customers=customers)
        request = CheckoutRequest(
            "tag-test", self.request().cart, date(2026, 10, 20), "upi", "walk_in", "S1", "42",
            frozenset(("priority",)), "+919000000042",
        )
        result = service.checkout(request)
        self.assertEqual("COMPLETED", result.status)
        self.assertEqual("0.00", self.orders.calls[0]["line_items"][0]["total_discount"])
        self.assertEqual(["+919000000042"], customers.calls)

    def test_ambiguous_shopify_phone_match_cannot_unlock_customer_tag_offer(self):
        offer = OfferDefinition(
            "VIP", True, date(2026, 1, 1), date(2026, 12, 31), False, 1,
            {"applies_to": "frame", "condition": {"customer_tag": "priority"},
             "discount": {"type": "flat", "value_paise": 10000}},
        )
        carts = CartService(
            Catalog(), OfferEngine(OfferBook((offer,))),
            TaxRateTable((TaxRate("90031900", 1800, date(2025, 1, 1)), TaxRate("90015000", 500, date(2025, 1, 1)))),
        )
        phone = "+919000000042"
        customers = Customers([
            {"id": 42, "phone": phone, "tags": "priority"},
            {"id": 43, "phone": phone, "tags": "campaign:staff"},
        ])
        service = self.make_service(carts=carts, customers=customers)
        result = service.checkout(CheckoutRequest(
            "ambiguous-customer-tag", self.request().cart, date(2026, 10, 20),
            "upi", "walk_in", "S1", "42", frozenset(("priority",)), phone,
        ))
        self.assertEqual("COMPLETED", result.status)
        self.assertEqual("0.00", self.orders.calls[0]["line_items"][0]["total_discount"])
        self.assertEqual(["+919000000042"], customers.calls)

    def test_verified_shopify_tag_can_unlock_an_offer(self):
        offer = OfferDefinition(
            "VIP", True, date(2026, 1, 1), date(2026, 12, 31), False, 1,
            {"applies_to": "frame", "condition": {"customer_tag": "priority"},
             "discount": {"type": "flat", "value_paise": 10000}},
        )
        carts = CartService(
            Catalog(), OfferEngine(OfferBook((offer,))),
            TaxRateTable((TaxRate("90031900", 1800, date(2025, 1, 1)), TaxRate("90015000", 500, date(2025, 1, 1)))),
        )
        customers = Customers([{"id": 42, "phone": "+919000000042", "tags": "priority"}])
        service = self.make_service(carts=carts, customers=customers)
        result = service.checkout(CheckoutRequest(
            "verified-tag", self.request().cart, date(2026, 10, 20), "upi", "walk_in", "S1", "42",
            frozenset(), "+919000000042",
        ))
        self.assertEqual("COMPLETED", result.status)
        self.assertEqual("100.00", self.orders.calls[0]["line_items"][0]["total_discount"])

    def test_declined_payment_is_not_retried_without_a_terminal_retry_contract(self):
        self.terminal.outcomes = [{"status": "declined", "charge_id": "ch_declined"}]
        first = self.service.checkout(self.request())
        with self.assertRaisesRegex(CheckoutError, "cannot safely retry"):
            self.service.retry_declined_payment(first.sale_id)
        self.assertEqual(1, len(self.terminal.calls))
        self.assertEqual("PAYMENT_DECLINED", self.store.get(first.sale_id)["status"])

    def test_operator_status_and_recovery_leave_ambiguous_payment_unresolved(self):
        self.terminal.outcomes = [HttpRequestError(None, "timeout")]
        first = self.service.checkout(self.request())
        status = self.service.sale_status(first.sale_id)
        recovered = self.service.recover(first.sale_id)
        self.assertEqual("PAYMENT_UNKNOWN", status.status)
        self.assertEqual("PAYMENT_UNKNOWN", recovered.status)
        self.assertEqual(1, len(self.terminal.calls))
        with self.assertRaisesRegex(CheckoutError, "not found"):
            self.service.recover("missing-sale")


if __name__ == "__main__":
    unittest.main()

import html as html_lib
import json
import unittest

from eluno_pos.adapters.http import HttpRequestError
from eluno_pos.documents import DocumentError, DocumentService


ORDER = {
    "id": 42,
    "order_number": 1001,
    "name": "#1001",
    "location_id": 7,
    "processed_at": "2026-10-26T10:00:00+05:30",
    "total_price": "118.00",
    "customer": {"id": 8, "first_name": "A<", "last_name": "Customer"},
    "note_attributes": [
        {"name": "invoice_no", "value": "ELU/HSR/FY26/00001"},
        {"name": "store_code", "value": "HSR"},
        {"name": "staff_id", "value": "S105"},
        {"name": "payment_method", "value": "upi"},
        {"name": "channel", "value": "whatsapp_booked"},
        {"name": "offer_codes", "value": "OFFER-10"},
    ],
    "transactions": [
        {"kind": "sale", "status": "success", "amount": "118.00", "authorization": "charge_1"}
    ],
    "line_items": [{
        "id": 10,
        "variant_id": 1,
        "title": "Frame <blue>",
        "variant_title": "Olive",
        "quantity": 1,
        "price": "118.00",
        "total_discount": "0.00",
        "properties": [
            {"name": "line_type", "value": "frame"},
            {"name": "hsn", "value": "90031900"},
        ],
        "tax_lines": [
            {"title": "CGST", "rate": 0.09, "price": "9.00"},
            {"title": "SGST", "rate": 0.09, "price": "9.00"},
        ],
    }],
}


class Sales:
    def __init__(self, status="COMPLETED", overrides=None):
        self.status = status
        self.get_calls = 0
        self.record = {
            "sale_id": "sale_1", "status": self.status, "invoice_state": "ISSUED",
            "invoice_no": "ELU/HSR/FY26/00001", "order_id": "42", "amount_paise": 11800,
            "payment_method": "upi", "customer_phone": "+919000000005", "operator": "S105",
            "business_date": "2026-10-26", "channel": "whatsapp_booked", "updated_at": "2026-10-26",
        }
        self.record.update(overrides or {})

    def get(self, _):
        self.get_calls += 1
        self.record["status"] = self.status
        return dict(self.record)


class Refunds:
    def __init__(self, status="COMPLETED"):
        self.status = status

    def refund_get(self, _):
        return {
            "refund_id": "refund_1", "status": self.status, "credit_note": "ELU/HSR/FY26/CN/00001",
            "order_id": "42", "store_code": "HSR", "amount_paise": 11800, "tax_paise": 1800,
            "reason": "Changed <mind>", "shopify_refund_id": "99", "created_at": "2026-10-26",
            "lines_json": '[{"line_item_id":10,"quantity":1}]',
        }


class Shopify:
    def __init__(self, order=None):
        self.order = order or ORDER
        self.get_order_calls = 0
        self.list_locations_calls = 0
        self.list_products_calls = 0

    def get_order(self, _):
        self.get_order_calls += 1
        return self.order

    def list_locations(self):
        self.list_locations_calls += 1
        return [{"id": 7, "name": "Eluno HSR Layout", "address1": "HSR Layout, Bengaluru"}]

    def list_products(self):
        self.list_products_calls += 1
        return [{
            "id": 20, "title": "Frame <blue>", "variants": [{"id": 1, "title": "Olive"}],
        }]

    def search_customers_by_phone(self, phone):
        if phone == "+919000000005":
            return [{"id": 8, "first_name": "A<", "last_name": "Customer", "phone": phone}]
        return []


class DocumentTests(unittest.TestCase):
    def test_invoice_shows_rx_id_without_repeating_on_file_label(self):
        order = dict(ORDER)
        order["line_items"] = [dict(ORDER["line_items"][0])]
        order["line_items"][0]["properties"] = [
            {"name": "rx_id", "value": "RX99000009"},
        ]

        class Prescriptions:
            def prescriptions_by_customer(self, customer_id):
                return [{
                    "customer_id": customer_id,
                    "phone": "+919000000005",
                    "rx_id": "RX99000009",
                    "date": "2026-10-25",
                    "valid_until": "2027-10-25",
                    "od": {"sph": "-1.00", "cyl": "-0.50"},
                    "os": {"sph": "-1.00", "cyl": "-0.50"},
                }]

        html = DocumentService(Sales(), Refunds(), Shopify(order), Prescriptions()).invoice_html("sale_1")
        text = html[html.index("<pre class='invoice'>") + len("<pre class='invoice'>"):html.index("</pre>")]
        self.assertIn("Rx on file : RX99000009", text)
        self.assertNotIn("Rx on file : On file:", text)

    def test_invoice_uses_reference_structure_and_print_friendly_monospaced_layout(self):
        html = DocumentService(Sales(), Refunds(), Shopify()).invoice_html("sale_1")
        text = html[html.index("<pre class='invoice'>") + len("<pre class='invoice'>"):html.index("</pre>")]
        sections = (
            "ELUNO", "TAX INVOICE", "Invoice no :", "Store      :", "Customer   :",
            "Rx on file :", "Description", "TOTAL", "Offers applied :",
            "Payment        :", "Amount in words:", "Shopify ref    :", "Prices are inclusive of GST.",
            "Lenses are made to order",
        )
        offsets = [text.index(section) for section in sections]
        self.assertEqual(sorted(offsets), offsets)
        self.assertIn("font-family:Consolas,'Courier New',monospace", html)
        self.assertIn("@page{size:A4 landscape;margin:10mm}", html)
        self.assertIn("white-space:pre", html)
        self.assertIn("onclick='window.print()'>Print</button>", html)
        self.assertIn(".print-control{display:none}", html)
        self.assertIn("ELU/HSR/FY26/00001", text)
        self.assertIn("Eluno HSR Layout · HSR Layout, Bengaluru", text)
        self.assertIn("Frame &lt;blue&gt; Olive", text)
        self.assertIn("90031900", text)
        self.assertIn("9% 9.00", text)
        self.assertIn("OFFER-10", text)
        self.assertIn("UPI ₹118.00  ref charge_1", text)
        self.assertIn("One Hundred Eighteen Rupees", text)
        self.assertIn("#1001 (order 42)", text)
        self.assertNotIn("Karthik Reddy", text)
        self.assertNotIn("Frame <blue>", text)

    def test_checkout_snapshot_reprints_without_order_lookup_and_labels_walk_in(self):
        snapshot = dict(ORDER)
        snapshot["customer"] = None
        snapshot["note_attributes"] = [
            item for item in ORDER["note_attributes"] if item["name"] != "offer_codes"
        ]
        snapshot["line_items"] = [dict(ORDER["line_items"][0])]
        snapshot["line_items"][0]["discount_allocations"] = []
        snapshot["line_items"][0].pop("title")
        snapshot["line_items"][0].pop("variant_title")
        snapshot["invoice_location"] = {
            "id": 7, "name": "Eluno HSR Layout", "address1": "HSR Layout, Bengaluru",
        }
        sale = Sales(overrides={
            "customer_phone": None, "order_snapshot_json": json.dumps(snapshot),
            "taxable_lines_json": json.dumps([{
                "variant_id": 1, "quantity": 1, "taxable_paise": 10000,
                "description": "Stored frame description", "hsn": "90031900",
            }]),
        })
        class SnapshotOnlyShopify(Shopify):
            def list_locations(self):
                raise AssertionError("persisted location snapshot should be used")

            def list_products(self):
                raise AssertionError("persisted line description should be used")

        shopify = SnapshotOnlyShopify()
        html = DocumentService(sale, Refunds(), shopify).invoice_html("sale_1")
        text = html[html.index("<pre class='invoice'>") + len("<pre class='invoice'>"):html.index("</pre>")]
        self.assertEqual(0, shopify.get_order_calls)
        self.assertIn("Customer   : Walk-in", text)
        self.assertIn("Rx on file : Not on file", text)
        self.assertIn("Offers applied : None", text)
        self.assertIn("Stored frame description", text)
        self.assertIn("100.00", text)

    def test_missing_historical_order_uses_only_saved_checkout_data(self):
        class MissingOrderShopify(Shopify):
            def get_order(self, _):
                raise HttpRequestError(404, "order no longer available")

        payload = {
            "id": 42, "location_id": 7, "line_items": ORDER["line_items"],
            "note_attributes": ORDER["note_attributes"], "transactions": ORDER["transactions"],
            "discount_applications": [],
        }
        html = DocumentService(
            Sales(overrides={"order_payload": json.dumps(payload)}), Refunds(), MissingOrderShopify(),
        ).invoice_html("sale_1")
        text = html[html.index("<pre class='invoice'>") + len("<pre class='invoice'>"):html.index("</pre>")]
        self.assertIn("Shopify ref    : N/A (order 42)", text)
        self.assertIn("UPI ₹118.00  ref charge_1", text)

    def test_historical_customer_lookup_requires_the_saved_customer_id_to_match(self):
        order = dict(ORDER)
        order["customer"] = {"id": 8}
        html = DocumentService(Sales(), Refunds(), Shopify(order)).invoice_html("sale_1")
        self.assertIn("Customer   : A&lt; Customer", html)

        order["customer"] = {"id": 9}
        html = DocumentService(Sales(), Refunds(), Shopify(order)).invoice_html("sale_1")
        self.assertIn("Customer   : Customer 9", html)
        self.assertNotIn("Customer   : A&lt; Customer", html)

    def test_table_aligns_columns_and_wraps_description_without_clipping_amounts(self):
        html = DocumentService(Sales(), Refunds(), Shopify()).invoice_html("sale_1")
        text = html[html.index("<pre class='invoice'>") + len("<pre class='invoice'>"):html.index("</pre>")]
        header_index = text.index("Description")
        header_start = text.rfind("\n", 0, header_index) + 1
        table_start = header_start
        table = html_lib.unescape(text[table_start:text.index("Offers applied")])
        lines = table.splitlines()
        self.assertEqual(len(set(map(len, lines))), 1)
        self.assertGreaterEqual(lines[1].count("-"), len("#  Description"))
        self.assertIn("Taxable", lines[0])
        self.assertIn("CGST", lines[0])
        self.assertIn("SGST", lines[0])
        self.assertTrue(all(len(line) == len(lines[0]) for line in lines))

    def test_invoice_shows_transaction_tax_discount_hsn_and_rates_for_multiple_lines(self):
        order = dict(ORDER)
        order["total_price"] = "233.00"
        order["line_items"] = [
            dict(ORDER["line_items"][0]),
            {
                "id": 11, "variant_id": 2, "title": "Lens", "variant_title": "Progressive",
                "quantity": 2, "price": "60.00", "total_discount": "5.00",
                "properties": [
                    {"name": "line_type", "value": "lens"}, {"name": "hsn", "value": "90015000"},
                    {"name": "lens_config", "value": "progressive/1.6/blue_cut"},
                    {"name": "rx_id", "value": "RX9"},
                ],
                "tax_lines": [
                    {"title": "CGST", "rate": 0.025, "price": "5.75"},
                    {"title": "SGST", "rate": 0.025, "price": "5.75"},
                ],
            },
        ]
        order["transactions"] = [{
            "kind": "sale", "status": "success", "amount": "233.00", "authorization": "txn_real_2",
        }]
        products_shopify = Shopify(order)
        products_shopify.list_products = lambda: [
            {"id": 20, "title": "Frame", "variants": [{"id": 1, "title": "Olive"}]},
            {"id": 21, "title": "Lens", "variants": [{"id": 2, "title": "Progressive"}]},
        ]
        html = DocumentService(Sales(overrides={"amount_paise": 23300}), Refunds(), products_shopify).invoice_html("sale_1")
        text = html[html.index("<pre class='invoice'>") + len("<pre class='invoice'>"):html.index("</pre>")]
        self.assertIn("Progressive 1.6 blue cut lens, Rx RX9", text)
        self.assertIn("ELU/HSR/FY26/00001", text)
        self.assertIn("90015000", text)
        self.assertIn("2.5% 5.75", text)
        self.assertIn("Discount", text)
        self.assertIn("5.00", text)
        self.assertIn("233.00", text)
        self.assertIn("UPI ₹233.00", text)
        self.assertIn("ref txn_real_2", text)
        self.assertIn("RX9", text)

    def test_missing_optional_invoice_fields_render_as_na(self):
        order = {
            "id": 42, "location_id": 99, "total_price": "118.00",
            "note_attributes": [], "line_items": ORDER["line_items"],
        }
        html = DocumentService(
            Sales(overrides={"customer_phone": None, "operator": None, "channel": None}),
            Refunds(), Shopify(dict(order, customer=None)),
        ).invoice_html("sale_1")
        self.assertIn("Customer   : Walk-in", html)
        self.assertIn("Phone       : N/A", html)
        self.assertIn("Rx on file : Not on file", html)
        self.assertIn("Offers applied : None", html)
        self.assertIn("ref N/A", html)
        self.assertIn("Store      : N/A", html)

    def test_reprinting_reads_existing_sale_without_creating_financial_records(self):
        sales = Sales()
        shopify = Shopify()
        service = DocumentService(sales, Refunds(), shopify)
        first = service.invoice_html("sale_1")
        second = service.invoice_html("sale_1")
        self.assertEqual(first, second)
        self.assertEqual(2, sales.get_calls)
        self.assertEqual(2, shopify.get_order_calls)
        self.assertEqual(2, shopify.list_locations_calls)
        self.assertEqual(0, shopify.list_products_calls)

    def test_unconfirmed_sale_is_not_printable(self):
        with self.assertRaises(DocumentError):
            DocumentService(Sales("ORDER_UNKNOWN"), Refunds(), Shopify()).invoice_html("sale_1")

    def test_completed_credit_note_is_line_limited_and_unresolved_is_rejected(self):
        html = DocumentService(Sales(), Refunds(), Shopify()).credit_note_html("refund_1")
        self.assertIn("CN/00001", html)
        self.assertIn("Original invoice: ELU/HSR/FY26/00001", html)
        self.assertIn("Changed &lt;mind&gt;", html)
        with self.assertRaises(DocumentError):
            DocumentService(Sales(), Refunds("REFUND_UNKNOWN"), Shopify()).credit_note_html("refund_1")


if __name__ == "__main__":
    unittest.main()

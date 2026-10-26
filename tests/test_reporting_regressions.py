from datetime import date
import unittest

from eluno_pos.reporting import DayEndReportService


class ReportShopify:
    def __init__(self, orders):
        self.orders = orders

    def list_locations(self):
        return [{"id": 7, "active": True, "eluno": {"store_code": "HSR"}}]

    def list_orders(self, **query):
        return self.orders


class ReportOperations:
    def tills_for(self, store_id, day):
        return []

    def cash_variance_alerts(self, store_id, day):
        return []

    def refunds_for(self, store_code):
        return []


def order(order_id, status, payment_method, refund=None, cancelled_at=None):
    return {
        "id": order_id,
        "location_id": 7,
        "processed_at": "2026-10-25T20:00:00+00:00",
        "cancelled_at": cancelled_at,
        "financial_status": status,
        "total_line_items_price": "100.00",
        "total_discounts": "10.00",
        "total_price": "90.00",
        "note_attributes": [
            {"name": "channel", "value": "walk_in"},
            {"name": "payment_method", "value": payment_method},
        ],
        "transactions": [
            {"kind": "sale", "status": "success", "gateway": "razorpay_terminal", "amount": "90.00"}
        ],
        "line_items": [
            {
                "price": "100.00",
                "quantity": 1,
                "total_discount": "10.00",
                "tax_lines": [
                    {"title": "CGST", "price": "7.63"},
                    {"title": "SGST", "price": "7.63"},
                ],
            }
        ],
        "refunds": [refund] if refund else [],
    }


class DayEndReportRegressionTests(unittest.TestCase):
    def test_paid_order_with_missing_capture_is_flagged_not_counted(self):
        unpaid = order(1, "paid", "upi")
        unpaid["transactions"] = []
        service = DayEndReportService(ReportShopify([unpaid]), ReportOperations())

        report = service.report("7", date(2026, 10, 26))

        self.assertEqual(0, report["completed_bill_count"])
        self.assertTrue(
            any(
                item["type"] == "payment_reconciliation" and item["order_id"] == 1
                for item in report["exceptions"]
            )
        )

    def test_refunded_sales_timezone_and_recorded_payment_method_are_preserved(self):
        refund = {
            "id": 51,
            "created_at": "2026-10-25T20:30:00+00:00",
            "transactions": [{"kind": "refund", "amount": "90.00"}],
            "refund_line_items": [{"total_tax": "15.26"}],
        }
        orders = [
            order(1, "paid", "upi"),
            order(2, "partially_refunded", "card", refund),
            order(3, "refunded", "upi", refund),
            order(4, "paid", "card", cancelled_at="2026-10-26T00:00:00+05:30"),
        ]
        service = DayEndReportService(ReportShopify(orders), ReportOperations())

        report = service.report("7", date(2026, 10, 26))

        self.assertEqual(3, report["completed_bill_count"])
        self.assertEqual(27000, report["payment_methods_paise"]["upi"] + report["payment_methods_paise"]["card"])
        self.assertEqual(18000, report["payment_methods_paise"]["upi"])
        self.assertEqual(9000, report["payment_methods_paise"]["card"])
        self.assertEqual(18000, report["metrics"]["refund_amount_paise"])
        self.assertEqual(30000, report["metrics"]["gross_sales_paise"])
        self.assertEqual(2, report["refund_count"])


if __name__ == "__main__":
    unittest.main()

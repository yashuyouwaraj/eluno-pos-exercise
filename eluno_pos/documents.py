"""Printable documents assembled from confirmed POS and Shopify records."""
from decimal import Decimal
from html import escape
import json
import logging
import textwrap

from .adapters.http import HttpRequestError
from .money import paise_from_decimal


class DocumentError(ValueError):
    pass


INVOICE_WIDTH = 124
TABLE_DESCRIPTION_WIDTH = 35
RETURN_POLICY = (
    "Lenses are made to order against the prescription on file and are not returnable once dispensed; "
    "frames may be returned within 7 days."
)
LOG = logging.getLogger(__name__)


def _e(value):
    return escape("" if value is None else str(value))


def _money(paise):
    amount = Decimal(int(paise)) / 100
    return "{:,.2f}".format(amount)


def _attrs(order):
    return {
        str(item.get("name")): item.get("value")
        for item in order.get("note_attributes", [])
        if isinstance(item, dict)
    }


def _amount_in_words(paise):
    value = abs(int(paise))
    rupees, fraction = divmod(value, 100)
    ones = (
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
        "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
        "seventeen", "eighteen", "nineteen",
    )
    tens = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")

    def under_thousand(number):
        words = []
        hundreds, remainder = divmod(number, 100)
        if hundreds:
            words.extend((ones[hundreds], "hundred"))
        if remainder >= 20:
            words.append(tens[remainder // 10])
            if remainder % 10:
                words.append(ones[remainder % 10])
        elif remainder:
            words.append(ones[remainder])
        return " ".join(words)

    groups = []
    for divisor, label in ((10000000, "crore"), (100000, "lakh"), (1000, "thousand")):
        count, rupees = divmod(rupees, divisor)
        if count:
            groups.extend((under_thousand(count), label))
    if rupees:
        groups.append(under_thousand(rupees))
    if not groups:
        groups.append("zero")
    result = " ".join(groups) + " rupees"
    if fraction:
        result += " and {} paise".format(under_thousand(fraction))
    return ("minus " if int(paise) < 0 else "") + result


def _percent(rate):
    value = Decimal(str(rate)) * 100
    return "{}%".format(format(value.normalize(), "f"))


def _format_table(rows, totals):
    headers = ("#", "Description", "HSN", "Qty", "MRP(incl)", "Discount", "Taxable", "CGST", "SGST", "Total")
    column_widths = [2, TABLE_DESCRIPTION_WIDTH, 8, 3, 10, 10, 10, 12, 12, 10]
    values = []
    for item in rows:
        values.append((
            str(item["index"]),
            item["description"],
            item["hsn"],
            str(item["quantity"]),
            _money(item["mrp_paise"]),
            _money(item["discount_paise"]),
            _money(item["taxable_paise"]),
            "{} {}".format(_percent(item["cgst_rate"]), _money(item["cgst_paise"])),
            "{} {}".format(_percent(item["sgst_rate"]), _money(item["sgst_paise"])),
            _money(item["total_paise"]),
        ))
    values.append((
        "TOTAL", "", "", "",
        _money(totals["mrp_paise"]),
        _money(totals["discount_paise"]),
        _money(totals["taxable_paise"]),
        _money(totals["cgst_paise"]),
        _money(totals["sgst_paise"]),
        _money(totals["total_paise"]),
    ))
    for row in (headers,) + tuple(values):
        for index, value in enumerate(row):
            column_widths[index] = max(column_widths[index], len(value))
    width = sum(column_widths) + (2 * (len(column_widths) - 1))
    separator = "-" * width

    def format_row(row, alignments):
        cells = []
        for value, column_width, alignment in zip(row, column_widths, alignments):
            cells.append(value.rjust(column_width) if alignment == "right" else value.ljust(column_width))
        return "  ".join(cells)

    lines = [separator, format_row(headers, ("left", "left", "left", "right", "right", "right", "right", "right", "right", "right")), separator]
    for item in rows:
        description_lines = textwrap.wrap(
            item["description"], width=column_widths[1], break_long_words=True, break_on_hyphens=False
        ) or [""]
        for part_index, description in enumerate(description_lines):
            row = (
                str(item["index"]) if part_index == 0 else "",
                description,
                item["hsn"] if part_index == 0 else "",
                str(item["quantity"]) if part_index == 0 else "",
                _money(item["mrp_paise"]) if part_index == 0 else "",
                _money(item["discount_paise"]) if part_index == 0 else "",
                _money(item["taxable_paise"]) if part_index == 0 else "",
                "{} {}".format(_percent(item["cgst_rate"]), _money(item["cgst_paise"])) if part_index == 0 else "",
                "{} {}".format(_percent(item["sgst_rate"]), _money(item["sgst_paise"])) if part_index == 0 else "",
                _money(item["total_paise"]) if part_index == 0 else "",
            )
            lines.append(format_row(row, ("right", "left", "left", "right", "right", "right", "right", "right", "right", "right")))
    lines.extend((separator, format_row(values[-1], ("left", "left", "left", "right", "right", "right", "right", "right", "right", "right")), separator))
    return "\n".join(lines), width


class DocumentService:
    def __init__(self, sales, refunds, shopify):
        self.sales, self.refunds, self.shopify = sales, refunds, shopify

    def invoice_html(self, sale_id):
        sale = self.sales.get(sale_id)
        if sale["status"] != "COMPLETED" or sale["invoice_state"] != "ISSUED" or not sale.get("order_id"):
            raise DocumentError("a printable invoice is available only for a completed, issued sale")
        snapshot = sale.get("order_snapshot_json")
        if snapshot:
            try:
                order = json.loads(snapshot)
            except (TypeError, ValueError) as exc:
                raise DocumentError("the persisted Shopify invoice snapshot is invalid") from exc
            if not isinstance(order, dict):
                raise DocumentError("the persisted Shopify invoice snapshot is invalid")
        else:
            try:
                order = self.shopify.get_order(int(sale["order_id"]))
            except HttpRequestError as exc:
                if exc.status != 404:
                    raise
                LOG.warning("Shopify order %s is unavailable; rendering from persisted sale data", sale["order_id"])
                try:
                    order = json.loads(sale["order_payload"])
                except (TypeError, ValueError) as payload_error:
                    raise DocumentError("the Shopify order and saved transaction details are unavailable") from payload_error
                if not isinstance(order, dict):
                    raise DocumentError("the saved transaction details are invalid")
                order = dict(order)
                order["id"] = sale["order_id"]
        return self._sale_document(sale, order)

    def credit_note_html(self, refund_id):
        refund = self.refunds.refund_get(refund_id)
        if refund["status"] != "COMPLETED":
            raise DocumentError("a printable credit note is available only for a completed refund")
        order = self.shopify.get_order(int(refund["order_id"]))
        selected = {str(item["line_item_id"]): int(item["quantity"]) for item in json.loads(refund["lines_json"])}
        lines = [item for item in order.get("line_items", []) if str(item.get("id")) in selected]
        body = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                _e(item.get("title") or item.get("name") or item.get("sku") or item.get("id")),
                selected[str(item["id"])],
                _money(self._line_amount(item, selected[str(item["id"])])),
            )
            for item in lines
        )
        attrs = _attrs(order)
        return self._page("Credit note " + _e(refund["credit_note"]), """
<h1>Credit note {credit}</h1><p>Status: Completed · Date: {date}</p>
<p>Original invoice: {invoice} · Shopify order: {order}</p><p>Store: {store}</p>
<table><tr><th>Refunded item</th><th>Qty</th><th>Amount</th></tr>{body}</table>
<p>Tax reversal: {tax}<br><strong>Refund total: {amount}</strong></p>
<p>Reason: {reason}<br>Refund reference: {reference}</p>""".format(
            credit=_e(refund["credit_note"]), date=_e(refund["created_at"]),
            invoice=_e(attrs.get("invoice_no", "N/A")), order=_e(order.get("name") or order.get("id")),
            store=_e(refund["store_code"]), body=body, tax=_money(refund["tax_paise"]),
            amount=_money(refund["amount_paise"]), reason=_e(refund["reason"]),
            reference=_e(refund.get("shopify_refund_id") or refund["refund_id"]),
        ), preformatted=False)

    def _sale_document(self, sale, order):
        attrs = _attrs(order)
        line_items = order.get("line_items", [])
        try:
            taxable_lines = json.loads(sale.get("taxable_lines_json") or "[]")
        except (TypeError, ValueError):
            taxable_lines = []
        if not isinstance(taxable_lines, list):
            taxable_lines = []

        location = order.get("invoice_location")
        if not isinstance(location, dict) or not (location.get("name") or location.get("address1") or location.get("city")):
            location = next(
                (item for item in self.shopify.list_locations() if str(item.get("id")) == str(order.get("location_id"))),
                {},
            )
        needs_catalog = any(
            not (
                index <= len(taxable_lines)
                and isinstance(taxable_lines[index - 1], dict)
                and taxable_lines[index - 1].get("description")
            )
            and not (line.get("title") or line.get("name"))
            for index, line in enumerate(line_items, 1)
        )
        products = self.shopify.list_products() if needs_catalog else []
        variants = {}
        for product in products:
            for variant in product.get("variants", []):
                variants[str(variant.get("id"))] = (product, variant)

        customer = order.get("customer") or {}
        persisted_payload = {}
        try:
            persisted_payload = json.loads(sale.get("order_payload") or "{}")
        except (TypeError, ValueError):
            LOG.warning("sale %s has an invalid stored checkout payload", sale.get("sale_id"))
        persisted_customer = persisted_payload.get("customer") or {}
        customer_id = customer.get("id") or persisted_customer.get("id")
        customer_name = " ".join(
            value for value in (customer.get("first_name"), customer.get("last_name")) if value
        ).strip()
        customer_phone = sale.get("customer_phone") or customer.get("phone") or "N/A"
        if not customer_name and customer_id is not None and customer_phone != "N/A":
            try:
                matches = self.shopify.search_customers_by_phone(customer_phone)
            except (HttpRequestError, ValueError) as exc:
                LOG.warning("customer details for sale %s could not be resolved: %s", sale.get("sale_id"), exc)
                matches = []
            matches = [item for item in matches if str(item.get("id")) == str(customer_id)]
            if len(matches) == 1:
                customer_name = " ".join(
                    value for value in (matches[0].get("first_name"), matches[0].get("last_name")) if value
                ).strip()
                customer_phone = matches[0].get("phone") or customer_phone
        if not customer_name:
            customer_name = "Customer {}".format(customer_id) if customer_id is not None else "Walk-in"

        rows = []
        totals = {
            "mrp_paise": 0, "discount_paise": 0, "taxable_paise": 0,
            "cgst_paise": 0, "sgst_paise": 0, "total_paise": 0,
        }
        prescription_ids = []
        for index, line in enumerate(line_items, 1):
            quantity = int(line.get("quantity", 1))
            price_paise = paise_from_decimal(line.get("price", "0.00"))
            mrp_paise = price_paise * quantity
            discount_paise = paise_from_decimal(line.get("total_discount", "0.00"))
            cgst_lines = [tax for tax in line.get("tax_lines", []) if tax.get("title") == "CGST"]
            sgst_lines = [tax for tax in line.get("tax_lines", []) if tax.get("title") == "SGST"]
            cgst_paise = sum(paise_from_decimal(tax.get("price", "0.00")) for tax in cgst_lines)
            sgst_paise = sum(paise_from_decimal(tax.get("price", "0.00")) for tax in sgst_lines)
            tax_paise = cgst_paise + sgst_paise
            taxable_paise = mrp_paise - discount_paise - tax_paise
            saved_line = taxable_lines[index - 1] if index <= len(taxable_lines) else None
            if index <= len(taxable_lines):
                if (
                    isinstance(saved_line, dict)
                    and str(saved_line.get("variant_id")) == str(line.get("variant_id"))
                    and str(saved_line.get("quantity")) == str(quantity)
                ):
                    if saved_line.get("taxable_paise") is not None:
                        taxable_paise = int(saved_line["taxable_paise"])
            line_total = self._line_amount(line, quantity)
            properties = {
                str(item.get("name")): item.get("value")
                for item in line.get("properties", [])
                if isinstance(item, dict)
            }
            variant_data = variants.get(str(line.get("variant_id")))
            product_title = line.get("title") or line.get("name") or (
                variant_data[0].get("title") if variant_data else None
            )
            variant_title = line.get("variant_title") or (variant_data[1].get("title") if variant_data else None)
            description = " ".join(value for value in (product_title, variant_title) if value)
            if properties.get("lens_config"):
                lens_type, index_value, coating = (properties["lens_config"].split("/") + ["", "", ""])[:3]
                description = "{} {} {} lens".format(lens_type.title(), index_value, coating.replace("_", " "))
                if properties.get("rx_id"):
                    description += ", Rx {}".format(properties["rx_id"])
            if isinstance(saved_line, dict) and saved_line.get("description"):
                description = saved_line["description"]
            if not description:
                description = line.get("title") or line.get("name") or line.get("sku") or "N/A"
            hsn = properties.get("hsn") or (
                saved_line.get("hsn") if isinstance(saved_line, dict) else None
            ) or "N/A"
            cgst_rate = cgst_lines[0].get("rate", 0) if cgst_lines else 0
            sgst_rate = sgst_lines[0].get("rate", 0) if sgst_lines else 0
            rows.append({
                "index": index, "description": str(description), "hsn": str(hsn), "quantity": quantity,
                "mrp_paise": mrp_paise, "discount_paise": discount_paise,
                "taxable_paise": taxable_paise, "cgst_paise": cgst_paise,
                "sgst_paise": sgst_paise, "total_paise": line_total,
                "cgst_rate": cgst_rate, "sgst_rate": sgst_rate,
            })
            for key, amount in (
                ("mrp_paise", mrp_paise), ("discount_paise", discount_paise),
                ("taxable_paise", taxable_paise), ("cgst_paise", cgst_paise),
                ("sgst_paise", sgst_paise), ("total_paise", line_total),
            ):
                totals[key] += amount
            rx_id = properties.get("rx_id")
            if rx_id and rx_id not in prescription_ids:
                prescription_ids.append(rx_id)

        table, table_width = _format_table(rows, totals)
        width = max(INVOICE_WIDTH, table_width)
        border = "=" * width
        divider = "-" * width
        store_code = attrs.get("store_code") or "N/A"
        store_name = location.get("name") or store_code
        address = location.get("address1") or location.get("city") or "N/A"
        heading = "{} · {}".format(store_name, address)
        date_value = order.get("processed_at") or sale.get("business_date") or order.get("created_at") or "N/A"
        invoice_date = str(date_value)[:10]
        invoice_no = sale.get("invoice_no") or attrs.get("invoice_no") or "N/A"
        staff_id = attrs.get("staff_id") or sale.get("operator") or "N/A"
        channel = attrs.get("channel") or sale.get("channel") or "N/A"
        has_lens = any(
            item.get("name") == "line_type" and item.get("value") == "lens"
            for line in order.get("line_items", [])
            for item in line.get("properties", [])
            if isinstance(item, dict)
        )
        rx_value = ", ".join(prescription_ids) if prescription_ids else (
            "Unavailable (not recorded)" if has_lens else "None"
        )
        metadata = (
            "Invoice no : {:<31} Date        : {}".format(invoice_no, invoice_date),
            "Store      : {:<31} Staff       : {}".format(store_code, staff_id),
            "Customer   : {:<31} Phone       : {}".format(customer_name or "N/A", customer_phone),
            "Rx on file : {:<31} Channel     : {}".format(rx_value, channel),
        )
        offer_codes = attrs.get("offer_codes")
        if offer_codes:
            offer_codes = str(offer_codes)
        else:
            discount_applications = order.get("discount_applications") or []
            applied_indexes = set()
            for line in order.get("line_items", []):
                for allocation in line.get("discount_allocations", []):
                    try:
                        if paise_from_decimal(allocation.get("amount", "0.00")) > 0:
                            applied_indexes.add(int(allocation["discount_application_index"]))
                    except (KeyError, TypeError, ValueError):
                        continue
            recovered = [
                str(application.get("title"))
                for index, application in enumerate(discount_applications)
                if index in applied_indexes and application.get("title")
                and application.get("title") != "POS_OFFERS"
            ]
            if recovered:
                offer_codes = ", ".join(recovered)
            else:
                any_discount = any(
                    paise_from_decimal(line.get("total_discount", "0.00")) > 0
                    for line in order.get("line_items", [])
                )
                offer_codes = "Unavailable (not recorded)" if any_discount else "None"
        transactions = order.get("transactions") or []
        sale_transaction = next(
            (item for item in transactions if item.get("kind") == "sale" and item.get("status") == "success"),
            {},
        )
        payment_ref = sale.get("payment_id") or sale_transaction.get("authorization") or "N/A"
        payment_method = (attrs.get("payment_method") or sale.get("payment_method") or "N/A").upper()
        order_id = order.get("id") or sale.get("order_id") or "N/A"
        payable_paise = int(sale["amount_paise"])
        if totals["total_paise"] != payable_paise:
            raise DocumentError(
                "saved invoice line totals do not reconcile to the completed sale amount; "
                "sale {}, lines {}".format(_money(payable_paise), _money(totals["total_paise"]))
            )
        payment_amount = payable_paise
        order_reference = order.get("name") or (
            "#{}".format(order["order_number"]) if order.get("order_number") is not None else "N/A"
        )
        lines = [
            border,
            "ELUNO".center(width),
            heading.center(width),
            "TAX INVOICE".center(width),
            border,
            *metadata,
            divider,
            table,
            border,
            "Offers applied : {}".format(offer_codes),
            "Payment        : {} ₹{}  ref {}".format(payment_method, _money(payment_amount), payment_ref),
            "Amount in words: {}".format(_amount_in_words(totals["total_paise"]).title()),
            "Shopify ref    : {} (order {})".format(order_reference, order_id),
            divider,
            "Prices are inclusive of GST. Intra-state supply: CGST + SGST. Taxable value = inclusive price less tax, per line.",
            RETURN_POLICY,
            border,
        ]
        return self._page("Invoice " + _e(invoice_no), "\n".join(lines), width)

    @staticmethod
    def _line_amount(line, quantity):
        line_quantity = int(line.get("quantity", 1))
        return (
            paise_from_decimal(line.get("price", "0.00")) * quantity
            - paise_from_decimal(line.get("total_discount", "0.00")) * quantity // line_quantity
        )

    @staticmethod
    def _page(title, body, width=INVOICE_WIDTH, preformatted=True):
        font_size = "7pt" if width > 116 else "8pt"
        content = "<pre class='invoice'>{}</pre>".format(_e(body)) if preformatted else body
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>{}</title>"
            "<style>html,body{{background:#fff;color:#000}}body{{font-family:Arial,sans-serif;margin:24px auto;max-width:100%;}}"
            ".invoice{{font-family:Consolas,'Courier New',monospace;font-size:{};line-height:1.35;"
            "white-space:pre;overflow:visible;margin:0 auto;width:max-content;max-width:none}}"
            "table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #777;padding:6px;text-align:left}}"
            "@page{{size:A4 landscape;margin:10mm}}@media print{{html,body{{margin:0;padding:0}}"
            ".invoice{{font-size:6.5pt;line-height:1.3;width:max-content;page-break-inside:avoid}}"
            "tr{{break-inside:avoid;page-break-inside:avoid}}}}</style></head><body>{}</body></html>"
        ).format(title, font_size, content)

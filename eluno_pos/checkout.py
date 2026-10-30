"""Durable, deliberately conservative checkout orchestration."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, FrozenSet, List, Optional, Protocol, Sequence, Tuple
from uuid import uuid4

from .adapters.http import HttpRequestError
from .cart import CartService
from .models import Cart, LensConfiguration, PrescriptionDataError
from .money import decimal_string_from_paise
from .offers import OfferDefinition
from .services import CustomerPrescriptionService, LensCatalogSource, LensValidator


LOG = logging.getLogger(__name__)
SHOPIFY_ORDER_PAGE_SIZE = 250
SHOPIFY_ORDER_PAGE_LIMIT = 1000


class CheckoutError(ValueError):
    pass


class CheckoutDependencyError(CheckoutError):
    pass


class IdempotencyConflict(CheckoutError):
    pass


class TerminalSource(Protocol):
    def charge(self, amount_paise: int, method: str, order_ref: str) -> Dict[str, Any]:
        ...


class OrderSource(Protocol):
    def create_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def list_orders(self, **query: str) -> List[Dict[str, Any]]:
        ...


class LocationSource(Protocol):
    def list_locations(self) -> List[Dict[str, Any]]:
        ...


class CustomerSource(Protocol):
    def search_customers_by_phone(self, phone: str) -> List[Dict[str, Any]]:
        ...


class InventorySource(Protocol):
    def get_variant(self, variant_id: int) -> Dict[str, Any]:
        ...

    def list_inventory_levels(
        self, inventory_item_ids: List[int], location_id: int
    ) -> List[Dict[str, Any]]:
        ...


@dataclass(frozen=True)
class CheckoutRequest:
    idempotency_key: str
    cart: Cart
    business_date: date
    payment_method: str
    channel: str
    staff_id: str
    customer_id: Optional[str] = None
    # Retained only for backwards-compatible request parsing. Checkout never
    # trusts these caller-provided tags for offer eligibility.
    customer_tags: FrozenSet[str] = frozenset()
    customer_phone: Optional[str] = None
    till_session_id: Optional[str] = None
    selected_offer_ids: Optional[Tuple[str, ...]] = None
    reviewed_prescription_ids: Tuple[str, ...] = ()
    allow_prescription_assistance: bool = False

    def fingerprint(self) -> str:
        data = {
            "cart": {
                "store_code": self.cart.store_code, "location_id": self.cart.location_id,
                "lines": [line.__dict__ for line in self.cart.lines],
            },
            "business_date": self.business_date.isoformat(), "payment_method": self.payment_method,
            "channel": self.channel, "staff_id": self.staff_id, "customer_id": self.customer_id,
            "customer_phone": self.customer_phone,
            "till_session_id": self.till_session_id,
            "selected_offer_ids": self.selected_offer_ids,
            "reviewed_prescription_ids": self.reviewed_prescription_ids,
            "allow_prescription_assistance": self.allow_prescription_assistance,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CheckoutResult:
    sale_id: str
    status: str
    order_id: Optional[str]
    invoice_no: Optional[str]
    message: str


class SaleStore:
    """SQLite ledger: one row per client idempotency key, safe across restarts."""

    def __init__(self, path: Path) -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialise(self) -> None:
        with self._lock, self._session() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS sales (
                sale_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL,
                status TEXT NOT NULL, invoice_no TEXT NOT NULL, invoice_state TEXT NOT NULL,
                order_payload TEXT NOT NULL, payment_method TEXT NOT NULL, amount_paise INTEGER NOT NULL,
                payment_id TEXT, order_id TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            for column in (
                "till_session_id TEXT", "cash_post_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED'",
                "business_date TEXT", "channel TEXT", "customer_phone TEXT",
                "order_snapshot_json TEXT", "taxable_lines_json TEXT NOT NULL DEFAULT '[]'",
            ):
                try:
                    connection.execute("ALTER TABLE sales ADD COLUMN " + column)
                except sqlite3.OperationalError:
                    pass
            connection.execute("""CREATE TABLE IF NOT EXISTS invoice_series (
                store_code TEXT PRIMARY KEY, next_number INTEGER NOT NULL)""")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS sales_invoice_no_unique ON sales(invoice_no)"
            )
            connection.execute("""CREATE TABLE IF NOT EXISTS audit_events (
                event_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
                reason TEXT, metadata_json TEXT NOT NULL)""")
            try:
                connection.execute("ALTER TABLE sales ADD COLUMN operator TEXT")
            except sqlite3.OperationalError:
                pass

    def create_or_get(
        self, request_hash: str, idempotency_key: str, store_code: str, invoice_prefix: str,
        order_payload: Dict[str, Any], payment_method: str, amount_paise: int, till_session_id: Optional[str] = None,
        business_date: Optional[date] = None, channel: Optional[str] = None, operator: Optional[str] = None,
        customer_phone: Optional[str] = None, taxable_lines_json: str = "[]",
        existing_invoice_numbers: Optional[Sequence[str]] = None,
    ) -> Tuple[Dict[str, Any], bool]:
        if not idempotency_key or len(idempotency_key) > 200:
            raise CheckoutError("idempotency_key is required and must be at most 200 characters")
        with self._lock, self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM sales WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if row:
                connection.execute("COMMIT")
                sale = dict(row)
                if sale["request_hash"] != request_hash:
                    raise IdempotencyConflict("idempotency key was already used for a different checkout")
                return sale, False
            series = connection.execute("SELECT next_number FROM invoice_series WHERE store_code = ?", (store_code,)).fetchone()
            number = int(series["next_number"]) if series else 1
            known_numbers = set(existing_invoice_numbers or ())
            known_numbers.update(
                row["invoice_no"] for row in connection.execute("SELECT invoice_no FROM sales").fetchall()
            )
            while "{}{:05d}".format(invoice_prefix, number) in known_numbers:
                number += 1
            if series:
                connection.execute("UPDATE invoice_series SET next_number = ? WHERE store_code = ?", (number + 1, store_code))
            else:
                connection.execute("INSERT INTO invoice_series(store_code, next_number) VALUES (?, ?)", (store_code, number + 1))
            now = datetime.now(timezone.utc).isoformat()
            sale_id = "sale_" + uuid4().hex
            invoice_no = "{}{:05d}".format(invoice_prefix, number)
            payload = json.loads(json.dumps(order_payload))
            for attribute in payload.get("note_attributes", []):
                if attribute.get("name") == "invoice_no":
                    attribute["value"] = invoice_no
            connection.execute(
                """INSERT INTO sales (sale_id,idempotency_key,request_hash,status,invoice_no,invoice_state,order_payload,payment_method,amount_paise,payment_id,order_id,error,created_at,updated_at,business_date,channel,operator,customer_phone,taxable_lines_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?)""",
                (sale_id, idempotency_key, request_hash, "PENDING_PAYMENT", invoice_no, "PENDING",
                 json.dumps(payload, separators=(",", ":")), payment_method, amount_paise, now, now,
                 business_date.isoformat() if business_date else None, channel, operator, customer_phone,
                 taxable_lines_json),
            )
            if till_session_id:
                connection.execute("UPDATE sales SET till_session_id=?, cash_post_status='PENDING' WHERE sale_id=?", (till_session_id, sale_id))
            self._audit_in_session(connection, operator or "unattributed", "CHECKOUT_SUBMITTED", sale_id,
                                   {"payment_method": payment_method, "amount_paise": amount_paise})
            connection.execute("COMMIT")
            return self.get(sale_id), True

    def get(self, sale_id: str) -> Dict[str, Any]:
        with self._lock, self._session() as connection:
            row = connection.execute("SELECT * FROM sales WHERE sale_id = ?", (sale_id,)).fetchone()
        if not row:
            raise CheckoutError("sale {} was not found".format(sale_id))
        return dict(row)

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._session() as connection:
            row = connection.execute(
                "SELECT * FROM sales WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return None if row is None else dict(row)

    def get_by_invoice_no(self, invoice_no: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._session() as connection:
            rows = connection.execute(
                "SELECT * FROM sales WHERE invoice_no = ? LIMIT 2", (invoice_no,)
            ).fetchall()
        if len(rows) > 1:
            raise CheckoutError("invoice number is ambiguous in the POS sale ledger")
        return None if not rows else dict(rows[0])

    def sale_for_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._session() as connection:
            row = connection.execute(
                "SELECT * FROM sales WHERE order_id = ? AND status = 'COMPLETED' LIMIT 2",
                (str(order_id),),
            ).fetchall()
        if len(row) > 1:
            raise CheckoutError("Shopify order is linked to multiple local sales")
        return None if not row else dict(row[0])
    def unresolved(self):
        with self._lock, self._session() as connection:
            return [dict(r) for r in connection.execute("SELECT * FROM sales WHERE status IN ('PAYMENT_UNKNOWN','ORDER_UNKNOWN','ORDER_FAILED')").fetchall()]

    def sales_for(self, store_code: str, business_date: date) -> List[Dict[str, Any]]:
        """Return local ledger records for one declared billing date and store.

        Older rows without the migration fields are intentionally excluded: their
        date/channel cannot be inferred safely from report-time input.
        """
        with self._lock, self._session() as connection:
            rows = connection.execute(
                "SELECT * FROM sales WHERE business_date = ?", (business_date.isoformat(),)
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            try:
                attrs = {a.get("name"): a.get("value") for a in json.loads(value["order_payload"]).get("note_attributes", [])}
            except (TypeError, ValueError):
                continue
            if attrs.get("store_code") == store_code:
                result.append(value)
        return result

    @staticmethod
    def _audit_in_session(connection, actor: str, action: str, sale_id: str, metadata: Dict[str, Any]) -> None:
        connection.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?)", (
            uuid4().hex, datetime.now(timezone.utc).isoformat(), str(actor), action, "sale", sale_id,
            None, json.dumps(metadata, separators=(",", ":")),
        ))

    def update(self, sale_id: str, status: str, audit_action: Optional[str] = None,
               audit_actor: Optional[str] = None, audit_metadata: Optional[Dict[str, Any]] = None,
               **values: Optional[str]) -> Dict[str, Any]:
        allowed = {
            "payment_id", "order_id", "error", "invoice_state", "order_payload", "cash_post_status",
            "order_snapshot_json",
        }
        if set(values) - allowed:
            raise ValueError("unsupported sale fields")
        columns = ["status = ?", "updated_at = ?"]
        arguments: List[Any] = [status, datetime.now(timezone.utc).isoformat()]
        for key, value in values.items():
            columns.append("{} = ?".format(key))
            arguments.append(value)
        arguments.append(sale_id)
        with self._lock, self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE sales SET {} WHERE sale_id = ?".format(", ".join(columns)), arguments)
            if audit_action:
                self._audit_in_session(connection, audit_actor or "unattributed", audit_action, sale_id, audit_metadata or {})
            connection.execute("COMMIT")
        return self.get(sale_id)


class CheckoutService:
    def __init__(self, carts: CartService, terminal: TerminalSource, orders: OrderSource,
                 locations: LocationSource, sales: SaleStore, customers: Optional[CustomerSource] = None,
                 tills: Any = None, prescription_lookup: Optional[CustomerPrescriptionService] = None,
                 lens_catalog: Optional[LensCatalogSource] = None,
                 inventory: Optional[InventorySource] = None) -> None:
        self._carts, self._terminal, self._orders, self._locations, self._sales = carts, terminal, orders, locations, sales
        self._customers = customers
        self._tills = tills
        self._prescription_lookup = prescription_lookup
        self._lens_catalog = lens_catalog
        self._inventory = inventory
        self._lens_validator = LensValidator()
        self._checkout_lock = threading.Lock()

    def _validate_frame_inventory(self, request: CheckoutRequest, pricing: Any) -> None:
        frame_lines = [
            line for line in pricing.lines
            if line.catalog_variant.line_type == "frame"
        ]
        if not frame_lines:
            return
        if self._inventory is None:
            raise CheckoutDependencyError(
                "Frame inventory could not be verified; no payment was taken."
            )
        try:
            by_item: Dict[str, int] = {}
            for line in frame_lines:
                variant = self._inventory.get_variant(int(line.catalog_variant.variant_id))
                if variant.get("inventory_item_id") is None:
                    raise CheckoutError(
                        "Frame inventory could not be verified for variant {}; ask a manager to check stock.".format(
                            line.catalog_variant.variant_id
                        )
                    )
                item_id = str(variant["inventory_item_id"])
                try:
                    numeric_item_id = int(item_id)
                except (TypeError, ValueError) as exc:
                    raise CheckoutError(
                        "Frame inventory data is invalid for variant {}; no payment was taken.".format(
                            line.catalog_variant.variant_id
                        )
                    ) from exc
                by_item[str(numeric_item_id)] = by_item.get(str(numeric_item_id), 0) + int(
                    line.cart_line.quantity
                )
            location_id = int(request.cart.location_id)
            levels = self._inventory.list_inventory_levels(
                [int(item_id) for item_id in by_item], location_id
            )
        except HttpRequestError as exc:
            raise CheckoutDependencyError(
                "Frame inventory is unavailable; no payment was taken. Check the store connection and try again."
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, CheckoutError):
                raise
            raise CheckoutDependencyError(
                "Frame inventory data could not be verified; no payment was taken."
            ) from exc

        available_by_item: Dict[str, int] = {}
        for level in levels:
            try:
                item_id = str(int(level["inventory_item_id"]))
                level_location = str(int(level["location_id"]))
                available = level["available"]
                if isinstance(available, bool) or not isinstance(available, int) or available < 0:
                    raise ValueError("invalid available quantity")
            except (KeyError, TypeError, ValueError) as exc:
                raise CheckoutDependencyError(
                    "Frame inventory response was incomplete or invalid; no payment was taken."
                ) from exc
            if level_location != str(location_id):
                continue
            if item_id in available_by_item:
                raise CheckoutDependencyError(
                    "Frame inventory response contained duplicate stock rows; no payment was taken."
                )
            available_by_item[item_id] = available

        for item_id, quantity in by_item.items():
            if item_id not in available_by_item:
                raise CheckoutError(
                    "Frame inventory is not recorded for this item at {} (location {}); no payment was taken.".format(
                        request.cart.store_code, location_id
                    )
                )
            if available_by_item[item_id] < quantity:
                raise CheckoutError(
                    "Insufficient frame stock at {} (location {}): {} available, {} requested; no payment was taken.".format(
                        request.cart.store_code, location_id, available_by_item[item_id], quantity
                    )
                )

    def _validate_lens_prescriptions(self, request: CheckoutRequest, pricing: Any) -> None:
        lens_lines = [
            line for line in pricing.lines
            if line.catalog_variant.line_type == "lens"
        ]
        assistance_lines = [
            line for line in lens_lines
            if line.cart_line.prescription_assistance_requested
        ]
        if assistance_lines and (
            not request.allow_prescription_assistance
            or request.channel != "online_pickup"
            or any(line.cart_line.prescription_id for line in assistance_lines)
        ):
            raise CheckoutError("prescription assistance is only allowed for explicitly requested online lens items")
        lens_lines = [
            line for line in lens_lines
            if not line.cart_line.prescription_assistance_requested
        ]
        if not lens_lines:
            return
        if self._prescription_lookup is None or self._lens_catalog is None:
            raise CheckoutError("lens checkout requires configured prescription and lens validation services")
        if not request.customer_id or not request.customer_phone:
            raise CheckoutError("a lens sale requires a verified customer and valid prescription")
        try:
            lookup = self._prescription_lookup.lookup(request.customer_phone, request.business_date)
            options = self._lens_catalog.list_lens_options()
        except PrescriptionDataError as exc:
            raise CheckoutError(
                "The prescription is invalid: {}; no payment was taken.".format(exc)
            ) from exc
        except (HttpRequestError, ValueError) as exc:
            raise CheckoutDependencyError(
                "The customer prescription or lens catalog could not be verified; no payment was taken."
            ) from exc
        if (
            lookup.code != "ready" or lookup.customer is None or lookup.prescription is None
            or str(lookup.customer.customer_id) != str(request.customer_id)
        ):
            raise CheckoutError("a current valid prescription for this customer is required for lens items")

        prescription = lookup.prescription
        if any(
            eye.axis is not None and eye.axis % 5
            for eye in (prescription.od, prescription.os)
        ) and prescription.rx_id not in request.reviewed_prescription_ids:
            raise CheckoutError(
                "prescription axis requires optometrist review acknowledgement; no payment was taken."
            )
        options_by_variant = {str(option.variant_id): option for option in options}
        cart_lines = {str(line.variant_id): line for line in request.cart.lines}
        for priced_line in lens_lines:
            line = cart_lines.get(str(priced_line.cart_line.variant_id))
            option = options_by_variant.get(str(priced_line.catalog_variant.variant_id))
            if line is None or not line.prescription_id:
                raise CheckoutError("each lens item must reference a valid prescription")
            if line.prescription_id != prescription.rx_id:
                raise CheckoutError("lens prescription does not match the latest valid customer prescription")
            if option is None:
                raise CheckoutError("the selected lens is not present in the validated lens matrix")
            validation = self._lens_validator.validate(
                LensConfiguration(option.lens_type, option.index, option.coating),
                prescription,
                (option,),
            )
            if not validation.is_valid:
                raise CheckoutError("; ".join(validation.errors))

    def checkout(self, request: CheckoutRequest) -> CheckoutResult:
        with self._checkout_lock:
            return self._checkout_locked(request)

    def _checkout_locked(self, request: CheckoutRequest) -> CheckoutResult:
        self._validate_request(request)
        existing = self._sales.get_by_idempotency_key(request.idempotency_key)
        if existing is not None:
            if existing["request_hash"] != request.fingerprint():
                raise IdempotencyConflict("idempotency key was already used for a different checkout")
            return self._result(existing, "Existing sale returned; no additional payment was initiated.")
        if request.payment_method == "cash":
            self._validate_till(request)
        customer_tags, customer_snapshot = self._authoritative_customer(request)
        try:
            pricing = self._carts.price(
                request.cart, request.business_date, customer_tags, request.selected_offer_ids,
                allow_prescription_assistance=request.allow_prescription_assistance,
            )
        except ValueError as exc:
            raise CheckoutError(str(exc)) from exc
        except HttpRequestError as exc:
            raise CheckoutDependencyError("catalog or offer data could not be verified: {}".format(exc)) from exc
        self._validate_lens_prescriptions(request, pricing)
        prefix, location_snapshot = self._invoice_prefix(request.cart)
        existing_invoice_numbers = self._existing_shopify_invoices()
        self._validate_frame_inventory(request, pricing)
        payload = self._order_payload(request, pricing)
        sale, created = self._sales.create_or_get(
            request.fingerprint(), request.idempotency_key, request.cart.store_code, prefix,
            payload, request.payment_method, pricing.payable_paise, request.till_session_id,
            request.business_date, request.channel,
            request.staff_id, request.customer_phone,
            json.dumps([
                {"variant_id": line.catalog_variant.variant_id, "quantity": line.cart_line.quantity,
                 "taxable_paise": line.taxable_paise,
                 "description": self._invoice_line_description(line),
                 "hsn": line.catalog_variant.hsn}
                for line in pricing.lines
            ], separators=(",", ":")),
            existing_invoice_numbers,
        )
        if not created:
            return self._result(sale, "Existing sale returned; no additional payment was initiated.")
        self._audit(request.staff_id, "CHECKOUT_SUBMITTED", sale["sale_id"], {"payment_method":request.payment_method,"amount_paise":pricing.payable_paise})
        LOG.info("checkout sale=%s status=PENDING_PAYMENT", sale["sale_id"])
        return self._charge_then_create(sale, customer_snapshot, location_snapshot)

    def selectable_offers(self, business_date: date) -> Tuple[OfferDefinition, ...]:
        return self._carts.selectable_offers(business_date)

    def _existing_shopify_invoices(self) -> Tuple[str, ...]:
        invoice_numbers = set()
        cursor = 0
        for _ in range(SHOPIFY_ORDER_PAGE_LIMIT):
            try:
                orders = self._orders.list_orders(
                    status="any",
                    limit=str(SHOPIFY_ORDER_PAGE_SIZE),
                    since_id=str(cursor),
                    order="created_at asc",
                )
            except HttpRequestError as exc:
                raise CheckoutDependencyError(
                    "Existing invoice numbers could not be checked; no payment was taken."
                ) from exc
            if (
                not isinstance(orders, list)
                or len(orders) > SHOPIFY_ORDER_PAGE_SIZE
                or any(not isinstance(order, dict) for order in orders)
            ):
                raise CheckoutDependencyError(
                    "Shopify invoice history is incomplete or invalid; reconcile the invoice sequence before checkout."
                )
            if not orders:
                return tuple(invoice_numbers)

            page_cursor = cursor
            for order in orders:
                order_id = order.get("id")
                if isinstance(order_id, bool) or not (
                    isinstance(order_id, int)
                    or isinstance(order_id, str) and order_id.isdigit()
                ):
                    raise CheckoutDependencyError(
                        "Shopify returned an invalid order ID while checking invoice history; no payment was taken."
                    )
                numeric_order_id = int(order_id)
                if numeric_order_id <= page_cursor:
                    raise CheckoutDependencyError(
                        "Shopify order pagination did not advance; invoice history cannot be verified."
                    )
                page_cursor = numeric_order_id
                attributes = order.get("note_attributes", [])
                if not isinstance(attributes, list) or any(not isinstance(item, dict) for item in attributes):
                    raise CheckoutDependencyError(
                        "Shopify returned invalid invoice details; no payment was taken."
                    )
                matching = [
                    attribute.get("value")
                    for attribute in attributes
                    if attribute.get("name") == "invoice_no"
                ]
                if len(matching) > 1:
                    raise CheckoutDependencyError(
                        "Shopify contains an ambiguous invoice record; no payment was taken."
                    )
                if matching:
                    value = matching[0]
                    if not isinstance(value, str) or not value:
                        raise CheckoutDependencyError(
                            "Shopify contains an invalid invoice identifier; no payment was taken."
                        )
                    invoice_numbers.add(value)

            if len(orders) < SHOPIFY_ORDER_PAGE_SIZE:
                return tuple(invoice_numbers)
            cursor = page_cursor
        raise CheckoutDependencyError(
            "Shopify invoice history exceeds the safe verification limit; reconcile the invoice sequence before checkout."
        )

    @staticmethod
    def _invoice_line_description(line: Any) -> str:
        variant = line.catalog_variant
        if variant.line_type == "lens":
            description = "{} {} {} lens".format(
                (variant.lens_type or "").replace("_", " ").title(),
                variant.index if variant.index is not None else "",
                (variant.coating or "").replace("_", " "),
            ).strip()
            if line.cart_line.prescription_id:
                description += ", Rx {}".format(line.cart_line.prescription_id)
            return description
        return " ".join(
            value for value in (variant.product_title, variant.variant_title) if value
        )

    def recover(self, sale_id: str, actor: str = "unattributed") -> CheckoutResult:
        sale = self._sales.get(sale_id)
        if sale["status"] == "COMPLETED" and sale.get("cash_post_status") == "PENDING":
            sale = self._post_cash_sale(sale)
            sale = self._sales.update(sale_id, sale["status"], audit_action="CHECKOUT_RECOVERY",
                                      audit_actor=actor, audit_metadata={"cash_post_status":sale.get("cash_post_status")})
            self._audit(actor,"CHECKOUT_RECOVERY",sale_id,{"cash_post_status":sale.get("cash_post_status")})
            return self._result(sale, "Completed sale cash posting was retried locally; no payment was initiated.")
        # Every in-flight external request is ambiguous after a process crash:
        # neither contract offers an operation-ID lookup. Preserve it for staff.
        if sale["status"] == "PENDING_PAYMENT":
            sale = self._sales.update(sale_id, "PAYMENT_UNKNOWN", error="restart during payment request", audit_action="CHECKOUT_RECOVERY", audit_actor=actor, audit_metadata={"status":"PAYMENT_UNKNOWN"})
        elif sale["status"] in ("PAYMENT_APPROVED", "ORDER_PENDING"):
            sale = self._sales.update(sale_id, "ORDER_UNKNOWN", error="restart during Shopify order request", audit_action="CHECKOUT_RECOVERY", audit_actor=actor, audit_metadata={"status":"ORDER_UNKNOWN"})
        elif sale["status"] not in ("PAYMENT_UNKNOWN", "ORDER_UNKNOWN"):
            raise CheckoutError("sale is not awaiting external reconciliation")
        self._audit(actor,"CHECKOUT_RECOVERY",sale_id,{"status":sale["status"]})
        return self._result(sale, "Recovery preserved the sale; reconciliation is required for uncertain operations.")

    def sale_status(self, sale_id: str) -> CheckoutResult:
        """Return locally persisted status without attempting external work."""
        return self._result(self._sales.get(sale_id), "Sale status retrieved.")

    def invoice_status(self, invoice_no: str) -> Optional[CheckoutResult]:
        sale = self._sales.get_by_invoice_no(invoice_no)
        return None if sale is None else self._result(sale, "Invoice status retrieved.")

    def retry_declined_payment(self, sale_id: str) -> CheckoutResult:
        sale = self._sales.get(sale_id)
        if sale["status"] != "PAYMENT_DECLINED":
            raise CheckoutError("only a declined payment may be explicitly retried")
        # The supplied terminal does not actually index charges by order_ref:
        # every POST creates a new charge and it exposes lookup only by charge
        # ID. It also has no documented declined-attempt retry token. A second
        # POST could therefore charge twice, so require a new checkout intent
        # rather than pretending this is a safe retry.
        raise CheckoutError("the terminal contract cannot safely retry a declined payment; start a new checkout intent")

    def _authoritative_customer(self, request: CheckoutRequest) -> Tuple[FrozenSet[str], Optional[Dict[str, Any]]]:
        """Use customer facts only when Shopify uniquely confirms the requested ID and phone.

        The verified mock exposes tags through phone search, not customer-ID
        lookup. Missing/unavailable/ambiguous data therefore grants no
        tag-conditioned offer and does not create an invoice customer snapshot.
        """
        if not request.customer_id or not request.customer_phone or self._customers is None:
            return frozenset(), None
        try:
            matches = self._customers.search_customers_by_phone(request.customer_phone)
        except (HttpRequestError, ValueError):
            LOG.warning("checkout customer tag lookup unavailable; tag offers denied")
            return frozenset(), None
        if len(matches) != 1 or str(matches[0].get("id")) != str(request.customer_id):
            LOG.warning("checkout customer tag lookup did not uniquely match the requested customer; tag offers denied")
            return frozenset(), None
        customer = matches[0]
        raw_tags = matches[0].get("tags")
        tags = (
            frozenset(tag.strip() for tag in raw_tags.split(",") if tag.strip())
            if isinstance(raw_tags, str) else frozenset()
        )
        snapshot = {
            key: customer[key] for key in ("id", "first_name", "last_name", "phone")
            if customer.get(key) is not None
        }
        return tags, snapshot

    def _charge_then_create(
        self, sale: Dict[str, Any], customer_snapshot: Optional[Dict[str, Any]] = None,
        location_snapshot: Optional[Dict[str, Any]] = None,
    ) -> CheckoutResult:
        try:
            charge = self._terminal.charge(int(sale["amount_paise"]), sale["payment_method"], sale["sale_id"])
        except HttpRequestError as exc:
            status = "PAYMENT_UNKNOWN" if exc.status is None else "PAYMENT_FAILED"
            sale = self._sales.update(sale["sale_id"], status, error="terminal request failed", audit_action="CHECKOUT_PAYMENT_RESULT", audit_actor=sale.get("operator") or "system", audit_metadata={"status":status})
            self._audit("system","CHECKOUT_PAYMENT_RESULT",sale["sale_id"],{"status":status}); return self._result(sale, "Payment outcome requires reconciliation." if status.endswith("UNKNOWN") else "Payment failed.")
        payment_status = str(charge.get("status", "")).lower()
        payment_id = str(charge.get("charge_id") or "")
        if payment_status in ("declined", "failed"):
            sale = self._sales.update(sale["sale_id"], "PAYMENT_DECLINED", payment_id=payment_id or None,
                                      error="terminal declined payment", invoice_state="VOID", audit_action="CHECKOUT_PAYMENT_RESULT", audit_actor=sale.get("operator") or "system", audit_metadata={"status":"PAYMENT_DECLINED"})
            self._audit("system","CHECKOUT_PAYMENT_RESULT",sale["sale_id"],{"status":"PAYMENT_DECLINED"}); return self._result(sale, "Payment was declined; no Shopify order was created.")
        if payment_status != "captured" or not payment_id:
            sale = self._sales.update(sale["sale_id"], "PAYMENT_UNKNOWN", error="terminal returned an unrecognised outcome", audit_action="CHECKOUT_PAYMENT_RESULT", audit_actor=sale.get("operator") or "system", audit_metadata={"status":"PAYMENT_UNKNOWN"})
            self._audit("system","CHECKOUT_PAYMENT_RESULT",sale["sale_id"],{"status":"PAYMENT_UNKNOWN"}); return self._result(sale, "Payment outcome requires reconciliation.")
        sale = self._sales.update(sale["sale_id"], "PAYMENT_APPROVED", payment_id=payment_id, error=None)
        payload = json.loads(sale["order_payload"])
        payload["transactions"] = [{
            "kind": "sale", "status": "success", "gateway": "razorpay_terminal",
            "amount": decimal_string_from_paise(int(sale["amount_paise"])), "authorization": payment_id,
        }]
        sale = self._sales.update(
            sale["sale_id"], "ORDER_PENDING", order_payload=json.dumps(payload, separators=(",", ":"))
        )
        try:
            order = self._orders.create_order(json.loads(sale["order_payload"]))
        except HttpRequestError as exc:
            status = "ORDER_UNKNOWN" if exc.status is None or exc.status >= 500 else "ORDER_FAILED"
            sale = self._sales.update(sale["sale_id"], status, error="Shopify order request failed", audit_action="CHECKOUT_ORDER_RESULT", audit_actor=sale.get("operator") or "system", audit_metadata={"status":status})
            self._audit("system","CHECKOUT_ORDER_RESULT",sale["sale_id"],{"status":status}); return self._result(sale, "Payment captured; Shopify order reconciliation is required.")
        order_id = str(order.get("id") or "")
        if not order_id:
            sale = self._sales.update(sale["sale_id"], "ORDER_UNKNOWN", error="Shopify order response lacked an id", audit_action="CHECKOUT_ORDER_RESULT", audit_actor=sale.get("operator") or "system", audit_metadata={"status":"ORDER_UNKNOWN"})
            self._audit("system","CHECKOUT_ORDER_RESULT",sale["sale_id"],{"status":"ORDER_UNKNOWN"}); return self._result(sale, "Payment captured; Shopify order reconciliation is required.")
        sale = self._sales.update(
            sale["sale_id"], "COMPLETED", order_id=order_id, invoice_state="ISSUED", error=None,
            order_snapshot_json=json.dumps(
                self._invoice_order_snapshot(order, customer_snapshot, sale, location_snapshot),
                separators=(",", ":"),
            ),
            audit_action="CHECKOUT_COMPLETED", audit_actor=sale.get("operator") or "system",
            audit_metadata={"order_id":order_id},
        )
        if sale.get("cash_post_status") == "PENDING":
            sale = self._post_cash_sale(sale)
        LOG.info("checkout sale=%s status=COMPLETED order=%s", sale["sale_id"], order_id)
        self._audit("system","CHECKOUT_COMPLETED",sale["sale_id"],{"order_id":order_id})
        return self._result(sale, "Checkout completed.")

    def _audit(self, actor: str, action: str, sale_id: str, metadata: Dict[str, Any]) -> None:
        # Normal application wiring shares one SQLite file with OperationsStore,
        # where SaleStore has already committed the event atomically. Lightweight
        # test or integration audit sinks remain a pre-payment availability gate.
        if self._tills is not None and hasattr(self._tills, "audit") and getattr(self._tills, "path", None) != self._sales._path:
            self._tills.audit(actor,action,"sale",sale_id,metadata=metadata)

    @staticmethod
    def _invoice_order_snapshot(
        order: Dict[str, Any], customer_snapshot: Optional[Dict[str, Any]] = None,
        sale: Optional[Dict[str, Any]] = None, location_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        fields = (
            "id", "order_number", "name", "processed_at", "created_at", "location_id",
            "line_items", "note_attributes", "transactions", "discount_applications", "total_price",
        )
        snapshot = {key: order[key] for key in fields if key in order}
        customer = order.get("customer")
        if isinstance(customer, dict):
            snapshot["customer"] = {
                key: customer[key] for key in ("id", "first_name", "last_name", "phone")
                if key in customer
            }
        submitted_customer_id = (snapshot.get("customer") or {}).get("id")
        if submitted_customer_id is None and sale:
            try:
                submitted_customer_id = (json.loads(sale.get("order_payload") or "{}").get("customer") or {}).get("id")
            except (TypeError, ValueError):
                submitted_customer_id = None
        if customer_snapshot and str(customer_snapshot.get("id")) == str(submitted_customer_id):
            snapshot.setdefault("customer", {}).setdefault("id", customer_snapshot["id"])
            for key in ("first_name", "last_name", "phone"):
                if not (snapshot["customer"].get(key) if snapshot.get("customer") else None):
                    value = customer_snapshot.get(key)
                    if value is not None:
                        snapshot["customer"][key] = value
        if location_snapshot and str(location_snapshot.get("id")) == str(snapshot.get("location_id")):
            snapshot["invoice_location"] = {
                key: location_snapshot[key] for key in ("id", "name", "address1", "city")
                if location_snapshot.get(key) is not None
            }
        return snapshot

    def _post_cash_sale(self, sale: Dict[str, Any]) -> Dict[str, Any]:
        """Post only a completed cash sale; the sale id makes posting idempotent."""
        if sale["payment_method"] != "cash":
            return self._sales.update(sale["sale_id"], sale["status"], cash_post_status="NOT_REQUIRED")
        if self._tills is None:
            return self._sales.update(sale["sale_id"], sale["status"], cash_post_status="PENDING", error="cash till posting is not configured")
        try:
            if not self._tills.movement_exists(sale["sale_id"]):
                self._tills.movement(
                    sale["till_session_id"], "cash_sale", int(sale["amount_paise"]),
                    sale.get("operator") or "unattributed", "completed checkout", sale["sale_id"],
                )
            return self._sales.update(sale["sale_id"], sale["status"], cash_post_status="POSTED")
        except Exception as exc:
            return self._sales.update(sale["sale_id"], sale["status"], cash_post_status="PENDING", error="cash posting pending: {}".format(exc))

    def _validate_till(self, request: CheckoutRequest) -> None:
        if self._tills is None:
            raise CheckoutError("cash till is not configured")
        try:
            till = self._tills.till_get(request.till_session_id)
        except Exception as exc:
            raise CheckoutError("cash till session is invalid") from exc
        if till.get("status") != "OPEN" or till.get("store_code") != request.cart.store_code or str(till.get("location_id")) != str(request.cart.location_id):
            raise CheckoutError("cash till session must be open and belong to the checkout store")

    def _invoice_prefix(self, cart: Cart) -> Tuple[str, Dict[str, Any]]:
        for location in self._locations.list_locations():
            metadata = location.get("eluno") or {}
            if str(location.get("id")) == str(cart.location_id) and metadata.get("store_code") == cart.store_code:
                prefix = metadata.get("invoice_prefix")
                if location.get("active") and metadata.get("kind") == "store" and isinstance(prefix, str):
                    return prefix, {
                        key: location[key] for key in ("id", "name", "address1", "city")
                        if location.get(key) is not None
                    }
        raise CheckoutError("store context does not match an active billing location")

    @staticmethod
    def _validate_request(request: CheckoutRequest) -> None:
        if not request.payment_method or not request.channel or not request.staff_id:
            raise CheckoutError("payment_method, channel, and staff_id are required")
        if request.channel not in ("walk_in", "whatsapp_booked", "online_pickup"):
            raise CheckoutError("channel is not supported")
        if request.payment_method == "cash" and not request.till_session_id:
            raise CheckoutError("cash checkout requires an open till_session_id")

    @staticmethod
    def _result(sale: Dict[str, Any], message: str) -> CheckoutResult:
        return CheckoutResult(
            sale_id=sale["sale_id"], status=sale["status"], order_id=sale.get("order_id"),
            invoice_no=sale["invoice_no"] if sale["invoice_state"] == "ISSUED" else None, message=message,
        )

    @staticmethod
    def _order_payload(request: CheckoutRequest, pricing: Any) -> Dict[str, Any]:
        line_items = []
        for index, line in enumerate(pricing.lines):
            properties = [
                {"name": "line_type", "value": line.catalog_variant.line_type},
                {"name": "hsn", "value": line.catalog_variant.hsn},
            ]
            if line.cart_line.pair_ref:
                properties.append({"name": "pair_ref", "value": line.cart_line.pair_ref})
            if line.cart_line.prescription_id:
                properties.append({"name": "rx_id", "value": line.cart_line.prescription_id})
            if line.cart_line.prescription_assistance_requested:
                properties.append({"name": "prescription_assistance_requested", "value": "true"})
            if line.catalog_variant.lens_type:
                properties.append({"name": "lens_config", "value": "{}/{}/{}".format(
                    line.catalog_variant.lens_type, line.catalog_variant.index, line.catalog_variant.coating)})
            allocations = ([] if not line.discount_paise else [{"amount": decimal_string_from_paise(line.discount_paise), "discount_application_index": 0}])
            line_items.append({
                "variant_id": int(line.catalog_variant.variant_id), "quantity": line.cart_line.quantity,
                "price": decimal_string_from_paise(line.catalog_variant.price_paise),
                "total_discount": decimal_string_from_paise(line.discount_paise),
                "tax_lines": [
                    {"title": "CGST", "rate": line.rate_bps / 20000, "price": decimal_string_from_paise(line.cgst_paise)},
                    {"title": "SGST", "rate": line.rate_bps / 20000, "price": decimal_string_from_paise(line.sgst_paise)},
                ], "discount_allocations": allocations, "properties": properties,
            })
        applied = [decision for decision in pricing.offers.applied]
        offer_codes = ",".join(decision.offer_id for decision in applied)
        return {"taxes_included": True, "location_id": int(request.cart.location_id),
                "inventory_behaviour": "decrement_obeying_policy", "source_name": "eluno-pos",
                "customer": ({"id": int(request.customer_id)} if request.customer_id else None),
                "tags": "pos,{}".format(request.channel),
                "note_attributes": [
                    {"name": "invoice_no", "value": "__ALLOCATED_AT_CREATE__"},
                    {"name": "store_code", "value": request.cart.store_code},
                    {"name": "channel", "value": request.channel}, {"name": "staff_id", "value": request.staff_id},
                    {"name": "payment_method", "value": request.payment_method},
                    {"name": "offer_codes", "value": offer_codes},
                ], "line_items": line_items,
                "discount_applications": ([{"type": "manual", "title": "POS_OFFERS", "value": decimal_string_from_paise(pricing.discount_paise),
                    "value_type": "fixed_amount", "allocation_method": "each", "target_selection": "explicit", "target_type": "line_item"}] if applied else [])}

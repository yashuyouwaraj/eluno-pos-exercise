"""Separate customer identities and durable, unpaid online-order records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .auth import _password_hash, _verify_password, validate_password


class CustomerAccountError(ValueError):
    pass


class CustomerLoginThrottled(CustomerAccountError):
    pass


class CustomerOrderConflict(ValueError):
    pass


@dataclass(frozen=True)
class CustomerAccount:
    customer_id: str
    shopify_customer_id: str
    phone: str
    display_name: str
    email: Optional[str] = None
    address: Optional[Dict[str, str]] = None


class CustomerAccountStore:
    def __init__(self, database_path: Path) -> None:
        self._path = str(database_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialise(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS customer_accounts (
                customer_id TEXT PRIMARY KEY,
                shopify_customer_id TEXT NOT NULL UNIQUE,
                phone TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                email TEXT,
                address_json TEXT NOT NULL DEFAULT '{}',
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(customer_accounts)")}
            if "email" not in columns:
                connection.execute("ALTER TABLE customer_accounts ADD COLUMN email TEXT")
            if "address_json" not in columns:
                connection.execute(
                    "ALTER TABLE customer_accounts ADD COLUMN address_json TEXT NOT NULL DEFAULT '{}'"
                )
            connection.execute("""CREATE TABLE IF NOT EXISTS customer_sessions (
                token_hash TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL REFERENCES customer_accounts(customer_id) ON DELETE CASCADE,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS customer_login_attempts (
                client_key TEXT PRIMARY KEY,
                failure_count INTEGER NOT NULL,
                window_started REAL NOT NULL,
                locked_until REAL NOT NULL
            )""")

    @staticmethod
    def _account(row: sqlite3.Row) -> CustomerAccount:
        return CustomerAccount(
            row["customer_id"], row["shopify_customer_id"], row["phone"], row["display_name"],
            row["email"], json.loads(row["address_json"]),
        )

    def create_account(
        self, shopify_customer_id: str, phone: str, display_name: str, password: str,
        email: Optional[str] = None,
    ) -> CustomerAccount:
        if not shopify_customer_id or not shopify_customer_id.isascii() or not shopify_customer_id.isdecimal():
            raise CustomerAccountError("Shopify customer ID is invalid.")
        if not isinstance(phone, str) or not phone:
            raise CustomerAccountError("A valid customer mobile number is required.")
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name.strip()) > 100:
            raise CustomerAccountError("Display name must contain 1–100 characters.")
        validate_password(password)
        now = datetime.now(timezone.utc).isoformat()
        account_id = "customer_" + secrets.token_hex(16)
        try:
            with self._lock, closing(self._connect()) as connection:
                connection.execute(
                    """INSERT INTO customer_accounts
                       (customer_id, shopify_customer_id, phone, display_name, email, password_hash, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (account_id, shopify_customer_id, phone, display_name.strip(), email,
                     _password_hash(password), now, now),
                )
                row = connection.execute(
                    "SELECT * FROM customer_accounts WHERE customer_id = ?", (account_id,)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise CustomerAccountError("An account already exists for these customer details.") from exc
        return self._account(row)

    def login(
        self, phone: str, password: str, remote_address: str
    ) -> Optional[Tuple[str, CustomerAccount]]:
        key = hashlib.sha256((remote_address + "\0" + str(phone)).encode("utf-8")).hexdigest()
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            attempt = connection.execute(
                "SELECT failure_count, window_started, locked_until FROM customer_login_attempts WHERE client_key = ?",
                (key,),
            ).fetchone()
            if attempt and attempt["locked_until"] > now:
                raise CustomerLoginThrottled("Too many sign-in attempts. Try again later.")
            if attempt and now - attempt["window_started"] > 600:
                connection.execute("DELETE FROM customer_login_attempts WHERE client_key = ?", (key,))
                attempt = None
            row = connection.execute(
                "SELECT * FROM customer_accounts WHERE phone = ? AND is_active = 1", (phone,)
            ).fetchone()
            encoded = row["password_hash"] if row else _DUMMY_PASSWORD_HASH
            valid = _verify_password(password, encoded)
            if not row or not valid:
                failures = (attempt["failure_count"] if attempt else 0) + 1
                window_started = attempt["window_started"] if attempt else now
                locked_until = now + 600 if failures >= 5 else 0
                connection.execute(
                    """INSERT INTO customer_login_attempts
                       (client_key, failure_count, window_started, locked_until)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(client_key) DO UPDATE SET
                       failure_count=excluded.failure_count,
                       window_started=excluded.window_started,
                       locked_until=excluded.locked_until""",
                    (key, failures, window_started, locked_until),
                )
                return None
            connection.execute("DELETE FROM customer_login_attempts WHERE client_key = ?", (key,))
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
            connection.execute(
                "INSERT INTO customer_sessions VALUES (?, ?, ?, ?)",
                (token_hash, row["customer_id"], now, now + 60 * 60 * 24 * 14),
            )
            return token, self._account(row)

    def authenticate(self, token: str) -> Optional[CustomerAccount]:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT a.* FROM customer_sessions s
                   JOIN customer_accounts a ON a.customer_id = s.customer_id
                   WHERE s.token_hash = ? AND s.expires_at > ? AND a.is_active = 1""",
                (token_hash, time.time()),
            ).fetchone()
            if not row:
                connection.execute(
                    "DELETE FROM customer_sessions WHERE token_hash = ?", (token_hash,)
                )
                return None
            return self._account(row)

    def logout(self, token: str) -> None:
        token_hash = hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("DELETE FROM customer_sessions WHERE token_hash = ?", (token_hash,))

    def update_profile(
        self, account: CustomerAccount, display_name: str, phone: str,
        email: Optional[str], address: Dict[str, str],
    ) -> CustomerAccount:
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name.strip()) > 100:
            raise CustomerAccountError("Name must contain 1–100 characters.")
        if not isinstance(phone, str) or not phone:
            raise CustomerAccountError("A valid customer mobile number is required.")
        if email is not None and (
            not isinstance(email, str) or len(email) > 254 or "@" not in email
            or any(character.isspace() for character in email)
        ):
            raise CustomerAccountError("Enter a valid email address.")
        allowed = {"line1", "line2", "city", "state", "postal_code"}
        if not isinstance(address, dict) or set(address) - allowed:
            raise CustomerAccountError("Delivery address contains unsupported fields.")
        if any(not isinstance(value, str) or len(value) > 200 for value in address.values()):
            raise CustomerAccountError("Delivery address fields must be text of at most 200 characters.")
        with self._lock, closing(self._connect()) as connection:
            try:
                connection.execute(
                    """UPDATE customer_accounts SET display_name=?, phone=?, email=?, address_json=?, updated_at=?
                       WHERE customer_id=? AND is_active=1""",
                    (
                        display_name.strip(), phone, email,
                        json.dumps(address, sort_keys=True, separators=(",", ":")),
                        datetime.now(timezone.utc).isoformat(), account.customer_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise CustomerAccountError("Another account already uses this mobile number.") from exc
            row = connection.execute(
                "SELECT * FROM customer_accounts WHERE customer_id=?", (account.customer_id,)
            ).fetchone()
        if not row:
            raise CustomerAccountError("Customer account is unavailable.")
        return self._account(row)

    def phone_available(self, phone: str, customer_id: str) -> bool:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM customer_accounts WHERE phone = ? AND customer_id != ?",
                (phone, customer_id),
            ).fetchone()
        return row is None

    def change_password(
        self, account: CustomerAccount, current_password: str, new_password: str
    ) -> None:
        validate_password(new_password)
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT password_hash FROM customer_accounts WHERE customer_id = ? AND is_active = 1",
                (account.customer_id,),
            ).fetchone()
            if not row or not _verify_password(current_password, row["password_hash"]):
                raise CustomerAccountError("Current password is incorrect.")
            connection.execute(
                "UPDATE customer_accounts SET password_hash = ?, updated_at = ? WHERE customer_id = ?",
                (_password_hash(new_password), datetime.now(timezone.utc).isoformat(), account.customer_id),
            )
            connection.execute(
                "DELETE FROM customer_sessions WHERE customer_id = ?",
                (account.customer_id,),
            )


class CustomerOrderStore:
    def __init__(self, database_path: Path) -> None:
        self._path = str(database_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS customer_online_orders (
                order_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL REFERENCES customer_accounts(customer_id),
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_payment','cancelled')),
                created_at TEXT NOT NULL,
                order_json TEXT NOT NULL,
                store_code TEXT NOT NULL DEFAULT '',
                confirmation_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(confirmation_status IN ('pending','confirmed','denied')),
                UNIQUE(customer_id, idempotency_key)
            )""")
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(customer_online_orders)")
            }
            if "store_code" not in columns:
                connection.execute(
                    "ALTER TABLE customer_online_orders ADD COLUMN store_code TEXT NOT NULL DEFAULT ''"
                )
            if "confirmation_status" not in columns:
                connection.execute(
                    """ALTER TABLE customer_online_orders
                       ADD COLUMN confirmation_status TEXT NOT NULL DEFAULT 'pending'
                       CHECK(confirmation_status IN ('pending','confirmed','denied'))"""
                )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS customer_order_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL REFERENCES customer_online_orders(order_id),
                    action TEXT NOT NULL CHECK(action IN ('STORE_CONFIRMED','STORE_DENIED')),
                    actor_staff_id TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS customer_order_queue
                   ON customer_online_orders(store_code, confirmation_status, created_at)"""
            )
            for row in connection.execute(
                """SELECT order_id, order_json, store_code, confirmation_status
                   FROM customer_online_orders"""
            ).fetchall():
                saved = json.loads(row["order_json"])
                store_code = row["store_code"] or str(saved.get("store_code") or "")
                needs_update = not row["store_code"] or any(
                    key not in saved for key in (
                        "store_confirmation_status",
                        "store_decision_by",
                        "store_decision_at",
                        "store_denial_reason",
                    )
                )
                if not needs_update:
                    continue
                saved.setdefault("store_confirmation_status", row["confirmation_status"])
                saved.setdefault("store_decision_by", None)
                saved.setdefault("store_decision_at", None)
                saved.setdefault("store_denial_reason", None)
                connection.execute(
                    """UPDATE customer_online_orders
                       SET store_code = ?, order_json = ? WHERE order_id = ?""",
                    (
                        store_code,
                        json.dumps(saved, sort_keys=True, separators=(",", ":")),
                        row["order_id"],
                    ),
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _request_hash(request_payload: Dict[str, Any]) -> str:
        encoded = json.dumps(request_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_idempotency_key(idempotency_key: str) -> str:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 100:
            raise CustomerOrderConflict("A valid idempotency key is required.")
        return idempotency_key.strip()

    def replay(
        self, customer_id: str, idempotency_key: str, request_payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        key = self._validate_idempotency_key(idempotency_key)
        request_hash = self._request_hash(request_payload)
        with closing(self._connect()) as connection:
            existing = connection.execute(
                """SELECT * FROM customer_online_orders
                   WHERE customer_id = ? AND idempotency_key = ?""",
                (customer_id, key),
            ).fetchone()
        if not existing:
            return None
        if existing["request_hash"] != request_hash:
            raise CustomerOrderConflict("This idempotency key was already used for a different order.")
        return json.loads(existing["order_json"])

    def create(
        self, customer_id: str, idempotency_key: str, order: Dict[str, Any],
        request_payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], bool]:
        key = self._validate_idempotency_key(idempotency_key)
        request_hash = self._request_hash(request_payload if request_payload is not None else order)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT * FROM customer_online_orders
                   WHERE customer_id = ? AND idempotency_key = ?""",
                (customer_id, key),
            ).fetchone()
            if existing:
                connection.execute("COMMIT")
                if existing["request_hash"] != request_hash:
                    raise CustomerOrderConflict("This idempotency key was already used for a different order.")
                return json.loads(existing["order_json"]), True
            order_id = "web_" + secrets.token_hex(12)
            record = {
                "order_id": order_id,
                "status": "awaiting_payment",
                "store_confirmation_status": "pending",
                "store_decision_by": None,
                "store_decision_at": None,
                "store_denial_reason": None,
                **order,
            }
            connection.execute(
                """INSERT INTO customer_online_orders
                   (order_id, customer_id, idempotency_key, request_hash, status, created_at, order_json,
                    store_code, confirmation_status)
                   VALUES (?, ?, ?, ?, 'awaiting_payment', ?, ?, ?, 'pending')""",
                (
                    order_id, customer_id, key, request_hash,
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(record, sort_keys=True, separators=(",", ":")),
                    str(record.get("store_code") or ""),
                ),
            )
            connection.execute("COMMIT")
            return record, False

    def pending_for_store(self, store_code: str) -> List[Dict[str, Any]]:
        if not isinstance(store_code, str) or not store_code.strip():
            raise CustomerOrderConflict("A cashier's assigned store is required.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT order_json FROM customer_online_orders
                   WHERE store_code = ? AND status = 'awaiting_payment'
                   AND confirmation_status = 'pending'
                   ORDER BY created_at, order_id""",
                (store_code.strip(),),
            ).fetchall()
        return [json.loads(row["order_json"]) for row in rows]

    def prescription_assistance_for_store(self, store_code: str) -> List[Dict[str, Any]]:
        if not isinstance(store_code, str) or not store_code.strip():
            raise CustomerOrderConflict("An Optometrist's assigned store is required.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT order_json FROM customer_online_orders
                   WHERE store_code = ? AND status = 'awaiting_payment'
                   AND confirmation_status = 'confirmed'
                   ORDER BY created_at, order_id""",
                (store_code.strip(),),
            ).fetchall()
        orders = [json.loads(row["order_json"]) for row in rows]
        return [
            order for order in orders
            if any(item.get("prescription_assistance_requested") is True
                   for item in order.get("items", []))
        ]

    def decide_for_store(
        self,
        order_id: str,
        store_code: str,
        staff_id: str,
        decision: str,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        if decision not in ("confirm", "deny"):
            raise CustomerOrderConflict("Choose confirm or deny for the store decision.")
        if not isinstance(staff_id, str) or not staff_id.strip():
            raise CustomerOrderConflict("An authenticated staff identity is required.")
        if decision == "deny" and (not isinstance(reason, str) or not reason.strip()):
            raise CustomerOrderConflict("A reason is required when denying an order.")
        if decision == "deny" and len(reason.strip()) > 400:
            raise CustomerOrderConflict("The denial reason must be 400 characters or fewer.")
        now = datetime.now(timezone.utc).isoformat()
        action = "STORE_CONFIRMED" if decision == "confirm" else "STORE_DENIED"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM customer_online_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if not row:
                connection.execute("ROLLBACK")
                raise CustomerOrderConflict("The order was not found.")
            if row["store_code"] != store_code:
                connection.execute("ROLLBACK")
                raise CustomerOrderConflict("This order belongs to a different store.")
            if row["status"] != "awaiting_payment":
                connection.execute("ROLLBACK")
                raise CustomerOrderConflict("This cancelled order is no longer eligible for review.")
            if row["confirmation_status"] != "pending":
                connection.execute("ROLLBACK")
                raise CustomerOrderConflict("A store decision has already been recorded.")
            order = json.loads(row["order_json"])
            if decision == "deny" and order.get("payment_status") == "paid":
                connection.execute("ROLLBACK")
                raise CustomerOrderConflict(
                    "This order has a captured payment and cannot be denied until a supported refund is recorded."
                )
            order["store_confirmation_status"] = "confirmed" if decision == "confirm" else "denied"
            order["store_decision_by"] = staff_id.strip()
            order["store_decision_at"] = now
            order["store_denial_reason"] = reason.strip() if decision == "deny" else None
            if decision == "confirm":
                assistance_requested = any(
                    item.get("prescription_assistance_requested") is True
                    for item in order.get("items", [])
                )
                order["status_message"] = (
                    "Store confirmed. Prescription assistance remains requested and awaits Optometrist review."
                    if assistance_requested
                    else "Store confirmed. Payment is still pending; this order has not been fulfilled."
                )
            else:
                order["status"] = "cancelled"
                order["status_message"] = (
                    "The store declined this unpaid order request: {}. No payment was captured."
                    .format(reason.strip())
                )
            connection.execute(
                """UPDATE customer_online_orders
                   SET status = ?, confirmation_status = ?, order_json = ?
                   WHERE order_id = ? AND confirmation_status = 'pending'""",
                (
                    order["status"],
                    order["store_confirmation_status"],
                    json.dumps(order, sort_keys=True, separators=(",", ":")),
                    order_id,
                ),
            )
            connection.execute(
                """INSERT INTO customer_order_events
                   (order_id, action, actor_staff_id, reason, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (order_id, action, staff_id.strip(), order["store_denial_reason"], now),
            )
            connection.execute("COMMIT")
        return order

    def list_for_customer(
        self, customer_id: str, page: int = 1, limit: int = 20
    ) -> Tuple[List[Dict[str, Any]], bool]:
        if not isinstance(page, int) or page < 1 or not 1 <= limit <= 50:
            raise CustomerOrderConflict("Order-history page is invalid.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT order_json FROM customer_online_orders
                   WHERE customer_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (customer_id, limit + 1, (page - 1) * limit),
            ).fetchall()
        return [json.loads(row["order_json"]) for row in rows[:limit]], len(rows) > limit


_DUMMY_PASSWORD_HASH = _password_hash("Invalid-Customer-Password-2026!")

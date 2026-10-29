"""Persistent staff accounts and revocable browser sessions for the local POS."""

from dataclasses import dataclass
from contextlib import closing
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4


SESSION_TTL_SECONDS = 8 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCK_SECONDS = 15 * 60
MAX_LOGIN_FAILURES = 5
MAX_PASSWORD_LENGTH = 1024
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
DEMO_ACCOUNTS = (
    ("admin", "ElunoAdmin@2026", "admin", "Demo Admin"),
    ("cashier", "ElunoCashier@2026", "cashier", "Demo Cashier"),
)
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")


class AuthenticationThrottled(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("Too many sign-in attempts. Wait before trying again.")
        self.retry_after = max(1, retry_after)


class DuplicateUsername(ValueError):
    pass


class StaffAccountError(ValueError):
    pass


@dataclass(frozen=True)
class AuthenticatedUser:
    username: str
    staff_id: str
    role: str
    display_name: str
    store_code: Optional[str] = None
    external_staff_id: Optional[str] = None


def normalize_username(value: str) -> str:
    if not isinstance(value, str):
        raise StaffAccountError("Username must be text.")
    normalized = value.strip().lower()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise StaffAccountError(
            "Username must be 3–32 characters using letters, numbers, dot, underscore or hyphen."
        )
    return normalized


def validate_password(value: str) -> None:
    if not isinstance(value, str) or len(value) < 12 or len(value) > MAX_PASSWORD_LENGTH:
        raise StaffAccountError("Password must contain 12–1024 characters.")
    if not (
        any(character.islower() for character in value)
        and any(character.isupper() for character in value)
        and any(character.isdigit() for character in value)
        and any(not character.isalnum() for character in value)
    ):
        raise StaffAccountError(
            "Password must include uppercase and lowercase letters, a number, and a symbol."
        )


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_value, digest_value = encoded.split("$")
        if algorithm != "scrypt":
            return False
        expected = base64.urlsafe_b64decode(digest_value.encode("ascii"))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.urlsafe_b64decode(salt_value.encode("ascii")),
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeEncodeError):
        return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuthManager:
    """SQLite is the source of truth; session tokens are only returned once at login."""

    def __init__(self, database_path: Path, bootstrap_accounts: Optional[List[Dict[str, str]]] = None) -> None:
        self._path = str(database_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialise()
        for account in bootstrap_accounts or []:
            self._seed_if_missing(account)
        for username, password, role, display_name in DEMO_ACCOUNTS:
            self._seed_if_missing({
                "username": username,
                "password": password,
                "role": role,
                "display_name": display_name,
            })

    @property
    def configured(self) -> bool:
        return True

    @classmethod
    def from_env(cls, database_path: Optional[Path] = None) -> "AuthManager":
        if database_path is None:
            configured_path = os.environ.get("ELUNO_STATE_DB")
            if configured_path:
                database_path = Path(configured_path)
            else:
                database_path = Path(__file__).resolve().parents[1] / "eluno_pos_state.sqlite3"
        raw = os.environ.get("ELUNO_AUTH_USERS", "").strip()
        if not raw:
            return cls(database_path)
        try:
            configured = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("ELUNO_AUTH_USERS must be a JSON object keyed by username") from exc
        if not isinstance(configured, dict):
            raise ValueError("ELUNO_AUTH_USERS must be a JSON object keyed by username")
        accounts = []
        seen = set()
        for username, entry in configured.items():
            if not isinstance(username, str) or not isinstance(entry, dict):
                raise ValueError("each ELUNO_AUTH_USERS account requires a username and object configuration")
            normalized = normalize_username(username)
            if normalized in seen:
                raise ValueError("ELUNO_AUTH_USERS contains duplicate usernames")
            seen.add(normalized)
            role = entry.get("role")
            password = entry.get("password")
            if role not in ("admin", "cashier"):
                raise ValueError("configured POS account roles must be admin or cashier")

            validate_password(password)
            staff_id = entry.get("staff_id")
            if staff_id is not None and (not isinstance(staff_id, str) or not staff_id.strip() or len(staff_id.strip()) > 100):
                raise ValueError("configured POS staff_id must be a non-empty string of at most 100 characters")
            accounts.append({
                "username": normalized,
                "password": password,
                "role": role,
                "staff_id": (staff_id.strip() if isinstance(staff_id, str) else ""),
                "display_name": entry.get("display_name", normalized),
                "created_by": "environment-bootstrap",
            })
        return cls(database_path, accounts)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialise(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS staff_accounts (
                staff_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin','cashier','optometrist')),
                is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                display_name TEXT NOT NULL,
                created_by TEXT,
                store_code TEXT,
                external_staff_id TEXT,
                CHECK((role='optometrist' AND external_staff_id IS NOT NULL)
                      OR (role!='optometrist' AND external_staff_id IS NULL))
            )""")
            schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='staff_accounts'"
            ).fetchone()
            if schema and "'optometrist'" not in schema["sql"]:
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute("BEGIN IMMEDIATE")
                has_sessions = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_sessions'"
                ).fetchone() is not None
                if has_sessions:
                    connection.execute("ALTER TABLE auth_sessions RENAME TO auth_sessions_legacy")
                connection.execute("ALTER TABLE staff_accounts RENAME TO staff_accounts_legacy")
                connection.execute("""CREATE TABLE staff_accounts (
                    staff_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin','cashier','optometrist')),
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    created_by TEXT,
                    store_code TEXT,
                    external_staff_id TEXT,
                    CHECK((role='optometrist' AND external_staff_id IS NOT NULL)
                          OR (role!='optometrist' AND external_staff_id IS NULL))
                )""")
                legacy_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(staff_accounts_legacy)")
                }
                store_expr = "store_code" if "store_code" in legacy_columns else (
                    "CASE WHEN role='cashier' THEN 'HSR' ELSE NULL END"
                )
                external_expr = "external_staff_id" if "external_staff_id" in legacy_columns else "NULL"
                connection.execute(
                    """INSERT INTO staff_accounts
                       (staff_id,username,password_hash,role,is_active,created_at,updated_at,display_name,
                        created_by,store_code,external_staff_id)
                       SELECT staff_id,username,password_hash,role,is_active,created_at,updated_at,
                              display_name,created_by,{},{} FROM staff_accounts_legacy""".format(
                        store_expr, external_expr
                    )
                )
                if has_sessions:
                    connection.execute("""CREATE TABLE auth_sessions (
                        token_hash TEXT PRIMARY KEY,
                        staff_id TEXT NOT NULL REFERENCES staff_accounts(staff_id) ON DELETE CASCADE,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    )""")
                    connection.execute(
                        "INSERT INTO auth_sessions SELECT token_hash,staff_id,created_at,expires_at FROM auth_sessions_legacy"
                    )
                    connection.execute("DROP TABLE auth_sessions_legacy")
                connection.execute("DROP TABLE staff_accounts_legacy")
                connection.execute("COMMIT")
                connection.execute("PRAGMA foreign_keys=ON")
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(staff_accounts)")
            }
            if "store_code" not in columns:
                connection.execute("ALTER TABLE staff_accounts ADD COLUMN store_code TEXT")
                connection.execute(
                    "UPDATE staff_accounts SET store_code='HSR' WHERE role='cashier'"
                )
            if "external_staff_id" not in columns:
                connection.execute("ALTER TABLE staff_accounts ADD COLUMN external_staff_id TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS staff_username_normalized ON staff_accounts(lower(username))"
            )
            connection.execute("""CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                staff_id TEXT NOT NULL REFERENCES staff_accounts(staff_id) ON DELETE CASCADE,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS auth_sessions_staff_id ON auth_sessions(staff_id)")
            connection.execute("""CREATE TABLE IF NOT EXISTS auth_login_attempts (
                client_key TEXT PRIMARY KEY,
                failure_count INTEGER NOT NULL,
                window_started REAL NOT NULL,
                locked_until REAL NOT NULL
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS staff_audit_events (
                event_id TEXT PRIMARY KEY,
                actor_staff_id TEXT NOT NULL,
                subject_staff_id TEXT NOT NULL,
                action TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )""")

    def _seed_if_missing(self, account: Dict[str, str]) -> None:
        username = normalize_username(account["username"])
        role = account["role"]
        if role not in ("admin", "cashier"):
            raise ValueError("configured POS account roles must be admin or cashier")
        validate_password(account["password"])
        display_name = str(account.get("display_name") or username).strip()
        if not display_name or len(display_name) > 100:
            raise ValueError("display name must contain 1–100 characters")
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM staff_accounts WHERE username = ?", (username,)
            ).fetchone()
            if exists:
                connection.execute("COMMIT")
                return
            reserved_role = {"admin": "admin", "cashier": "cashier"}.get(username)
            if reserved_role is not None and role != reserved_role:
                connection.execute("ROLLBACK")
                raise ValueError("configured demo usernames must keep their Admin/Cashier roles")
            staff_id = account.get("staff_id") or "staff_" + uuid4().hex
            now = _utc_now()
            connection.execute(
                """INSERT INTO staff_accounts
                   (staff_id, username, password_hash, role, is_active, created_at, updated_at, display_name, created_by, store_code)
                   VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
                (staff_id, username, _password_hash(account["password"]), role, now, now,
                 display_name, account.get("created_by"), account.get("store_code") or ("HSR" if username == "cashier" else None)),
            )
            connection.execute("COMMIT")

    @staticmethod
    def _public_user(row: sqlite3.Row) -> AuthenticatedUser:
        return AuthenticatedUser(
            row["username"], row["staff_id"], row["role"], row["display_name"],
            row["store_code"] if "store_code" in row.keys() else None,
            row["external_staff_id"] if "external_staff_id" in row.keys() else None,
        )

    @staticmethod
    def _public_staff(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "staff_id": row["staff_id"],
            "operator_id": row["staff_id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
            "is_active": bool(row["is_active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "store_code": row["store_code"] if "store_code" in row.keys() else None,
            "external_staff_id": row["external_staff_id"] if "external_staff_id" in row.keys() else None,
        }

    def _client_key(self, remote_address: str) -> str:
        return hashlib.sha256(remote_address.encode("utf-8", "replace")).hexdigest()

    def login(self, username: str, password: str, remote_address: str = "local") -> Optional[Tuple[str, AuthenticatedUser]]:
        normalized = username.strip().lower() if isinstance(username, str) else ""
        key = self._client_key(remote_address)
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            attempt = connection.execute(
                "SELECT failure_count, window_started, locked_until FROM auth_login_attempts WHERE client_key = ?",
                (key,),
            ).fetchone()
            if attempt and attempt["locked_until"] > now:
                raise AuthenticationThrottled(int(attempt["locked_until"] - now) + 1)
            row = connection.execute(
                "SELECT * FROM staff_accounts WHERE username = ? AND is_active = 1", (normalized,)
            ).fetchone()
            stored_hash = row["password_hash"] if row else _DUMMY_PASSWORD_HASH
            password_in_range = isinstance(password, str) and len(password) <= MAX_PASSWORD_LENGTH
            candidate = password if password_in_range else ""
            valid_password = _verify_password(candidate, stored_hash) and password_in_range
            if row is None or not valid_password:
                if attempt and now - attempt["window_started"] < LOGIN_WINDOW_SECONDS:
                    failures = int(attempt["failure_count"]) + 1
                    window_started = float(attempt["window_started"])
                else:
                    failures = 1
                    window_started = now
                locked_until = now + LOGIN_LOCK_SECONDS if failures >= MAX_LOGIN_FAILURES else 0
                connection.execute(
                    """INSERT INTO auth_login_attempts(client_key, failure_count, window_started, locked_until)
                       VALUES (?, ?, ?, ?) ON CONFLICT(client_key) DO UPDATE SET
                       failure_count=excluded.failure_count, window_started=excluded.window_started,
                       locked_until=excluded.locked_until""",
                    (key, failures, window_started, locked_until),
                )
                if locked_until:
                    raise AuthenticationThrottled(LOGIN_LOCK_SECONDS)
                return None

            connection.execute("DELETE FROM auth_login_attempts WHERE client_key = ?", (key,))
            token = secrets.token_urlsafe(32)
            connection.execute(
                "INSERT INTO auth_sessions(token_hash, staff_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (_token_hash(token), row["staff_id"], now, now + SESSION_TTL_SECONDS),
            )
            return token, self._public_user(row)

    def user_for_token(self, token: str) -> Optional[AuthenticatedUser]:
        if not token:
            return None
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT a.* FROM auth_sessions s JOIN staff_accounts a ON a.staff_id=s.staff_id
                   WHERE s.token_hash=? AND s.expires_at>? AND a.is_active=1""",
                (_token_hash(token), time.time()),
            ).fetchone()
            if row is None:
                connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (time.time(),))
                return None
            return self._public_user(row)

    def logout(self, token: str) -> None:
        if not token:
            return
        with self._lock, closing(self._connect()) as connection:
            connection.execute("DELETE FROM auth_sessions WHERE token_hash=?", (_token_hash(token),))

    def list_staff(self) -> List[Dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM staff_accounts ORDER BY lower(display_name), username"
            ).fetchall()
        return [self._public_staff(row) for row in rows]

    def create_staff(
        self, username: str, password: str, role: str, display_name: Optional[str], created_by: str,
        store_code: Optional[str] = None, external_staff_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized = normalize_username(username)
        validate_password(password)
        if role not in ("admin", "cashier", "optometrist"):
            raise StaffAccountError("Role must be Admin, Cashier, or Optometrist.")
        if role == "optometrist" and (
            not isinstance(external_staff_id, str)
            or not external_staff_id.strip()
            or len(external_staff_id.strip()) > 100
        ):
            raise StaffAccountError("Optometrist accounts must be linked to an authorized clinical staff record.")
        if role != "optometrist" and external_staff_id is not None:
            raise StaffAccountError("Only Optometrist accounts may have a clinical staff link.")
        name = (display_name or normalized).strip()
        if not name or len(name) > 100 or any(ord(character) < 32 for character in name):
            raise StaffAccountError("Display name must contain 1–100 printable characters.")
        now = _utc_now()
        staff_id = "staff_" + uuid4().hex
        try:
            with self._lock, closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO staff_accounts
                       (staff_id, username, password_hash, role, is_active, created_at, updated_at, display_name, created_by, store_code, external_staff_id)
                       VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)""",
                    (
                        staff_id, normalized, _password_hash(password), role, now, now, name,
                        created_by, store_code, external_staff_id.strip() if external_staff_id else None,
                    ),
                )
                row = connection.execute("SELECT * FROM staff_accounts WHERE staff_id=?", (staff_id,)).fetchone()
                connection.execute(
                    "INSERT INTO staff_audit_events VALUES (?, ?, ?, ?, ?, ?)",
                    (uuid4().hex, created_by, staff_id, "STAFF_CREATED", now,
                     json.dumps({"username": normalized, "role": role}, separators=(",", ":"))),
                )
                connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            raise DuplicateUsername("That username already exists.") from exc
        return self._public_staff(row)

    def update_staff(
        self, staff_id: str, changes: Dict[str, Any], updated_by: str = "system"
    ) -> Dict[str, Any]:
        allowed = {"display_name", "role", "is_active", "password", "store_code", "external_staff_id"}
        if not changes or set(changes) - allowed:
            raise StaffAccountError("Provide a supported staff update.")
        assignments: Dict[str, Any] = {}
        invalidate_sessions = False
        if "display_name" in changes:
            name = changes["display_name"]
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100 or any(ord(c) < 32 for c in name):
                raise StaffAccountError("Display name must contain 1–100 printable characters.")
            assignments["display_name"] = name.strip()
        if "role" in changes:
            if changes["role"] not in ("admin", "cashier", "optometrist"):
                raise StaffAccountError("Role must be Admin, Cashier, or Optometrist.")
            assignments["role"] = changes["role"]
            invalidate_sessions = True
        if "is_active" in changes:
            if not isinstance(changes["is_active"], bool):
                raise StaffAccountError("is_active must be a boolean.")
            assignments["is_active"] = int(changes["is_active"])
            invalidate_sessions = invalidate_sessions or not changes["is_active"]
        if "password" in changes:
            validate_password(changes["password"])
            assignments["password_hash"] = _password_hash(changes["password"])
            invalidate_sessions = True
        if "store_code" in changes:
            store_code = changes["store_code"]
            if store_code is not None and (
                not isinstance(store_code, str)
                or not re.fullmatch(r"[A-Z0-9_-]{2,20}", store_code)
            ):
                raise StaffAccountError("Store assignment must be a valid store code.")
            assignments["store_code"] = store_code
        if "external_staff_id" in changes:
            external_staff_id = changes["external_staff_id"]
            if external_staff_id is not None and (
                not isinstance(external_staff_id, str)
                or not external_staff_id.strip()
                or len(external_staff_id.strip()) > 100
            ):
                raise StaffAccountError("Clinical staff link must be a non-empty ID.")
            assignments["external_staff_id"] = (
                external_staff_id.strip() if isinstance(external_staff_id, str) else None
            )

        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM staff_accounts WHERE staff_id=?", (staff_id,)).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise StaffAccountError("Staff account was not found.")
            updated_role = assignments.get("role", row["role"])
            updated_active = assignments.get("is_active", row["is_active"])
            updated_external_id = assignments.get("external_staff_id", row["external_staff_id"])
            if updated_role == "optometrist" and not updated_external_id:
                connection.execute("ROLLBACK")
                raise StaffAccountError("Optometrist accounts must be linked to an authorized clinical staff record.")
            if updated_role != "optometrist" and updated_external_id:
                connection.execute("ROLLBACK")
                raise StaffAccountError("Only Optometrist accounts may have a clinical staff link.")
            if updated_role == "admin" and updated_active:
                remaining_admins = connection.execute(
                    """SELECT COUNT(*) FROM staff_accounts WHERE role='admin' AND is_active=1
                       AND staff_id != ?""", (staff_id,)
                ).fetchone()[0]
            else:
                remaining_admins = connection.execute(
                    "SELECT COUNT(*) FROM staff_accounts WHERE role='admin' AND is_active=1 AND staff_id != ?",
                    (staff_id,),
                ).fetchone()[0]
            if row["role"] == "admin" and row["is_active"] and not (
                updated_role == "admin" and updated_active
            ) and remaining_admins == 0:
                connection.execute("ROLLBACK")
                raise StaffAccountError("The only active Admin cannot be disabled or changed to Cashier.")
            assignments["updated_at"] = _utc_now()
            values = list(assignments.values()) + [staff_id]
            connection.execute(
                "UPDATE staff_accounts SET {} WHERE staff_id=?".format(
                    ", ".join("{}=?".format(column) for column in assignments)
                ),
                values,
            )
            if invalidate_sessions:
                connection.execute("DELETE FROM auth_sessions WHERE staff_id=?", (staff_id,))
            metadata = {
                key: value for key, value in assignments.items()
                if key not in ("password_hash", "updated_at")
            }
            if "password_hash" in assignments:
                metadata["password_changed"] = True
            connection.execute(
                "INSERT INTO staff_audit_events VALUES (?, ?, ?, ?, ?, ?)",
                (uuid4().hex, updated_by, staff_id, "STAFF_UPDATED", assignments["updated_at"],
                 json.dumps(metadata, separators=(",", ":"))),
            )
            updated = connection.execute("SELECT * FROM staff_accounts WHERE staff_id=?", (staff_id,)).fetchone()
            connection.execute("COMMIT")
        return self._public_staff(updated)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


_DUMMY_PASSWORD_HASH = _password_hash("Invalid-User-Password-2026!")

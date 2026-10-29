import json
import os
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from eluno_pos.auth import (
    AuthenticationThrottled,
    AuthManager,
    DuplicateUsername,
    StaffAccountError,
)


ADMIN_PASSWORD = "ElunoAdmin@2026"
CASHIER_PASSWORD = "ElunoCashier@2026"
STRONG_PASSWORD = "New-Staff-Password-2026!"


class AuthManagerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.database = Path(self.tempdir.name) / "pos.sqlite3"

    def test_first_initialization_seeds_demo_accounts_with_salted_scrypt_hashes(self):
        with patch.dict(os.environ, {}, clear=True):
            manager = AuthManager.from_env(self.database)
        accounts = {account["username"]: account for account in manager.list_staff()}
        self.assertEqual({"admin", "cashier"}, set(accounts))
        self.assertEqual("admin", accounts["admin"]["role"])
        self.assertEqual("cashier", accounts["cashier"]["role"])
        self.assertTrue(accounts["admin"]["is_active"])

        with closing(sqlite3.connect(str(self.database))) as connection:
            rows = connection.execute(
                "SELECT username, password_hash FROM staff_accounts ORDER BY username"
            ).fetchall()
        hashes = dict(rows)
        self.assertTrue(hashes["admin"].startswith("scrypt$"))
        self.assertTrue(hashes["cashier"].startswith("scrypt$"))
        self.assertNotEqual(ADMIN_PASSWORD, hashes["admin"])
        self.assertNotEqual(CASHIER_PASSWORD, hashes["cashier"])
        self.assertNotEqual(hashes["admin"], hashes["cashier"])

    def test_reinitialization_never_duplicates_accounts_or_restores_password_or_active_status(self):
        manager = AuthManager(self.database)
        admin = next(item for item in manager.list_staff() if item["username"] == "admin")
        manager.update_staff(admin["staff_id"], {"password": STRONG_PASSWORD})
        manager.create_staff("another-admin", STRONG_PASSWORD, "admin", "Other Admin", admin["staff_id"])
        manager.update_staff(admin["staff_id"], {"is_active": False})

        restarted = AuthManager(self.database)
        accounts = restarted.list_staff()
        self.assertEqual(3, len(accounts))
        self.assertFalse(next(item for item in accounts if item["username"] == "admin")["is_active"])
        self.assertIsNone(restarted.login("admin", ADMIN_PASSWORD))
        self.assertIsNone(restarted.login("admin", STRONG_PASSWORD))

    def test_login_is_case_normalized_and_uses_password_not_client_role(self):
        manager = AuthManager(self.database)
        result = manager.login("ADMIN", ADMIN_PASSWORD, "127.0.0.2")
        self.assertIsNotNone(result)
        token, user = result
        self.assertEqual("admin", user.role)
        self.assertEqual("Demo Admin", user.display_name)
        self.assertEqual(user.staff_id, manager.user_for_token(token).staff_id)
        self.assertIsNone(manager.login("admin", "wrong-password", "127.0.0.3"))

    def test_persisted_session_survives_manager_restart_and_logout_revokes_it(self):
        manager = AuthManager(self.database)
        token, _ = manager.login("cashier", CASHIER_PASSWORD, "127.0.0.4")
        with closing(sqlite3.connect(str(self.database))) as connection:
            stored_token = connection.execute(
                "SELECT token_hash FROM auth_sessions"
            ).fetchone()[0]
        self.assertNotEqual(token, stored_token)
        self.assertEqual(64, len(stored_token))
        restarted = AuthManager(self.database)
        self.assertEqual("cashier", restarted.user_for_token(token).role)
        restarted.logout(token)
        self.assertIsNone(manager.user_for_token(token))

    def test_expired_sessions_and_disabled_accounts_are_rejected(self):
        manager = AuthManager(self.database)
        token, user = manager.login("cashier", CASHIER_PASSWORD, "127.0.0.5")
        with closing(sqlite3.connect(str(self.database))) as connection:
            connection.execute(
                "UPDATE auth_sessions SET expires_at=0 WHERE staff_id=?", (user.staff_id,)
            )
            connection.commit()
        self.assertIsNone(manager.user_for_token(token))
        manager.update_staff(user.staff_id, {"is_active": False})
        self.assertIsNone(manager.login("cashier", CASHIER_PASSWORD, "127.0.0.6"))

    def test_staff_creation_rejects_duplicate_username_and_invalid_role(self):
        manager = AuthManager(self.database)
        manager.create_staff("supervisor", STRONG_PASSWORD, "admin", "Supervisor", "demo-admin")
        with self.assertRaises(DuplicateUsername):
            manager.create_staff(" Supervisor ", STRONG_PASSWORD, "cashier", "Duplicate", "demo-admin")
        with self.assertRaises(StaffAccountError):
            manager.create_staff("badrole", STRONG_PASSWORD, "manager", "Bad role", "demo-admin")
        with self.assertRaises(StaffAccountError):
            manager.create_staff("weakpass", "short", "cashier", "Weak", "demo-admin")
        self.assertEqual(3, len(manager.list_staff()))

    def test_operator_ids_are_unique_immutable_and_safe_under_concurrent_creation(self):
        managers = [AuthManager(self.database) for _ in range(6)]

        def create(index):
            return managers[index % len(managers)].create_staff(
                "team-member-{}".format(index),
                STRONG_PASSWORD,
                "admin",
                "Team Member {}".format(index),
                "creator",
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            accounts = list(pool.map(create, range(24)))
        operator_ids = [account["operator_id"] for account in accounts]
        self.assertEqual(len(operator_ids), len(set(operator_ids)))
        for account in accounts:
            with self.assertRaises(StaffAccountError):
                managers[0].update_staff(account["staff_id"], {"staff_id": "forged"})
        self.assertEqual(set(operator_ids), {
            account["operator_id"] for account in managers[0].list_staff()
            if account["username"].startswith("team-member-")
        })

    def test_store_assignment_migration_preserves_existing_staff_ids(self):
        manager = AuthManager(self.database)
        cashier = next(item for item in manager.list_staff() if item["username"] == "cashier")
        legacy_path = Path(self.tempdir.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(str(self.database))) as connection:
            row = connection.execute(
                """SELECT staff_id,username,password_hash,role,is_active,created_at,
                   updated_at,display_name,created_by FROM staff_accounts WHERE username='cashier'"""
            ).fetchone()
        with closing(sqlite3.connect(str(legacy_path))) as connection:
            connection.execute("""CREATE TABLE staff_accounts (
                staff_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, role TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, display_name TEXT NOT NULL, created_by TEXT
            )""")
            connection.execute("INSERT INTO staff_accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
            connection.commit()
        migrated = AuthManager(legacy_path)
        migrated_cashier = next(item for item in migrated.list_staff() if item["username"] == "cashier")
        self.assertEqual(cashier["staff_id"], migrated_cashier["staff_id"])
        self.assertEqual("HSR", migrated_cashier["store_code"])

    def test_optometrist_role_is_persisted_with_store_and_clinical_identity(self):
        manager = AuthManager(self.database)
        account = manager.create_staff(
            "optometrist-one", STRONG_PASSWORD, "optometrist", "Optometrist One",
            "demo-admin", "HSR", "S107",
        )
        token, user = manager.login("optometrist-one", STRONG_PASSWORD)
        self.assertEqual("optometrist", account["role"])
        self.assertEqual("HSR", user.store_code)
        self.assertEqual("S107", user.external_staff_id)
        self.assertEqual("S107", manager.user_for_token(token).external_staff_id)
        with self.assertRaises(StaffAccountError):
            manager.create_staff(
                "unlinked-optometrist", STRONG_PASSWORD, "optometrist", "No link",
                "demo-admin", "HSR",
            )

    def test_last_active_admin_cannot_be_disabled_or_demoted(self):
        manager = AuthManager(self.database)
        admin = next(item for item in manager.list_staff() if item["username"] == "admin")
        with self.assertRaisesRegex(StaffAccountError, "only active Admin"):
            manager.update_staff(admin["staff_id"], {"is_active": False})
        with self.assertRaisesRegex(StaffAccountError, "only active Admin"):
            manager.update_staff(admin["staff_id"], {"role": "cashier"})
        manager.create_staff("another-admin", STRONG_PASSWORD, "admin", "Other Admin", admin["staff_id"])
        manager.update_staff(admin["staff_id"], {"is_active": False})
        self.assertFalse(next(item for item in manager.list_staff() if item["staff_id"] == admin["staff_id"])["is_active"])

    def test_staff_list_does_not_expose_password_hashes_and_password_reset_revokes_session(self):
        manager = AuthManager(self.database)
        token, cashier = manager.login("cashier", CASHIER_PASSWORD, "127.0.0.7")
        self.assertTrue(all("password_hash" not in item and "password" not in item for item in manager.list_staff()))
        manager.update_staff(cashier.staff_id, {"password": STRONG_PASSWORD})
        self.assertIsNone(manager.user_for_token(token))
        self.assertIsNone(manager.login("cashier", CASHIER_PASSWORD, "127.0.0.8"))
        self.assertIsNotNone(manager.login("cashier", STRONG_PASSWORD, "127.0.0.9"))
        with closing(sqlite3.connect(str(self.database))) as connection:
            audit_rows = connection.execute(
                "SELECT metadata_json FROM staff_audit_events"
            ).fetchall()
        self.assertTrue(all(STRONG_PASSWORD not in row[0] for row in audit_rows))
        restarted = AuthManager(self.database)
        self.assertIsNotNone(restarted.login("cashier", STRONG_PASSWORD, "127.0.0.15"))
        self.assertIsNone(restarted.login("cashier", CASHIER_PASSWORD, "127.0.0.16"))

    def test_environment_accounts_are_first_run_bootstrap_only(self):
        config = json.dumps({
            "admin": {
                "password": STRONG_PASSWORD, "role": "admin", "staff_id": "S-LOCAL",
                "display_name": "Configured Admin",
            },
            "manager.one": {
                "password": "Other-Staff-Password-2026!", "role": "cashier", "staff_id": "S-OTHER",
            },
        })
        with patch.dict(os.environ, {"ELUNO_AUTH_USERS": config}):
            manager = AuthManager.from_env(self.database)
        self.assertIsNotNone(manager.login("admin", STRONG_PASSWORD, "127.0.0.10"))
        self.assertIsNotNone(manager.login("manager.one", "Other-Staff-Password-2026!", "127.0.0.11"))

        with patch.dict(os.environ, {"ELUNO_AUTH_USERS": json.dumps({
            "admin": {"password": "Changed-Env-Password-2026!", "role": "cashier"},
        })}):
            restarted = AuthManager.from_env(self.database)
        self.assertIsNotNone(restarted.login("admin", STRONG_PASSWORD, "127.0.0.12"))
        self.assertIsNone(restarted.login("admin", "Changed-Env-Password-2026!", "127.0.0.13"))
        self.assertEqual(3, len(restarted.list_staff()))

    def test_deployment_mode_disables_demo_accounts_and_requires_admin_and_cashier(self):
        config = json.dumps({
            "owner": {"password": STRONG_PASSWORD, "role": "admin"},
            "staff": {"password": "Other-Staff-Password-2026!", "role": "cashier"},
        })
        with patch.dict(os.environ, {
            "ELUNO_DISABLE_DEMO_ACCOUNTS": "1",
            "ELUNO_AUTH_USERS": config,
        }, clear=True):
            manager = AuthManager.from_env(self.database)
        self.assertEqual({"owner", "staff"}, {
            account["username"] for account in manager.list_staff()
        })
        self.assertIsNotNone(manager.login("owner", STRONG_PASSWORD, "127.0.0.20"))
        self.assertIsNotNone(manager.login("staff", "Other-Staff-Password-2026!", "127.0.0.21"))
        self.assertIsNone(manager.login("admin", ADMIN_PASSWORD, "127.0.0.22"))
        self.assertIsNone(manager.login("cashier", CASHIER_PASSWORD, "127.0.0.23"))

    def test_deployment_mode_rejects_missing_or_incomplete_account_configuration(self):
        with patch.dict(os.environ, {"ELUNO_DISABLE_DEMO_ACCOUNTS": "1"}, clear=True):
            with self.assertRaisesRegex(ValueError, "ELUNO_AUTH_USERS is required"):
                AuthManager.from_env(self.database)

        with patch.dict(os.environ, {
            "ELUNO_DISABLE_DEMO_ACCOUNTS": "1",
            "ELUNO_AUTH_USERS": json.dumps({
                "owner": {"password": STRONG_PASSWORD, "role": "admin"},
            }),
        }, clear=True):
            with self.assertRaisesRegex(ValueError, "Admin and one Cashier"):
                AuthManager.from_env(self.database)

    def test_login_attempts_are_throttled(self):
        manager = AuthManager(self.database)
        for _ in range(4):
            self.assertIsNone(manager.login("admin", "incorrect", "127.0.0.14"))
        with self.assertRaises(AuthenticationThrottled):
            manager.login("admin", "incorrect", "127.0.0.14")
        with self.assertRaises(AuthenticationThrottled):
            manager.login("admin", ADMIN_PASSWORD, "127.0.0.14")


if __name__ == "__main__":
    unittest.main()

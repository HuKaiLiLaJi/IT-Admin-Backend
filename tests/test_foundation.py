import json
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import uvicorn

from app.auth import password_key, session_key
from app.database import connect, initialize_database
from app.main import create_app
from app.seed import TEST_PASSWORD, seed_test_data


class HTTPTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template_directory = tempfile.TemporaryDirectory()
        cls.template = Path(cls.template_directory.name) / "template.sqlite3"
        seed_test_data(cls.template)

    @classmethod
    def tearDownClass(cls):
        cls.template_directory.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "test.sqlite3"
        with connect(self.template) as source, connect(self.path) as destination:
            source.backup(destination)
        self.opener = build_opener(ProxyHandler({}))
        self.start_server()

    def start_server(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        self.app = create_app(self.path)
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.listener]}, daemon=True)
        self.addCleanup(self.stop_server)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.server.started, "Server startup failed")

    def stop_server(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.listener.close()
        self.assertFalse(self.thread.is_alive(), "Server did not shut down")

    def request(self, path, method="GET", body=None, token=None, headers=None):
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        request = Request(
            self.url + path, method=method, headers=request_headers,
            data=None if body is None else json.dumps(body).encode("utf-8"),
        )
        try:
            response = self.opener.open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            content = response.read()
            return response.status, json.loads(content) if content else None, response.headers

    def login(self, username="submitter"):
        status, body, headers = self.request(
            "/api/v1/auth/login", "POST", {"username": username, "password": TEST_PASSWORD},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body["token_type"], "bearer")
        return body["access_token"]


class FoundationTests(HTTPTestCase):
    def test_health_openapi_and_no_automatic_test_seed(self):
        self.assertEqual(self.request("/health")[:2], (200, {"status": "ok"}))
        status, schema, _ = self.request("/openapi.json")
        self.assertEqual(status, 200)
        self.assertIn("/api/v1/auth/login", schema["paths"])
        empty_path = Path(self.directory.name) / "empty.sqlite3"
        initialize_database(empty_path)
        initialize_database(empty_path)
        with connect(empty_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_login_identity_logout_and_token_storage(self):
        token = self.login()
        status, body, _ = self.request("/api/v1/me", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], "test-submitter")
        self.assertEqual(body["roles"], ["submitter"])
        self.assertNotIn("password_hash", body)
        with connect(self.path) as connection:
            stored = connection.execute("SELECT token_hash FROM sessions").fetchone()[0]
            self.assertEqual(stored, session_key(token))
            self.assertNotEqual(stored, token)
        self.assertEqual(self.request("/api/v1/auth/logout", "POST", token=token)[0], 204)
        self.assertEqual(self.request("/api/v1/me", token=token)[0], 401)

    def test_unauthenticated_bad_credentials_and_impersonation(self):
        for path in ("/api/v1/me", "/api/v1/service-systems", "/api/v1/support-groups", "/api/v1/support-groups/test-it"):
            status, _, headers = self.request(path, headers={"X-User-Id": "test-dispatcher", "X-Role": "dispatcher"})
            self.assertEqual(status, 401)
            self.assertEqual(headers["WWW-Authenticate"], "Bearer")
        for username in ("submitter", "missing", "' OR 1=1 --"):
            self.assertEqual(self.request(
                "/api/v1/auth/login", "POST", {"username": username, "password": "incorrect"},
            )[0], 401)
        self.assertEqual(self.request("/api/v1/me", token="forged")[0], 401)
        token = self.login()
        status, body, _ = self.request("/api/v1/me?user_id=test-dispatcher&role=dispatcher", token=token)
        self.assertEqual((status, body["id"]), (200, "test-submitter"))
        self.assertEqual(self.request("/api/v1/support-groups", token=token)[0], 403)

    def test_validation_does_not_echo_credentials(self):
        body = {"username": "submitter", "password": TEST_PASSWORD, "roles": ["dispatcher"]}
        status, error, _ = self.request("/api/v1/auth/login", "POST", body)
        self.assertEqual(status, 422)
        self.assertNotIn(TEST_PASSWORD, json.dumps(error))
        body = {"username": "submitter", "password": "sensitive" * 40}
        status, error, _ = self.request("/api/v1/auth/login", "POST", body)
        self.assertEqual(status, 422)
        self.assertNotIn(body["password"], json.dumps(error))

    def test_role_and_group_scope_enforcement(self):
        token = self.login("dispatcher")
        status, identity, _ = self.request("/api/v1/me", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(identity["roles"], ["dispatcher", "submitter"])
        self.assertEqual(identity["group_scopes"], {"dispatcher": ["test-it"]})
        status, body, _ = self.request("/api/v1/support-groups", token=token)
        self.assertEqual(status, 200)
        self.assertEqual([group["id"] for group in body["items"]], ["test-it"])
        self.assertEqual(self.request("/api/v1/support-groups/test-it", token=token)[0], 200)
        self.assertEqual(self.request("/api/v1/support-groups/test-admin", token=token)[0], 403)
        self.assertEqual(self.request("/api/v1/support-groups/missing", token=token)[0], 403)
        for username in ("handler", "handler_other", "supervisor", "config_admin"):
            other_token = self.login(username)
            self.assertEqual(self.request("/api/v1/support-groups", token=other_token)[0], 403)

    def test_permission_and_active_changes_apply_to_existing_sessions(self):
        token = self.login("dispatcher")
        with connect(self.path) as connection, connection:
            connection.execute("DELETE FROM user_group_scopes WHERE user_id = 'test-dispatcher'")
        self.assertEqual(self.request("/api/v1/support-groups/test-it", token=token)[0], 403)
        self.assertEqual(self.request("/api/v1/support-groups", token=token)[1], {"items": []})
        with connect(self.path) as connection, connection:
            connection.execute("DELETE FROM user_roles WHERE user_id = 'test-dispatcher' AND role = 'dispatcher'")
        self.assertEqual(self.request("/api/v1/support-groups", token=token)[0], 403)
        with connect(self.path) as connection, connection:
            connection.execute("UPDATE users SET active = 0 WHERE username = 'dispatcher'")
        self.assertEqual(self.request("/api/v1/me", token=token)[0], 401)
        self.assertEqual(self.request(
            "/api/v1/auth/login", "POST", {"username": "dispatcher", "password": TEST_PASSWORD},
        )[0], 401)

    def test_expiry_and_logout_only_revoke_matching_session(self):
        first = self.login()
        second = self.login()
        self.assertNotEqual(first, second)
        self.assertEqual(self.request("/api/v1/auth/logout", "POST", token=first)[0], 204)
        self.assertEqual(self.request("/api/v1/me", token=second)[0], 200)
        with connect(self.path) as connection, connection:
            connection.execute("UPDATE sessions SET created_at = 1, expires_at = 2 WHERE token_hash = ?", (session_key(second),))
        self.assertEqual(self.request("/api/v1/me", token=second)[0], 401)

    def test_test_seed_is_atomic_and_non_destructive(self):
        token = self.login("dispatcher")
        with connect(self.path) as connection, connection:
            before = dict(connection.execute("SELECT * FROM users WHERE username = 'dispatcher'").fetchone())
            connection.execute("UPDATE users SET display_name = '保留修改', active = 0 WHERE username = 'dispatcher'")
            connection.execute("DELETE FROM user_group_scopes WHERE user_id = 'test-dispatcher'")
            connection.execute("INSERT INTO support_groups (id, name) VALUES ('custom', '保留分组')")
        self.assertEqual(seed_test_data(self.path), 0)
        with connect(self.path) as connection:
            user = connection.execute("SELECT * FROM users WHERE username = 'dispatcher'").fetchone()
            self.assertEqual(user["password_hash"], before["password_hash"])
            self.assertEqual(user["display_name"], "保留修改")
            self.assertEqual(user["active"], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM user_group_scopes WHERE user_id = 'test-dispatcher'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM support_groups WHERE id = 'custom'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions WHERE token_hash = ?", (session_key(token),)).fetchone()[0], 1)
        # An existing eight-account installation gains the administrative dispatcher.
        with connect(self.path) as connection, connection:
            connection.execute("DELETE FROM user_group_scopes WHERE user_id = 'test-dispatcher_admin'")
            connection.execute("DELETE FROM user_roles WHERE user_id = 'test-dispatcher_admin'")
            connection.execute("DELETE FROM users WHERE id = 'test-dispatcher_admin'")
        self.assertEqual(seed_test_data(self.path), 1)
        self.assertEqual(seed_test_data(self.path), 0)
        with connect(self.path) as connection:
            user = connection.execute("SELECT * FROM users WHERE username = 'dispatcher'").fetchone()
            self.assertEqual(user["password_hash"], before["password_hash"])
            self.assertEqual(user["display_name"], "保留修改")
            self.assertEqual(user["active"], 0)
        # A conflicting id must roll back all changes in a fresh seed transaction.
        conflict_path = Path(self.directory.name) / "conflict.sqlite3"
        initialize_database(conflict_path)
        with connect(conflict_path) as connection, connection:
            connection.execute(
                "INSERT INTO users (id, username, display_name, password_salt, password_hash) VALUES (?, ?, ?, ?, ?)",
                ("test-handler", "unrelated", "已有账号", b"salt", b"key"),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            seed_test_data(conflict_path)
        with connect(conflict_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM roles").fetchone()[0], 0)

    def test_restart_preserves_sessions_and_configuration(self):
        token = self.login("dispatcher")
        self.stop_server()
        self.start_server()
        self.assertEqual(self.request("/api/v1/me", token=token)[0], 200)
        status, body, _ = self.request("/api/v1/service-systems", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 2)
        with connect(self.path) as connection:
            user = connection.execute("SELECT * FROM users WHERE username = 'submitter'").fetchone()
            self.assertNotEqual(user["password_hash"], TEST_PASSWORD.encode())
            self.assertEqual(password_key(TEST_PASSWORD, user["password_salt"]), user["password_hash"])

    def test_foreign_keys_and_failed_transaction(self):
        with connect(self.path) as connection:
            with self.assertRaises(sqlite3.IntegrityError), connection:
                connection.execute("INSERT INTO roles (code, name) VALUES ('temporary', '回滚')")
                connection.execute("INSERT INTO user_roles (user_id, role) VALUES ('missing', 'temporary')")
            self.assertIsNone(connection.execute("SELECT 1 FROM roles WHERE code = 'temporary'").fetchone())

    def test_database_unavailable_returns_service_error(self):
        self.app.state.database_path = self.path.parent / "missing-parent" / "unavailable.sqlite3"
        for path, method, body in (
            ("/health", "GET", None),
            ("/api/v1/auth/login", "POST", {"username": "submitter", "password": TEST_PASSWORD}),
        ):
            status, error, _ = self.request(path, method, body)
            self.assertEqual(status, 503)
            self.assertEqual(error["detail"]["code"], "storage_unavailable")
            self.assertNotIn(str(self.path.parent), json.dumps(error))

    def test_newer_schema_is_rejected_without_rewriting(self):
        with connect(self.path) as connection, connection:
            connection.execute("PRAGMA user_version = 6")
        with self.assertRaises(RuntimeError):
            initialize_database(self.path)
        with connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 9)


if __name__ == "__main__":
    unittest.main()

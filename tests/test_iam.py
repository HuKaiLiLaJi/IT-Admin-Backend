import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from app.database import SCHEMA_VERSION, connect, initialize_database
from app.iam import CACHE_SECONDS, EmployeeProfile, IAMClient, IAMError, save_profile
from tests.test_foundation import HTTPTestCase


PASSWORD = "Personnel-Test-2026!"
PROFILE = EmployeeProfile("iam-fixture-1", "fixture001", "测试员工甲", "测试研发部",
                          "fixture@example.invalid", "桐庐", "wx-fixture-1", "dept-1", "工程师")


def payload(profile=PROFILE):
    return {"total_num": 1, "data": [{
        "id": profile.iam_user_id, "username": profile.employee_number, "status": "1",
        "display_name": profile.display_name, "region": profile.location, "email": profile.email,
        "work_title": profile.work_title,
        "sys_ext_props": {"qywx_id": profile.qywx_id, "org_name": profile.department,
                          "org_code": profile.department_code},
    }]}


class IAMTests(HTTPTestCase):
    def test_employee_passwordless_first_login_and_existing_account(self):
        self.app.state.iam_passwordless = True
        self.app.state.iam_client = Mock()
        self.app.state.iam_client.lookup.return_value = PROFILE
        self.assertTrue(self.request('/api/v1/auth/login-options')[1]['employee_passwordless'])
        status, body, _ = self.request('/api/v1/auth/login', 'POST', {'username': PROFILE.employee_number})
        self.assertEqual(status, 200)
        token = body['access_token']
        me = self.request('/api/v1/me', token=token)[1]
        self.assertEqual((me['username'], me['roles'], me['department']),
                         (PROFILE.employee_number, ['submitter'], PROFILE.department))
        self.app.state.iam_client.lookup.assert_called_once_with(username=PROFILE.employee_number)
        self.assertEqual(self.request('/api/v1/auth/login', 'POST', {'username': PROFILE.employee_number})[0], 200)
        self.app.state.iam_client.lookup.assert_called_once()
        self.app.state.iam_passwordless = False
        self.assertEqual(self.request('/api/v1/auth/login', 'POST', {'username': PROFILE.employee_number})[0], 401)
        self.app.state.iam_passwordless = True
        with connect(self.path) as connection, connection:
            connection.execute('UPDATE users SET active = 0 WHERE id = ?', (me['id'],))
        self.assertEqual(self.request('/api/v1/auth/login', 'POST', {'username': PROFILE.employee_number})[0], 401)
        self.assertEqual(self.request('/api/v1/me', token=token)[0], 401)

    def test_passwordless_failed_lookup_and_custom_accounts(self):
        self.app.state.iam_passwordless = True
        self.app.state.iam_client = Mock()
        self.app.state.iam_client.lookup.side_effect = IAMError('iam_no_match', '未返回匹配人员', 422)
        self.assertEqual(self.request('/api/v1/auth/login', 'POST', {'username': 'missing'})[0], 422)
        with connect(self.path) as connection, connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM iam_profiles').fetchone()[0], 0)
            connection.execute("""INSERT INTO users (id, username, display_name, password_salt, password_hash)
                SELECT 'custom', 'custom', 'Custom', password_salt, password_hash
                FROM users WHERE username = 'submitter'""")
        self.assertEqual(self.request('/api/v1/auth/login', 'POST', {'username': 'custom'})[0], 401)
        self.assertEqual(self.request('/api/v1/auth/login', 'POST',
                                     {'username': 'submitter', 'role': 'manager'})[0], 422)

    def provision(self, profile=PROFILE):
        with connect(self.path) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return save_profile(connection, profile, password=PASSWORD)

    def employee_login(self):
        status, body, _ = self.request("/api/v1/auth/login", "POST", {
            "username": PROFILE.employee_number, "password": PASSWORD,
        })
        self.assertEqual(status, 200)
        return body

    def client_with_payload(self, data):
        client = IAMClient("fixture-access", "fixture-secret", "https://iam.example.invalid/users")
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(json.dumps(data).encode())
        return client

    def test_lookup_filters_and_validates_identity_and_safe_errors(self):
        for query in ({"username": PROFILE.employee_number}, {"qywx_id": PROFILE.qywx_id}):
            client = self.client_with_payload(payload())
            self.assertEqual(client.lookup(**query), PROFILE)
            request = client.opener.open.call_args.args[0]
            params = parse_qs(urlsplit(request.full_url).query)
            self.assertEqual(params["status"], ["1"])
            self.assertEqual(request.method, "GET")
            self.assertIn("$v2$fixture-access$sm3$", request.get_header("X-idmesh-signature"))
            self.assertNotIn("fixture-secret", request.full_url + str(request.headers))
        cases = [({"total_num": 0, "data": []}, "iam_no_match"),
                 ({"total_num": 2, "data": payload()["data"] * 2}, "iam_ambiguous"),
                 ({"message": "error"}, "iam_invalid_response"),
                 (payload(replace(PROFILE, employee_number="wrong")), "iam_mismatched_identity")]
        inactive = payload()
        inactive["data"][0]["status"] = "0"
        cases.append((inactive, "iam_invalid_response"))
        for data, code in cases:
            with self.subTest(code=code), self.assertRaises(IAMError) as caught:
                self.client_with_payload(data).lookup(username=PROFILE.employee_number)
            self.assertEqual(caught.exception.code, code)
        for failure in (URLError("fixture-secret"), HTTPError("url", 403, "fixture-secret", {}, None)):
            client = IAMClient("fixture-access", "fixture-secret", "https://iam.example.invalid/users")
            client.opener = Mock()
            client.opener.open.side_effect = failure
            with self.assertRaises(IAMError) as caught:
                client.lookup(username=PROFILE.employee_number)
            self.assertNotIn("fixture-secret", str(caught.exception))

    def test_import_password_login_profile_and_ticket_snapshot(self):
        user_id = self.provision()
        self.assertEqual(self.request("/api/v1/auth/login", "POST", {"username": PROFILE.employee_number})[0], 401)
        token = self.employee_login()["access_token"]
        status, me, _ = self.request("/api/v1/me", token=token)
        self.assertEqual((status, me["id"], me["roles"]), (200, user_id, ["submitter"]))
        self.assertEqual(me["department"], PROFILE.department)
        self.assertFalse(me["iam_profile"]["stale"])
        self.assertNotIn(PROFILE.employee_number, str(self.request("/api/v1/auth/test-accounts")[1]))
        conversation = self.request("/api/v1/conversations", "POST", {}, token)[1]
        ticket = self.request(f"/api/v1/conversations/{conversation['id']}/ticket", "POST", {
            "service_system_id": "test-office", "title": "资料带入", "description": "独立测试库",
        }, token)[1]
        self.assertEqual((ticket["submitter_id"], ticket["department"], ticket["email"], ticket["location"]),
                         (user_id, PROFILE.department, PROFILE.email, PROFILE.location))
        self.app.state.iam_client = Mock()
        self.app.state.iam_client.lookup.return_value = replace(PROFILE, department="新部门", location="杭州")
        self.assertEqual(self.request("/api/v1/me/profile/refresh", "POST", token=token)[0], 200)
        self.assertEqual(self.request("/api/v1/me", token=token)[1]["department"], "新部门")
        previous = self.request(f"/api/v1/tickets/{ticket['id']}", token=token)[1]
        self.assertEqual((previous["department"], previous["location"]), (PROFILE.department, PROFILE.location))

    def test_self_refresh_permission_and_no_arbitrary_person_lookup(self):
        self.provision()
        token = self.employee_login()["access_token"]
        self.app.state.iam_client = Mock()
        self.app.state.iam_client.lookup.return_value = PROFILE
        self.assertEqual(self.request("/api/v1/me/profile/refresh", "POST")[0], 401)
        other = self.login("submitter_other")
        self.assertEqual(self.request("/api/v1/me/profile/refresh", "POST", token=other)[0], 409)
        self.app.state.iam_client.lookup.assert_not_called()
        self.assertEqual(self.request("/api/v1/me/profile/refresh?username=other", "POST", token=token)[0], 200)
        self.app.state.iam_client.lookup.assert_called_once_with(username=PROFILE.employee_number)
        self.app.state.iam_client = None
        self.assertEqual(self.request("/api/v1/me/profile/refresh", "POST", token=token)[0], 503)

    def test_cached_login_refresh_failure_and_identity_conflict(self):
        user_id = self.provision()
        self.app.state.iam_client = Mock()
        self.employee_login()
        self.app.state.iam_client.lookup.assert_not_called()
        with connect(self.path) as connection, connection:
            connection.execute("UPDATE iam_profiles SET refreshed_at = refreshed_at - ?", (CACHE_SECONDS,))
        self.app.state.iam_client.lookup.side_effect = IAMError("iam_unavailable", "IAM 暂时无法连接", 503)
        body = self.employee_login()
        self.assertIn("上次保存", body["profile_warning"])
        token = body["access_token"]
        self.assertTrue(self.request("/api/v1/me", token=token)[1]["iam_profile"]["stale"])
        self.assertEqual(self.request("/api/v1/me/profile/refresh", "POST", token=token)[0], 503)
        self.app.state.iam_client.lookup.side_effect = None
        self.app.state.iam_client.lookup.return_value = replace(PROFILE, iam_user_id="different-person", department="错误部门")
        self.assertEqual(self.request("/api/v1/me/profile/refresh", "POST", token=token)[0], 409)
        with connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT department FROM users WHERE id = ?", (user_id,)).fetchone()[0], PROFILE.department)

    def test_concurrent_import_and_conflict_rollback(self):
        def import_one(_):
            return self.provision()
        with ThreadPoolExecutor(max_workers=2) as executor:
            ids = list(executor.map(import_one, range(2)))
        self.assertEqual(ids[0], ids[1])
        with self.assertRaises(sqlite3.IntegrityError):
            self.provision(replace(PROFILE, iam_user_id="other-iam", employee_number="fixture002"))
        with connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM iam_profiles").fetchone()[0], 1)
            self.assertIsNone(connection.execute("SELECT id FROM users WHERE username = 'fixture002'").fetchone())

    def test_disabled_account_during_refresh_is_not_updated(self):
        user_id = self.provision()
        token = self.employee_login()["access_token"]
        def disable(**query):
            with connect(self.path) as connection, connection:
                connection.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
            return replace(PROFILE, department="不应保存")
        self.app.state.iam_client = Mock()
        self.app.state.iam_client.lookup.side_effect = disable
        self.assertEqual(self.request("/api/v1/me/profile/refresh", "POST", token=token)[0], 401)
        with connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT department FROM users WHERE id = ?", (user_id,)).fetchone()[0], PROFILE.department)

    def test_session_revocation_during_refresh_and_account_disable_during_login(self):
        user_id = self.provision()
        token = self.employee_login()["access_token"]
        def revoke(**query):
            with connect(self.path) as connection, connection:
                connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            return replace(PROFILE, department="不应保存")
        self.app.state.iam_client = Mock()
        self.app.state.iam_client.lookup.side_effect = revoke
        self.assertEqual(self.request("/api/v1/me/profile/refresh", "POST", token=token)[0], 401)
        with connect(self.path) as connection, connection:
            self.assertEqual(connection.execute("SELECT department FROM users WHERE id = ?", (user_id,)).fetchone()[0], PROFILE.department)
            connection.execute("UPDATE iam_profiles SET refreshed_at = 0")
        def disable(**query):
            with connect(self.path) as connection, connection:
                connection.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
            return PROFILE
        self.app.state.iam_client.lookup.side_effect = disable
        self.assertEqual(self.request("/api/v1/auth/login", "POST", {
            "username": PROFILE.employee_number, "password": PASSWORD,
        })[0], 401)

    def test_v6_migration_preserves_data_and_is_atomic(self):
        legacy = Path(self.directory.name) / "v6.sqlite3"
        with connect(self.path) as source, connect(legacy) as destination, destination:
            source.backup(destination)
            destination.execute("DROP TABLE iam_profiles")
            destination.execute("ALTER TABLE tickets DROP COLUMN priority")
            destination.execute("PRAGMA user_version = 6")
        broken = Path(self.directory.name) / "broken.sqlite3"
        with connect(legacy) as source, connect(broken) as destination, destination:
            source.backup(destination)
            destination.execute("CREATE VIEW iam_profiles AS SELECT 1")
        with self.assertRaises(sqlite3.OperationalError):
            initialize_database(broken)
        with connect(broken) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
        initialize_database(legacy)
        initialize_database(legacy)
        with connect(legacy) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 9)

    def test_config_requires_endpoint_and_keeps_credentials_out_of_errors(self):
        config = Path(self.directory.name) / 'iam-config.md'
        config.write_text('AccessKey: fixture-access\nSecretKey: fixture-secret\n', encoding='utf-8')
        with self.assertRaises(IAMError) as caught:
            IAMClient.from_file(config)
        self.assertEqual(caught.exception.code, 'iam_configuration')
        self.assertNotIn('fixture-secret', str(caught.exception))
        config.write_text('https://iam.example.invalid/users\nAccessKey: fixture-access\nSecretKey: fixture-secret\n',
                          encoding='utf-8')
        self.assertEqual(IAMClient.from_file(config).url, 'https://iam.example.invalid/users')

    def test_cli_uses_explicit_config_and_prompts_only_for_new_accounts(self):
        from unittest.mock import patch
        from app.iam import main
        arguments = ["iam", "import-user", "--username", PROFILE.employee_number, "--database", str(self.path)]
        client = Mock()
        client.lookup.return_value = PROFILE
        with patch("sys.argv", arguments), patch("app.iam.IAMClient.from_environment", return_value=client), \
                patch("app.iam.getpass.getpass", return_value=PASSWORD) as prompt, patch("builtins.print"):
            main()
            self.assertEqual(prompt.call_count, 2)
            main()
            self.assertEqual(prompt.call_count, 2)
        self.employee_login()

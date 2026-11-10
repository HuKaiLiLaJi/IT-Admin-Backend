import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

from app.auth import get_connection
from app.database import SCHEMA, connect, initialize_database
from tests.test_foundation import HTTPTestCase


class TicketHTTPTestCase(HTTPTestCase):
    def conversation(self, token, previous_ticket_id=None):
        body = {} if previous_ticket_id is None else {"previous_ticket_id": previous_ticket_id}
        status, result, _ = self.request("/api/v1/conversations", "POST", body, token)
        self.assertEqual(status, 201)
        return result["id"]

    def ticket(self, token, system="test-office"):
        conversation_id = self.conversation(token)
        status, result, _ = self.request(
            f"/api/v1/conversations/{conversation_id}/ticket", "POST",
            {"service_system_id": system, "title": "打印机故障", "description": "无法打印，请协助"}, token,
        )
        self.assertEqual(status, 201)
        return result


class TicketTests(TicketHTTPTestCase):
    def test_intake_base_location_is_saved_without_changing_account_or_retry(self):
        token = self.login()
        with connect(self.path) as connection, connection:
            connection.execute("UPDATE users SET location = '账号基地' WHERE username = 'submitter'")
        conversation_id = self.conversation(token)
        url = f"/api/v1/conversations/{conversation_id}/ticket"
        body = {"service_system_id": "test-office", "title": "测试标题", "description": "测试描述"}
        for invalid in ("x" * 201, 123, ["测试基地 A"]):
            self.assertEqual(self.request(url, "POST", {**body, "location": invalid}, token)[0], 422)
        status, ticket, _ = self.request(url, "POST", {**body, "location": " 测试基地 A "}, token)
        self.assertEqual(status, 201)
        self.assertEqual(ticket["location"], "测试基地 A")
        self.assertEqual(self.request("/api/v1/me", token=token)[1]["location"], "账号基地")
        status, retry, _ = self.request(url, "POST", {**body, "location": "测试基地 B"}, token)
        self.assertEqual(status, 200)
        self.assertEqual(retry, ticket)
        self.assertEqual(self.ticket(token)["location"], "账号基地")
        for location in ("", "  ", None):
            conversation_id = self.conversation(token)
            status, result, _ = self.request(f"/api/v1/conversations/{conversation_id}/ticket", "POST",
                                             {**body, "location": location}, token)
            self.assertEqual(status, 201)
            self.assertEqual(result["location"], "账号基地")

    def test_list_filters_preserve_visibility_and_pagination_totals(self):
        token = self.login()
        other = self.login("submitter_other")
        tickets = [self.ticket(token) for _ in range(2)]
        self.ticket(token, system="test-facility")
        hidden = self.ticket(other)
        with connect(self.path) as connection, connection:
            connection.execute("UPDATE tickets SET title = 'VPN 100%_故障' WHERE id = ?", (tickets[0]['id'],))
        for filters, expected in (
            ({"q": "vpn"}, [tickets[0]['id']]),
            ({"q": "%_"}, [tickets[0]['id']]),
            ({"q": tickets[1]['id']}, [tickets[1]['id']]),
            ({"q": hidden['id']}, []),
            ({"q": "' OR 1=1 --"}, []),
            ({"status": "closed"}, []),
            ({"status": "pending_assignment", "service_system_id": "test-office"}, [ticket['id'] for ticket in tickets]),
        ):
            with self.subTest(filters=filters):
                code, result, _ = self.request('/api/v1/tickets?' + urlencode(filters), token=token)
                self.assertEqual(code, 200)
                self.assertEqual(result['total'], len(expected))
                self.assertCountEqual([ticket['id'] for ticket in result['items']], expected)
        query = urlencode({"service_system_id": "test-office", "limit": 1, "offset": 1})
        code, result, _ = self.request('/api/v1/tickets?' + query, token=token)
        self.assertEqual(code, 200)
        self.assertEqual(result['total'], 2)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(self.request('/api/v1/tickets?status=invalid', token=token)[0], 422)
        self.assertEqual(self.request('/api/v1/tickets?q=' + 'x' * 201, token=token)[0], 422)

    def test_create_retry_history_and_restart(self):
        token = self.login()
        with connect(self.path) as connection, connection:
            connection.execute("UPDATE users SET email = 'local@example.test', location = '测试地点' WHERE username = 'submitter'")
        ticket = self.ticket(token)
        self.assertEqual(ticket["submitter_id"], "test-submitter")
        self.assertEqual(ticket["department"], "测试部门")
        self.assertEqual(ticket["email"], "local@example.test")
        self.assertEqual(ticket["location"], "测试地点")
        self.assertEqual(ticket["group_id"], "test-it")
        self.assertEqual(ticket["status"], "pending_assignment")
        self.assertEqual(ticket["version"], 1)
        self.assertIsNone(ticket["assignee_id"])
        self.assertIsNone(ticket["issue_id"])
        self.assertIsNone(ticket["previous_ticket_id"])
        with connect(self.path) as connection, connection:
            connection.execute("UPDATE service_systems SET active = 0 WHERE id = 'test-office'")
        status, retry, _ = self.request(
            f"/api/v1/conversations/{ticket['conversation_id']}/ticket", "POST",
            {"service_system_id": "missing", "title": "不同标题", "description": "重试不覆盖"}, token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(retry, ticket)
        self.stop_server()
        self.start_server()
        status, detail, _ = self.request(f"/api/v1/tickets/{ticket['id']}", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(len(detail["history"]), 1)
        event = detail["history"][0]
        self.assertEqual(event["action"], "created")
        self.assertEqual(event["actor_id"], "test-submitter")
        self.assertIsNone(event["from_status"])
        self.assertEqual(event["to_status"], ticket["status"])
        self.assertEqual(event["created_at"], ticket["created_at"])
        status, conversation, _ = self.request(f"/api/v1/conversations/{ticket['conversation_id']}", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(conversation["ticket_id"], ticket["id"])

    def test_validation_and_ownership(self):
        token = self.login()
        other = self.login("submitter_other")
        conversation_id = self.conversation(token)
        url = f"/api/v1/conversations/{conversation_id}/ticket"
        valid = {"service_system_id": "test-office", "title": "有效标题", "description": "有效描述"}
        for key in valid:
            invalid = dict(valid)
            del invalid[key]
            self.assertEqual(self.request(url, "POST", invalid, token)[0], 422)
            invalid[key] = " \n\t "
            self.assertEqual(self.request(url, "POST", invalid, token)[0], 422)
        for key, value in (("title", "x" * 201), ("description", "x" * 10001),
                           ("submitter_id", "test-submitter_other"), ("status", "closed"),
                           ("group_id", "test-admin"), ("assignee_id", "test-handler"), ("issue_id", "fake")):
            self.assertEqual(self.request(url, "POST", {**valid, key: value}, token)[0], 422)
        self.assertEqual(self.request(url, "POST", valid, other)[0], 404)
        self.assertEqual(self.request(f"/api/v1/conversations/{conversation_id}", token=other)[0], 404)
        self.assertEqual(self.request("/api/v1/conversations", "POST", {"submitter_id": "forged"}, token)[0], 422)
        self.assertEqual(self.request("/api/v1/conversations/missing/ticket", "POST", valid, token)[0], 404)
        for body in ({**valid, "service_system_id": "missing"},):
            self.assertEqual(self.request(url, "POST", body, token)[0], 422)
        with connect(self.path) as connection, connection:
            connection.execute("UPDATE support_groups SET active = 0 WHERE id = 'test-it'")
        self.assertEqual(self.request(url, "POST", valid, token)[0], 422)
        with connect(self.path) as connection, connection:
            connection.execute("UPDATE support_groups SET active = 1 WHERE id = 'test-it'")
            connection.execute("UPDATE service_systems SET active = 0 WHERE id = 'test-office'")
        self.assertEqual(self.request(url, "POST", valid, token)[0], 422)
        with connect(self.path) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ticket_history").fetchone()[0], 0)
            connection.execute("UPDATE service_systems SET active = 1 WHERE id = 'test-office'")
        status, result, _ = self.request(url, "POST", {**valid, "title": " 标题 ", "description": " 描述 "}, token)
        self.assertEqual(status, 201)
        self.assertEqual((result["title"], result["description"]), ("标题", "描述"))

    def test_intake_rechecks_identity_and_role_after_obtaining_write_lock(self):
        for endpoint in ('conversation', 'ticket'):
            for revocation, expected in (('account', 401), ('role', 403), ('session', 401)):
                with self.subTest(endpoint=endpoint, revocation=revocation):
                    with connect(self.path) as connection, connection:
                        connection.execute("UPDATE users SET active = 1 WHERE id = 'test-submitter'")
                        connection.execute("INSERT OR IGNORE INTO user_roles VALUES ('test-submitter', 'submitter')")
                    token = self.login()
                    conversation = self.conversation(token)
                    reached = threading.Event()
                    resume = threading.Event()

                    def paused_connection():
                        with connect(self.path) as connection:
                            def pause(statement):
                                if statement == 'BEGIN IMMEDIATE':
                                    reached.set()
                                    resume.wait(timeout=15)
                            connection.set_trace_callback(pause)
                            yield connection

                    self.app.dependency_overrides[get_connection] = paused_connection
                    try:
                        url = '/api/v1/conversations'
                        body = {}
                        if endpoint == 'ticket':
                            url += f'/{conversation}/ticket'
                            body = {'service_system_id': 'test-office', 'title': 't', 'description': 'd'}
                        with ThreadPoolExecutor(max_workers=1) as executor:
                            pending = executor.submit(self.request, url, 'POST', body, token)
                            try:
                                self.assertTrue(reached.wait(timeout=3))
                                with connect(self.path) as connection, connection:
                                    before = tuple(connection.execute(
                                        'SELECT (SELECT COUNT(*) FROM conversations), (SELECT COUNT(*) FROM tickets), '
                                        '(SELECT COUNT(*) FROM ticket_history)').fetchone())
                                    if revocation == 'account':
                                        connection.execute("UPDATE users SET active = 0 WHERE id = 'test-submitter'")
                                    elif revocation == 'role':
                                        connection.execute("DELETE FROM user_roles WHERE user_id = 'test-submitter'")
                                    else:
                                        connection.execute("DELETE FROM sessions WHERE user_id = 'test-submitter'")
                            finally:
                                resume.set()
                            self.assertEqual(pending.result()[0], expected)
                        with connect(self.path) as connection:
                            after = tuple(connection.execute(
                                'SELECT (SELECT COUNT(*) FROM conversations), (SELECT COUNT(*) FROM tickets), '
                                '(SELECT COUNT(*) FROM ticket_history)').fetchone())
                        self.assertEqual(after, before)
                    finally:
                        resume.set()
                        self.app.dependency_overrides.clear()

    def test_all_entry_points_require_authentication(self):
        for path, method, body in (
            ("/api/v1/conversations", "POST", {}),
            ("/api/v1/conversations/missing", "GET", None),
            ("/api/v1/conversations/missing/ticket", "POST", {"service_system_id": "test-office", "title": "t", "description": "d"}),
            ("/api/v1/tickets", "GET", None), ("/api/v1/tickets/missing", "GET", None),
        ):
            self.assertEqual(self.request(path, method, body, headers={"X-User-Id": "test-dispatcher"})[0], 401)
        token = self.login()
        with connect(self.path) as connection, connection:
            connection.execute("DELETE FROM user_roles WHERE user_id = 'test-submitter'")
        self.assertEqual(self.request("/api/v1/conversations", "POST", {}, token)[0], 403)

    def test_scope_filtered_lists_totals_details_and_reassignment(self):
        tokens = {name: self.login(name) for name in (
            "submitter", "submitter_other", "dispatcher", "handler", "handler_other", "supervisor", "config_admin",
        )}
        first = self.ticket(tokens["submitter"])
        second = self.ticket(tokens["submitter_other"], "test-facility")
        third = self.ticket(tokens["dispatcher"])

        def assert_visible(name, expected):
            token = tokens[name]
            status, result, _ = self.request("/api/v1/tickets?limit=1", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(result["total"], len(expected))
            self.assertEqual(len(result["items"]), min(1, len(expected)))
            ids = []
            for offset in range(len(expected)):
                page = self.request(f"/api/v1/tickets?limit=1&offset={offset}", token=token)[1]
                ids.extend(item["id"] for item in page["items"])
            self.assertEqual(set(ids), set(expected))
            for ticket in (first, second, third):
                status, _, _ = self.request(f"/api/v1/tickets/{ticket['id']}", token=token)
                self.assertEqual(status, 200 if ticket["id"] in expected else 404)

        assert_visible("submitter", [first["id"]])
        assert_visible("submitter_other", [second["id"]])
        assert_visible("dispatcher", [first["id"], third["id"]])
        for name in ("handler", "handler_other", "supervisor", "config_admin"):
            assert_visible(name, [])
        # Assignment is a T03 fixture. No state-changing API is introduced in T02.
        with connect(self.path) as connection, connection:
            connection.execute("UPDATE tickets SET assignee_id = 'test-handler' WHERE id = ?", (first["id"],))
        assert_visible("handler", [first["id"]])
        with connect(self.path) as connection, connection:
            connection.execute("UPDATE tickets SET assignee_id = 'test-handler_other' WHERE id = ?", (first["id"],))
            connection.execute("DELETE FROM user_group_scopes WHERE user_id = 'test-dispatcher'")
        assert_visible("handler", [])
        assert_visible("handler_other", [first["id"]])
        assert_visible("dispatcher", [third["id"]])
        missing = self.request("/api/v1/tickets/missing", token=tokens["submitter"])
        hidden = self.request(f"/api/v1/tickets/{second['id']}", token=tokens["submitter"])
        self.assertEqual(missing[:2], hidden[:2])
        for query in ("limit=0", "limit=101", "offset=-1"):
            self.assertEqual(self.request("/api/v1/tickets?" + query, token=tokens["submitter"])[0], 422)

    def test_terminal_ticket_links_use_new_conversation_and_new_ticket(self):
        token = self.login()
        other = self.login("submitter_other")
        old = self.ticket(token)
        self.assertEqual(self.request("/api/v1/conversations", "POST", {"previous_ticket_id": old["id"]}, token)[0], 409)
        for status in ("closed", "cancelled"):
            # Terminal state is a T04 fixture, to be covered through real actions in T06.
            with connect(self.path) as connection, connection:
                connection.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, old["id"]))
            self.assertEqual(self.request("/api/v1/conversations", "POST", {"previous_ticket_id": old["id"]}, other)[0], 404)
            new_conversation = self.conversation(token, old["id"])
            code, new, _ = self.request(
                f"/api/v1/conversations/{new_conversation}/ticket", "POST",
                {"service_system_id": "test-office", "title": "再次报障", "description": "问题再次出现"}, token,
            )
            self.assertEqual(code, 201)
            self.assertNotEqual(new["id"], old["id"])
            self.assertNotEqual(new["conversation_id"], old["conversation_id"])
            self.assertEqual(new["previous_ticket_id"], old["id"])
            code, retry, _ = self.request(
                f"/api/v1/conversations/{old['conversation_id']}/ticket", "POST",
                {"service_system_id": "test-office", "title": "重复", "description": "重复"}, token,
            )
            self.assertEqual(code, 200)
            self.assertEqual(retry["id"], old["id"])
        self.assertEqual(self.request("/api/v1/conversations", "POST", {"previous_ticket_id": "missing"}, token)[0], 404)

    def test_concurrent_submissions_create_one_ticket_and_one_history(self):
        token = self.login()
        conversation_id = self.conversation(token)
        barrier = threading.Barrier(8)

        def submit(index):
            barrier.wait(timeout=15)
            return self.request(
                f"/api/v1/conversations/{conversation_id}/ticket", "POST",
                {"service_system_id": "test-office", "title": f"故障{index}", "description": "并发提交"}, token,
            )[:2]

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(submit, range(8)))
        self.assertEqual(sum(status == 201 for status, _ in results), 1)
        self.assertEqual(sum(status == 200 for status, _ in results), 7)
        self.assertEqual(len({body["id"] for _, body in results}), 1)
        self.assertTrue(all(body == results[0][1] for _, body in results))
        with connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ticket_history").fetchone()[0], 1)

    def test_history_failure_rolls_back_ticket_and_allows_retry(self):
        token = self.login()
        conversation_id = self.conversation(token)
        with connect(self.path) as connection, connection:
            connection.execute("""CREATE TRIGGER fail_history BEFORE INSERT ON ticket_history
                                  BEGIN SELECT unavailable_test_function(); END""")
        body = {"service_system_id": "test-office", "title": "失败测试", "description": "初始历史失败"}
        url = f"/api/v1/conversations/{conversation_id}/ticket"
        self.assertEqual(self.request(url, "POST", body, token)[0], 503)
        with connect(self.path) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ticket_history").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)
            connection.execute("DROP TRIGGER fail_history")
        self.assertEqual(self.request(url, "POST", body, token)[0], 201)

    def test_v1_migration_preserves_existing_data_and_rolls_back_failure(self):
        legacy = Path(self.directory.name) / "legacy.sqlite3"
        with connect(legacy) as connection, connection:
            for statement in SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 1")
            connection.execute("INSERT INTO users (id, username, display_name, password_salt, password_hash) VALUES ('old', 'old', '已有账号', X'01', X'02')")
            connection.execute("INSERT INTO roles (code, name) VALUES ('submitter', '提交人')")
            connection.execute("INSERT INTO user_roles VALUES ('old', 'submitter')")
            connection.execute("INSERT INTO support_groups (id, name) VALUES ('old-group', '已有组')")
            connection.execute("INSERT INTO user_group_scopes VALUES ('old', 'submitter', 'old-group')")
            connection.execute("INSERT INTO service_systems (id, name, group_id) VALUES ('old-system', '已有系统', 'old-group')")
            connection.execute("INSERT INTO sessions VALUES ('existing-session', 'old', 1, 2000000000)")
        initialize_database(legacy)
        initialize_database(legacy)
        with connect(legacy) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(connection.execute("SELECT display_name, password_salt, password_hash FROM users").fetchone()[:], ("已有账号", b'\x01', b'\x02'))
            for table in ("roles", "user_roles", "support_groups", "user_group_scopes", "service_systems", "sessions"):
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 0)
        broken = Path(self.directory.name) / "conflict.sqlite3"
        with connect(broken) as connection, connection:
            for statement in SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 1")
            connection.execute("CREATE TABLE tickets (id TEXT)")
        with self.assertRaises(sqlite3.OperationalError):
            initialize_database(broken)
        with connect(broken) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name = 'conversations'").fetchone())

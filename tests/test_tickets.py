import sqlite3
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch

from app.auth import get_connection
from app.database import SCHEMA, SCHEMA_VERSION, connect, initialize_database
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
    def test_legacy_number_format_daily_reset_and_restart(self):
        token = self.login()
        before_midnight = int(datetime.fromisoformat('2026-11-11T23:59:59+08:00').timestamp())
        with patch('app.tickets.time') as clock:
            clock.time.return_value = before_midnight
            first = self.ticket(token)
            second = self.ticket(token, system='test-facility')
            self.assertEqual(first['id'], 'IT202611110001')
            self.assertEqual(second['id'], 'IT202611110002')
            self.assertEqual(second['ticket_id'], second['id'])
            self.stop_server()
            self.start_server()
            self.assertEqual(self.ticket(token)['id'], 'IT202611110003')
            clock.time.return_value = before_midnight + 1
            next_day = self.ticket(token)
            self.assertEqual(next_day['id'], 'IT202611120001')
            retry = self.request(f"/api/v1/conversations/{first['conversation_id']}/ticket", 'POST', {
                'service_system_id': 'test-office', 'title': '重试', 'description': '跨天重试',
            }, token)
            self.assertEqual(retry[:2], (200, first))
        result = self.request('/api/v1/tickets?' + urlencode({'q': first['id']}), token=token)[1]
        self.assertEqual([item['id'] for item in result['items']], [first['id']])

    def test_existing_uuid_ticket_remains_accessible_and_linkable(self):
        token = self.login()
        ticket = self.ticket(token)
        legacy_id = '2af77124-f187-4cca-abef-23b5ab5a12e4'
        with connect(self.path) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('PRAGMA defer_foreign_keys = ON')
            connection.execute('UPDATE tickets SET id = ? WHERE id = ?', (legacy_id, ticket['id']))
            connection.execute('UPDATE ticket_history SET ticket_id = ? WHERE ticket_id = ?',
                               (legacy_id, ticket['id']))
        url = f'/api/v1/tickets/{legacy_id}'
        self.assertEqual(self.request(url, token=token)[1]['id'], legacy_id)
        self.assertEqual(self.request('/api/v1/tickets?q=' + legacy_id, token=token)[1]['total'], 1)
        retry = self.request(f"/api/v1/conversations/{ticket['conversation_id']}/ticket", 'POST', {
            'service_system_id': 'test-office', 'title': '重试', 'description': '保留旧编号',
        }, token)
        self.assertEqual((retry[0], retry[1]['id']), (200, legacy_id))
        cancelled = self.request(url + '/cancel', 'POST', {
            'expected_version': 1, 'reason': '旧编号兼容验证',
        }, token)
        self.assertEqual(cancelled[0], 200, cancelled)
        conversation = self.conversation(token, legacy_id)
        self.assertEqual(self.request(f'/api/v1/conversations/{conversation}', token=token)[1]['previous_ticket_id'], legacy_id)
        self.assertRegex(self.ticket(token)['id'], r'^IT\d{12}$')

    def test_concurrent_distinct_conversations_allocate_unique_numbers(self):
        token = self.login()
        conversations = [self.conversation(token) for _ in range(8)]
        barrier = threading.Barrier(8)

        def submit(conversation):
            barrier.wait(timeout=15)
            return self.request(f'/api/v1/conversations/{conversation}/ticket', 'POST', {
                'service_system_id': 'test-office', 'title': '并发编号', 'description': '独立会话',
            }, token)

        with patch('app.tickets.time') as clock:
            clock.time.return_value = int(datetime.fromisoformat('2026-11-11T12:00:00+08:00').timestamp())
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(submit, conversations))
        self.assertTrue(all(result[0] == 201 for result in results), results)
        self.assertEqual(sorted(result[1]['id'] for result in results),
                         [f'IT20261111{number:04d}' for number in range(1, 9)])

    def test_daily_number_exhaustion_returns_conflict_without_partial_ticket(self):
        token = self.login()
        with patch('app.tickets.time') as clock:
            clock.time.return_value = int(datetime.fromisoformat('2026-11-11T12:00:00+08:00').timestamp())
            ticket = self.ticket(token)
            with connect(self.path) as connection, connection:
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('PRAGMA defer_foreign_keys = ON')
                connection.execute("UPDATE tickets SET id = 'IT202611119999' WHERE id = ?", (ticket['id'],))
                connection.execute("UPDATE ticket_history SET ticket_id = 'IT202611119999' WHERE ticket_id = ?",
                                   (ticket['id'],))
            conversation = self.conversation(token)
            response = self.request(f'/api/v1/conversations/{conversation}/ticket', 'POST', {
                'service_system_id': 'test-office', 'title': '容量检查', 'description': '编号已用尽',
            }, token)
        self.assertEqual(response[0], 409)
        self.assertEqual(response[1]['detail']['code'], 'ticket_number_exhausted')
        with connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM tickets').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM ticket_history').fetchone()[0], 1)

    def test_priority_options_roundtrip_validation_and_retry(self):
        token = self.login()
        body = {'service_system_id': 'test-office', 'title': '优先级选项', 'description': '原文'}
        for priority in (None, 'P1', 'P3'):
            conversation = self.conversation(token)
            path = f'/api/v1/conversations/{conversation}/ticket'
            status, ticket, _ = self.request(path, 'POST', {**body, 'priority': priority}, token)
            self.assertEqual(status, 201, ticket)
            self.assertEqual(ticket['priority'], priority)
            self.assertEqual(ticket['status'], 'pending_assignment')
            self.assertIsNone(ticket['assignee_id'])
            retry = self.request(path, 'POST', {**body, 'priority': 'P3'}, token)
            self.assertEqual(retry[:2], (200, ticket))
            detail = self.request(f"/api/v1/tickets/{ticket['id']}", token=token)[1]
            self.assertEqual(detail['priority'], priority)
            updated = self.request(f"/api/v1/tickets/{ticket['id']}/supplements", 'POST', {
                'expected_version': 1, 'description': '员工补充',
            }, token)[1]
            self.assertEqual(updated['priority'], priority)
        self.assertIsNone(self.ticket(token)['priority'])
        items = self.request('/api/v1/tickets?scope=mine', token=token)[1]['items']
        self.assertCountEqual([item['priority'] for item in items], [None, 'P1', 'P3', None])
        path = f'/api/v1/conversations/{self.conversation(token)}/ticket'
        for invalid in ('P2', '', 'p1', 1, True):
            self.assertEqual(self.request(path, 'POST', {**body, 'priority': invalid}, token)[0], 422)

    def test_v7_priority_migration_preserves_ticket_and_checks_values(self):
        token = self.login()
        ticket = self.ticket(token)
        legacy = Path(self.directory.name) / 'v7.sqlite3'
        with connect(self.path) as source, connect(legacy) as destination, destination:
            source.backup(destination)
            destination.execute('ALTER TABLE tickets DROP COLUMN priority')
            destination.execute('PRAGMA user_version = 7')
            original = dict(destination.execute('SELECT * FROM tickets').fetchone())
        initialize_database(legacy)
        initialize_database(legacy)
        with connect(legacy) as connection, connection:
            migrated = dict(connection.execute('SELECT * FROM tickets').fetchone())
            self.assertIsNone(migrated.pop('priority'))
            self.assertEqual(migrated, original)
            self.assertEqual(migrated['id'], ticket['id'])
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE tickets SET priority = 'P2'")

    def test_mine_scope_restricts_multirole_list_detail_and_pagination(self):
        token = self.login('dispatcher')
        other = self.login()
        own = [self.ticket(token) for _ in range(2)]
        visible_other = self.ticket(other)
        self.assertEqual(self.request('/api/v1/tickets', token=token)[1]['total'], 3)
        status, result, _ = self.request('/api/v1/tickets?scope=mine&limit=1&offset=1', token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result['total'], 2)
        self.assertEqual(len(result['items']), 1)
        self.assertIn(result['items'][0]['id'], [item['id'] for item in own])
        detail_url = f"/api/v1/tickets/{visible_other['id']}"
        self.assertEqual(self.request(detail_url, token=token)[0], 200)
        self.assertEqual(self.request(detail_url + '?scope=mine', token=token)[0], 404)
        query = urlencode({'scope': 'mine', 'q': visible_other['id']})
        self.assertEqual(self.request('/api/v1/tickets?' + query, token=token)[1]['total'], 0)
        self.assertEqual(self.request(f"/api/v1/tickets/{own[0]['id']}?scope=mine", token=token)[0], 200)
        for url in ('/api/v1/tickets', detail_url):
            self.assertEqual(self.request(url + '?scope=mine')[0], 401)
            self.assertEqual(self.request(url + '?scope=invalid', token=token)[0], 422)
        with connect(self.path) as connection, connection:
            connection.execute("DELETE FROM user_roles WHERE user_id = 'test-dispatcher' AND role = 'submitter'")
        self.assertEqual(self.request('/api/v1/tickets?scope=mine', token=token)[0], 403)

    def test_description_and_system_search_preserves_scope_and_totals(self):
        token = self.login()
        other = self.login('submitter_other')
        own = [self.ticket(token) for _ in range(2)]
        hidden = self.ticket(other)
        with connect(self.path) as connection, connection:
            connection.execute("UPDATE service_systems SET name = 'PLM测试系统' WHERE id = 'test-office'")
            connection.execute("UPDATE tickets SET description = '仅描述包含 ConnectionLost' WHERE id IN (?, ?)",
                               (own[0]['id'], hidden['id']))
        query = urlencode({'q': 'connectionlost', 'scope': 'mine'})
        result = self.request('/api/v1/tickets?' + query, token=token)[1]
        self.assertEqual([item['id'] for item in result['items']], [own[0]['id']])
        query = urlencode({'q': 'plm', 'scope': 'mine', 'limit': 1, 'offset': 1})
        result = self.request('/api/v1/tickets?' + query, token=token)[1]
        self.assertEqual((result['total'], len(result['items'])), (2, 1))
        self.assertNotEqual(result['items'][0]['id'], hidden['id'])

    def test_response_aliases_times_and_optional_engineer_mapping(self):
        token = self.login()
        ticket = self.ticket(token)
        self.assertEqual(ticket['ticket_id'], ticket['id'])
        self.assertIsNone(ticket['engineer_employee_no'])
        for field in ('created_at', 'updated_at'):
            self.assertIsInstance(ticket[field], int)
            self.assertTrue(ticket[field + '_iso'].endswith('+08:00'))
            self.assertEqual(int(datetime.fromisoformat(ticket[field + '_iso']).timestamp()), ticket[field])
        with connect(self.path) as connection, connection:
            connection.execute("""INSERT INTO iam_profiles
                (user_id, iam_user_id, employee_number, refreshed_at) VALUES (?, ?, ?, ?)""",
                ('test-handler', 'iam-handler-fixture', '000123', 0))
        dispatcher = self.login('dispatcher')
        url = f"/api/v1/tickets/{ticket['id']}"
        code, assigned, _ = self.request(url + '/assignments', 'POST',
                                         {'expected_version': ticket['version'], 'assignee_id': 'test-handler'}, dispatcher)
        self.assertEqual(code, 200)
        self.assertEqual(assigned['engineer_employee_no'], '000123')
        detail = self.request(url, token=token)[1]
        listed = self.request('/api/v1/tickets?scope=mine', token=token)[1]['items'][0]
        for item in (assigned, detail, listed):
            self.assertEqual(item['ticket_id'], ticket['id'])
            self.assertEqual(item['engineer_employee_no'], '000123')
            self.assertEqual(item['system'], '测试办公系统')
        code, reassigned, _ = self.request(url + '/reassignments', 'POST',
                                           {'expected_version': assigned['version'], 'assignee_id': 'test-handler_backup',
                                            'reason': '改派到未关联IAM账号'}, dispatcher)
        self.assertEqual(code, 200)
        self.assertIsNone(reassigned['engineer_employee_no'])

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
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
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

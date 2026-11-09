import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.database import SCHEMA, TICKET_SCHEMA, connect, initialize_database
from tests.test_tickets import TicketHTTPTestCase


class TicketResolutionTests(TicketHTTPTestCase):
    def setUp(self):
        super().setUp()
        self.submitter = self.login()
        self.dispatcher = self.login('dispatcher')
        self.handler = self.login('handler')

    def act(self, ticket, action, token, **fields):
        return self.request(f"/api/v1/tickets/{ticket['id']}/{action}", 'POST',
                            {'expected_version': ticket['version'], **fields}, token)

    def success(self, ticket, action, token, **fields):
        status, updated, _ = self.act(ticket, action, token, **fields)
        self.assertEqual(status, 200, updated)
        return updated

    def active_ticket(self):
        ticket = self.ticket(self.submitter)
        ticket = self.success(ticket, 'assignments', self.dispatcher, assignee_id='test-handler')
        return self.success(ticket, 'accept', self.handler)

    def detail(self, ticket):
        status, detail, _ = self.request(f"/api/v1/tickets/{ticket['id']}", token=self.submitter)
        self.assertEqual(status, 200)
        return detail

    def pending_request(self, ticket):
        ticket = self.success(ticket, 'cancellation-requests', self.submitter, reason='无需继续')
        request_id = self.detail(ticket)['cancellation_requests'][-1]['id']
        return ticket, f'cancellation-requests/{request_id}/decision'

    def test_result_rounds_confirmation_close_and_new_ticket(self):
        ticket = self.active_ticket()
        ticket = self.success(ticket, 'results', self.handler, result=' 第一次修复 ')
        first = ticket
        self.assertEqual(ticket['result_round'], 1)
        self.assertIsNone(ticket['closed_at'])
        self.stop_server()
        self.start_server()
        self.assertEqual(self.detail(ticket)['status'], 'awaiting_confirmation')
        ticket = self.success(ticket, 'confirmations', self.submitter, result_round=1, outcome='unresolved')
        self.assertEqual(ticket['status'], 'in_progress')
        self.assertIsNone(ticket['closed_at'])
        ticket = self.success(ticket, 'results', self.handler, result='第二次修复')
        self.assertIsNone(ticket['confirmed_at'])
        self.assertEqual(self.act(first, 'confirmations', self.submitter, result_round=1, outcome='resolved')[0], 409)
        stale_round = self.act(ticket, 'confirmations', self.submitter, result_round=1, outcome='resolved')
        self.assertEqual(stale_round[1]['detail']['code'], 'result_round_conflict')
        awaiting = ticket
        ticket = self.success(ticket, 'confirmations', self.submitter, result_round=2, outcome='resolved')
        self.assertEqual(ticket['status'], 'closed')
        self.assertEqual(ticket['closed_at'], ticket['confirmed_at'])
        self.assertIsNone(ticket['cancelled_at'])
        self.assertEqual(self.act(awaiting, 'confirmations', self.submitter, result_round=2, outcome='unresolved')[0], 409)
        detail = self.detail(ticket)
        self.assertEqual([r['confirmation'] for r in detail['results']], ['unresolved', 'resolved'])
        self.assertEqual(detail['results'][0]['content'], '第一次修复')
        self.assertEqual(detail['results'][1]['confirmed_at'], ticket['closed_at'])
        events = [e for e in detail['history'] if e['result_round'] is not None]
        self.assertEqual([e['result_round'] for e in events], [1, 1, 2, 2])
        conversation = self.conversation(self.submitter, ticket['id'])
        status, new, _ = self.request(f'/api/v1/conversations/{conversation}/ticket', 'POST',
                                     {'service_system_id': 'test-office', 'title': '再次报障', 'description': '新问题'}, self.submitter)
        self.assertEqual(status, 201)
        self.assertEqual(new['previous_ticket_id'], ticket['id'])

    def test_direct_cancel_and_application_decisions(self):
        for assigned in (False, True):
            ticket = self.ticket(self.submitter)
            if assigned:
                ticket = self.success(ticket, 'assignments', self.dispatcher, assignee_id='test-handler')
            ticket = self.success(ticket, 'cancel', self.submitter, reason=' 填错了 ')
            self.assertEqual(ticket['status'], 'cancelled')
            self.assertIsInstance(ticket['cancelled_at'], int)
            self.assertIsNone(ticket['closed_at'])
            self.assertEqual(self.detail(ticket)['history'][-1]['note'], '填错了')
            self.conversation(self.submitter, ticket['id'])
        ticket = self.active_ticket()
        self.assertEqual(self.act(ticket, 'cancel', self.submitter, reason='直接撤销')[0], 409)
        ticket, decision = self.pending_request(ticket)
        self.assertEqual(ticket['status'], 'in_progress')
        self.assertEqual(self.act(ticket, 'cancellation-requests', self.submitter, reason='重复')[0], 409)
        self.assertEqual(self.act(ticket, decision, self.dispatcher, decision='approved', reason='越权')[0], 403)
        self.assertEqual(self.act(ticket, decision, self.submitter, decision='approved', reason='越权')[0], 403)
        ticket = self.success(ticket, decision, self.handler, decision='rejected', reason='需要继续处理')
        self.assertEqual(ticket['status'], 'in_progress')
        self.assertEqual(self.act(ticket, decision, self.handler, decision='approved', reason='重复')[0], 409)
        ticket, decision = self.pending_request(ticket)
        ticket = self.success(ticket, 'results', self.handler, result='已处理')
        ticket = self.success(ticket, decision, self.handler, decision='approved', reason='同意撤销')
        self.assertEqual(ticket['status'], 'cancelled')
        self.assertIsNone(ticket['closed_at'])
        self.assertEqual([r['status'] for r in self.detail(ticket)['cancellation_requests']], ['rejected', 'approved'])

    def test_current_handler_after_reassignment_and_revocation(self):
        backup = self.login('handler_backup')
        ticket, decision = self.pending_request(self.active_ticket())
        ticket = self.success(ticket, 'reassignments', self.dispatcher,
                              assignee_id='test-handler_backup', reason='换人处理')
        self.assertEqual(self.act(ticket, decision, self.handler, decision='approved', reason='旧处理人')[0], 404)
        with connect(self.path) as connection, connection:
            connection.execute("DELETE FROM user_group_scopes WHERE user_id = 'test-handler_backup'")
            connection.execute("DELETE FROM user_roles WHERE user_id = 'test-handler_backup' AND role = 'handler'")
        self.assertEqual(self.act(ticket, decision, backup, decision='approved', reason='权限收回')[0], 404)
        with connect(self.path) as connection, connection:
            connection.execute("INSERT INTO user_roles VALUES ('test-handler_backup', 'handler')")
        ticket = self.success(ticket, decision, backup, decision='approved', reason='当前处理人同意')
        self.assertEqual(ticket['status'], 'cancelled')

    def test_reassignment_preserves_cancellation_approval_requirement(self):
        backup = self.login('handler_backup')
        for started in (False, True):
            ticket = self.ticket(self.submitter)
            ticket = self.success(ticket, 'assignments', self.dispatcher, assignee_id='test-handler')
            if started:
                ticket = self.success(ticket, 'accept', self.handler)
            ticket = self.success(ticket, 'reassignments', self.dispatcher,
                                  assignee_id='test-handler_backup', reason='换人')
            self.assertEqual(bool(ticket['cancellation_requires_approval']), started)
            self.assertIsNone(ticket['accepted_at'])
            self.stop_server()
            self.start_server()
            if started:
                response = self.act(ticket, 'cancel', self.submitter, reason='直接撤销')
                self.assertEqual(response[0], 409)
                self.assertEqual(response[1]['detail']['code'], 'cancellation_approval_required')
                ticket, decision = self.pending_request(ticket)
                self.assertEqual(self.act(ticket, decision, self.handler,
                                          decision='approved', reason='原处理人')[0], 404)
                ticket = self.success(ticket, decision, backup, decision='approved', reason='同意')
                self.assertEqual(self.detail(ticket)['cancellation_requests'][0]['decided_by'],
                                 'test-handler_backup')
            else:
                self.assertEqual(self.act(ticket, 'cancellation-requests', self.submitter, reason='撤销')[0], 409)
                ticket = self.success(ticket, 'cancel', self.submitter, reason='无需处理')
            self.assertEqual(ticket['status'], 'cancelled')

    def test_administrative_ticket_can_be_assigned_and_completed(self):
        dispatcher = self.login('dispatcher_admin')
        handler = self.login('handler_other')
        ticket = self.ticket(self.submitter, system='test-facility')
        self.assertEqual(self.act(ticket, 'assignments', self.dispatcher,
                                  assignee_id='test-handler_other')[0], 404)
        ticket = self.success(ticket, 'assignments', dispatcher, assignee_id='test-handler_other')
        ticket = self.success(ticket, 'accept', handler)
        ticket = self.success(ticket, 'results', handler, result='设施已修复')
        ticket = self.success(ticket, 'confirmations', self.submitter, result_round=1, outcome='resolved')
        self.assertEqual(ticket['status'], 'closed')

    def test_permissions_fields_and_terminal_states(self):
        ticket = self.active_ticket()
        actions = [('results', {'result': '完成'}), ('confirmations', {'result_round': 1, 'outcome': 'resolved'}),
                   ('cancel', {'reason': '原因'}), ('cancellation-requests', {'reason': '原因'}),
                   ('cancellation-requests/missing/decision', {'decision': 'approved', 'reason': '原因'})]
        for action, fields in actions:
            self.assertEqual(self.act(ticket, action, None, **fields)[0], 401)
        self.assertEqual(self.act(ticket, 'results', self.submitter, result='越权')[0], 403)
        for action in ('cancel', 'cancellation-requests'):
            self.assertEqual(self.act(ticket, action, self.handler, reason='越权')[0], 403)
        for value in (' ', 'x' * 10001):
            self.assertEqual(self.act(ticket, 'results', self.handler, result=value)[0], 422)
            self.assertEqual(self.act(ticket, 'cancellation-requests', self.submitter, reason=value)[0], 422)
        self.assertEqual(self.act(ticket, 'results', self.handler, result='完成', status='closed')[0], 422)
        self.assertEqual(self.act(ticket, 'results', self.handler, result='完成', expected_version=True)[0], 422)
        ticket = self.success(ticket, 'results', self.handler, result='完成')
        other = self.login('submitter_other')
        self.assertEqual(self.act(ticket, 'confirmations', other, result_round=1, outcome='resolved')[0], 404)
        self.assertEqual(self.act(ticket, 'confirmations', self.handler, result_round=1, outcome='resolved')[0], 403)
        self.assertEqual(self.act(ticket, 'confirmations', self.submitter, result_round=True, outcome='resolved')[0], 422)
        ticket, decision = self.pending_request(ticket)
        ticket = self.success(ticket, 'confirmations', self.submitter, result_round=1, outcome='resolved')
        self.assertEqual(self.detail(ticket)['cancellation_requests'][0]['status'], 'superseded')
        for action, fields in actions:
            token = self.handler if action == 'results' or action.endswith('decision') else self.submitter
            self.assertEqual(self.act(ticket, action, token, **fields)[0], 409)
        self.assertEqual(self.act(ticket, decision, self.handler, decision='approved', reason='已关闭')[0], 409)

    def test_concurrent_confirmation_and_approval(self):
        for scenario in ('confirmations', 'mixed', 'requests'):
            ticket = self.active_ticket()
            decision = None
            if scenario != 'requests':
                ticket = self.success(ticket, 'results', self.handler, result='完成')
            if scenario == 'mixed':
                ticket, decision = self.pending_request(ticket)
            barrier = threading.Barrier(2)

            def perform(index):
                barrier.wait(timeout=10)
                if scenario == 'requests':
                    return self.act(ticket, 'cancellation-requests', self.submitter, reason='并发申请')
                if scenario == 'mixed' and index == 1:
                    return self.act(ticket, decision, self.handler, decision='approved', reason='同意')
                return self.act(ticket, 'confirmations', self.submitter, result_round=1, outcome='resolved')

            before = self.detail(ticket)
            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(perform, range(2)))
            self.assertEqual(sorted(r[0] for r in responses), [200, 409])
            after = self.detail(ticket)
            self.assertEqual(after['version'], before['version'] + 1)
            self.assertEqual(len(after['history']), len(before['history']) + 1)
            if scenario == 'mixed':
                self.assertIn(after['cancellation_requests'][0]['status'], ('approved', 'superseded'))
                self.assertEqual(after['closed_at'] is not None, after['status'] == 'closed')

    def test_failures_roll_back_business_rows_and_history(self):
        ticket = self.active_ticket()
        actions = [('results', self.handler, {'result': '第一次'}),
                   ('confirmations', self.submitter, {'result_round': 1, 'outcome': 'unresolved'}),
                   ('cancellation-requests', self.submitter, {'reason': '申请'})]
        for action, token, fields in actions:
            ticket = self.rollback_then_succeed(ticket, action, token, fields)
        request_id = self.detail(ticket)['cancellation_requests'][0]['id']
        decision = f'cancellation-requests/{request_id}/decision'
        ticket = self.rollback_then_succeed(ticket, decision, self.handler, {'decision': 'rejected', 'reason': '继续'})
        ticket, decision = self.pending_request(ticket)
        self.rollback_then_succeed(ticket, decision, self.handler, {'decision': 'approved', 'reason': '同意'})
        ticket = self.active_ticket()
        ticket = self.success(ticket, 'results', self.handler, result='修复')
        ticket, _ = self.pending_request(ticket)
        self.rollback_then_succeed(ticket, 'confirmations', self.submitter, {'result_round': 1, 'outcome': 'resolved'})
        self.rollback_then_succeed(self.ticket(self.submitter), 'cancel', self.submitter, {'reason': '撤销'})
        # Failure after save_action must also roll back its update and history.
        ticket = self.active_ticket()
        before = self.detail(ticket)
        with connect(self.path) as connection, connection:
            connection.execute('''CREATE TRIGGER fail_result BEFORE INSERT ON ticket_results
                                  BEGIN SELECT unavailable_test_function(); END''')
        self.assertEqual(self.act(ticket, 'results', self.handler, result='失败')[0], 503)
        self.assertEqual(self.detail(ticket), before)

    def rollback_then_succeed(self, ticket, action, token, fields):
        before = self.detail(ticket)
        with connect(self.path) as connection, connection:
            connection.execute('''CREATE TRIGGER fail_history BEFORE INSERT ON ticket_history
                                  BEGIN SELECT unavailable_test_function(); END''')
        self.assertEqual(self.act(ticket, action, token, **fields)[0], 503)
        self.assertEqual(self.detail(ticket), before)
        with connect(self.path) as connection, connection:
            connection.execute('DROP TRIGGER fail_history')
        return self.success(ticket, action, token, **fields)

    def test_v3_migration_preserves_data_and_rolls_back(self):
        legacy = Path(self.directory.name) / 'legacy.sqlite3'
        with connect(legacy) as connection, connection:
            for statement in (*SCHEMA, *TICKET_SCHEMA):
                connection.execute(statement)
            for statement in ('ALTER TABLE tickets ADD COLUMN assigned_at INTEGER',
                              'ALTER TABLE tickets ADD COLUMN accepted_at INTEGER',
                              'ALTER TABLE ticket_history ADD COLUMN from_assignee_id TEXT REFERENCES users(id)',
                              'ALTER TABLE ticket_history ADD COLUMN to_assignee_id TEXT REFERENCES users(id)',
                              'ALTER TABLE ticket_history ADD COLUMN note TEXT'):
                connection.execute(statement)
            connection.execute('PRAGMA user_version = 3')
        ticket = self.active_ticket()
        with connect(self.path) as source, connect(legacy) as destination, destination:
            for table in ('users', 'roles', 'user_roles', 'support_groups', 'user_group_scopes',
                          'service_systems', 'sessions', 'conversations', 'tickets', 'ticket_history'):
                columns = [row[1] for row in destination.execute(f'PRAGMA table_info({table})')]
                names = ', '.join(columns)
                placeholders = ', '.join('?' for _ in columns)
                destination.executemany(f'INSERT INTO {table} ({names}) VALUES ({placeholders})',
                                        source.execute(f'SELECT {names} FROM {table}').fetchall())
        broken = Path(self.directory.name) / 'broken.sqlite3'
        with connect(legacy) as source, connect(broken) as destination, destination:
            source.backup(destination)
            destination.execute('ALTER TABLE tickets ADD COLUMN result_submitted_at INTEGER')
        with self.assertRaises(sqlite3.OperationalError):
            initialize_database(broken)
        with connect(broken) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 3)
            self.assertNotIn('result_round', [r[1] for r in connection.execute('PRAGMA table_info(tickets)')])
        initialize_database(legacy)
        initialize_database(legacy)
        with connect(legacy) as connection:
            row = dict(connection.execute('SELECT * FROM tickets WHERE id = ?', (ticket['id'],)).fetchone())
            self.assertEqual(row, {k: v for k, v in ticket.items() if k not in ('previous_ticket_id', 'cancellation_requires_approval')})
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM ticket_history').fetchone()[0], 3)
            self.assertGreater(connection.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 5)

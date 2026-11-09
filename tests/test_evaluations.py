import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.database import connect, initialize_database
from app.evaluations import BEIJING, evaluation_deadline, expire_evaluations
from tests.test_tickets import TicketHTTPTestCase


class EvaluationTests(TicketHTTPTestCase):
    def setUp(self):
        super().setUp()
        self.submitter = self.login()
        self.dispatcher = self.login('dispatcher')
        self.handler = self.login('handler')

    def closed_ticket(self):
        ticket = self.ticket(self.submitter)
        for action, token, fields in (
            ('assignments', self.dispatcher, {'assignee_id': 'test-handler'}),
            ('accept', self.handler, {}),
            ('results', self.handler, {'result': '已处理'}),
            ('confirmations', self.submitter, {'result_round': 1, 'outcome': 'resolved'}),
        ):
            status, ticket, _ = self.request(
                f"/api/v1/tickets/{ticket['id']}/{action}", 'POST',
                {'expected_version': ticket['version'], **fields}, token,
            )
            self.assertEqual(status, 200, ticket)
        return ticket

    def detail(self, ticket, token=None):
        status, body, _ = self.request(f"/api/v1/tickets/{ticket['id']}", token=token or self.submitter)
        self.assertEqual(status, 200, body)
        return body

    def evaluate(self, ticket, rating='good', token=None, **fields):
        return self.request(f"/api/v1/tickets/{ticket['id']}/evaluation", 'PUT',
                            {'expected_version': ticket['version'], 'rating': rating, **fields},
                            token or self.submitter)

    def test_create_modify_reason_history_and_restart(self):
        ticket = self.closed_ticket()
        closed_at = ticket['closed_at']
        status, first, _ = self.evaluate(ticket, 'neutral', reason=' 处理较慢 ')
        self.assertEqual(status, 200, first)
        status, second, _ = self.evaluate(first, 'bad', reason='仍不满意')
        self.assertEqual(status, 200, second)
        self.assertEqual(self.evaluate(first)[0], 409)
        status, last, _ = self.evaluate(second)
        self.assertEqual(status, 200, last)
        self.stop_server()
        self.start_server()
        detail = self.detail(last)
        self.assertEqual(detail['closed_at'], closed_at)
        self.assertEqual(detail['version'], ticket['version'] + 3)
        self.assertEqual(detail['evaluation']['rating'], 'good')
        self.assertIsNone(detail['evaluation']['reason'])
        self.assertEqual(detail['evaluation']['source'], 'active')
        self.assertEqual([e['rating'] for e in detail['evaluation_history']], ['neutral', 'bad', 'good'])
        self.assertEqual(detail['evaluation_history'][0]['reason'], '处理较慢')
        self.assertEqual(detail['evaluation_history'][-1]['actor_id'], 'test-submitter')
        self.assertEqual(self.detail(last, self.handler)['evaluation'], detail['evaluation'])

    def test_permissions_states_and_validation(self):
        pending = self.ticket(self.submitter)
        self.assertEqual(self.evaluate(pending)[0], 409)
        ticket = self.closed_ticket()
        path = f"/api/v1/tickets/{ticket['id']}/evaluation"
        self.assertEqual(self.request(path, 'PUT', {'expected_version': ticket['version'], 'rating': 'good'})[0], 401)
        self.assertEqual(self.evaluate(ticket, token=self.handler)[0], 403)
        self.assertEqual(self.evaluate(ticket, token=self.login('submitter_other'))[0], 404)
        for rating in ('neutral', 'bad'):
            for reason in (None, '', ' \n '):
                self.assertEqual(self.evaluate(ticket, rating, reason=reason)[0], 422)
        for fields in ({'rating': 'unknown'}, {'reason': 'x' * 10001}, {'reason': 123},
                       {'expected_version': True}, {'source': 'automatic'}, {'actor_id': 'test-handler'}):
            body = {'expected_version': ticket['version'], 'rating': 'good', **fields}
            self.assertEqual(self.request(path, 'PUT', body, self.submitter)[0], 422)
        with connect(self.path) as connection, connection:
            connection.execute("DELETE FROM user_roles WHERE user_id = 'test-submitter' AND role = 'submitter'")
        self.assertEqual(self.evaluate(ticket)[0], 404)

    def test_calendar_deadline_and_exact_lock_boundary(self):
        for date in ('2026-11-06T00:00:00', '2026-11-06T23:59:59', '2028-02-26T12:00:00', '2026-12-28T09:00:00'):
            closed = datetime.fromisoformat(date).replace(tzinfo=BEIJING)
            deadline = datetime.fromtimestamp(evaluation_deadline(int(closed.timestamp())), BEIJING)
            self.assertEqual((deadline.date() - closed.date()).days, 8)
            self.assertEqual((deadline.hour, deadline.minute, deadline.second), (0, 0, 0))
        ticket = self.closed_ticket()
        deadline = evaluation_deadline(ticket['closed_at'])
        with patch('app.tickets.time', SimpleNamespace(time=lambda: deadline - 1)):
            status, updated, _ = self.evaluate(ticket, 'bad', reason='待改进')
            self.assertEqual(status, 200)
            self.assertFalse(self.detail(updated)['evaluation_locked'])
        with patch('app.tickets.time', SimpleNamespace(time=lambda: deadline)):
            self.assertEqual(self.evaluate(updated)[1]['detail']['code'], 'evaluation_locked')
            self.assertTrue(self.detail(updated)['evaluation_locked'])
        with patch('app.evaluations.time', SimpleNamespace(time=lambda: deadline)):
            self.assertEqual(expire_evaluations(self.path), 0)
        self.assertEqual(self.detail(updated)['evaluation']['rating'], 'bad')

    def test_automatic_boundary_repeat_and_existing_result(self):
        ticket = self.closed_ticket()
        deadline = evaluation_deadline(ticket['closed_at'])
        with patch('app.evaluations.time', SimpleNamespace(time=lambda: deadline - 1)):
            self.assertEqual(expire_evaluations(self.path), 0)
        with patch('app.evaluations.time', SimpleNamespace(time=lambda: deadline)):
            self.assertEqual(expire_evaluations(self.path), 1)
            self.assertEqual(expire_evaluations(self.path), 0)
        detail = self.detail(ticket)
        self.assertEqual(detail['version'], ticket['version'] + 1)
        self.assertEqual(detail['closed_at'], ticket['closed_at'])
        self.assertEqual(detail['evaluation']['source'], 'automatic')
        self.assertIsNone(detail['evaluation']['actor_id'])
        self.assertEqual(len(detail['evaluation_history']), 1)
        with patch('app.tickets.time', SimpleNamespace(time=lambda: deadline)):
            self.assertEqual(self.evaluate(detail)[1]['detail']['code'], 'evaluation_locked')
        # A later wall-clock correction must not reopen an automatic result.
        with patch('app.tickets.time', SimpleNamespace(time=lambda: deadline - 1)):
            self.assertEqual(self.evaluate(detail)[1]['detail']['code'], 'evaluation_locked')
            self.assertTrue(self.detail(detail)['evaluation_locked'])

    def test_concurrent_manual_and_expiration(self):
        for automatic in (False, True):
            ticket = self.closed_ticket()
            deadline = evaluation_deadline(ticket['closed_at'])
            barrier = threading.Barrier(2)

            def perform(index):
                barrier.wait(timeout=10)
                if automatic and index:
                    return expire_evaluations(self.path)
                return self.evaluate(ticket, 'bad', reason='并发评价')[0]

            # Manual request samples just before deadline while expiration samples at it.
            with patch('app.tickets.time', SimpleNamespace(time=lambda: deadline - 1)), \
                 patch('app.evaluations.time', SimpleNamespace(time=lambda: deadline)), \
                 ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(perform, range(2)))
            if automatic:
                self.assertIn(responses, ([200, 0], [409, 1]))
            else:
                self.assertEqual(sorted(responses), [200, 409])
            detail = self.detail(ticket)
            self.assertEqual(detail['version'], ticket['version'] + 1)
            self.assertEqual(len(detail['evaluation_history']), 1)
        # Two independent expiration workers also serialize and create one result.
        ticket = self.closed_ticket()
        with patch('app.evaluations.time', SimpleNamespace(time=lambda: evaluation_deadline(ticket['closed_at']))), \
             ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(lambda _: expire_evaluations(self.path), range(2))), [0, 1])

    def test_write_failure_rolls_back_and_retries(self):
        ticket = self.closed_ticket()
        before = self.detail(ticket)
        with connect(self.path) as connection, connection:
            connection.execute('''CREATE TRIGGER fail_evaluation BEFORE INSERT ON evaluation_history
                                  BEGIN SELECT unavailable_test_function(); END''')
        self.assertEqual(self.evaluate(ticket)[0], 503)
        self.assertEqual(self.detail(ticket), before)
        with patch('app.evaluations.time', SimpleNamespace(time=lambda: evaluation_deadline(ticket['closed_at']))):
            with self.assertRaises(sqlite3.OperationalError):
                expire_evaluations(self.path)
        self.assertEqual(self.detail(ticket), before)
        with connect(self.path) as connection, connection:
            connection.execute('DROP TRIGGER fail_evaluation')
        status, updated, _ = self.evaluate(ticket)
        self.assertEqual(status, 200, updated)
        due = self.closed_ticket()
        with patch('app.evaluations.time', SimpleNamespace(time=lambda: evaluation_deadline(due['closed_at']))):
            self.assertEqual(expire_evaluations(self.path), 1)
            self.assertEqual(expire_evaluations(self.path), 0)

    def test_statistics_scope_counts_and_participation(self):
        manager = self.login('supervisor')
        path = '/api/v1/evaluation-statistics'
        self.assertEqual(self.request(path)[0], 401)
        for token in (self.submitter, self.handler, self.dispatcher, self.login('config_admin')):
            self.assertEqual(self.request(path, token=token)[0], 403)
        self.assertIsNone(self.request(path, token=manager)[1]['participation_rate'])
        self.assertEqual(self.request(path + '?group_id=test-admin', token=manager)[0], 403)
        active = self.closed_ticket()
        self.assertEqual(self.evaluate(active, 'neutral', reason='一般')[0], 200)
        automatic = self.closed_ticket()
        waiting = self.closed_ticket()
        hidden = self.closed_ticket()
        with connect(self.path) as connection, connection:
            connection.execute('UPDATE tickets SET closed_at = 0 WHERE id = ?', (automatic['id'],))
            connection.execute("UPDATE tickets SET group_id = 'test-admin' WHERE id = ?", (hidden['id'],))
        stats = self.request(path, token=manager)[1]
        self.assertEqual(stats['closed_total'], 3)
        self.assertEqual(stats['overdue_unevaluated'], 1)
        self.assertEqual(expire_evaluations(self.path), 1)
        stats = self.request(path + '?group_id=test-it', token=manager)[1]
        self.assertEqual(stats['active'], {'good': 0, 'neutral': 1, 'bad': 0})
        self.assertEqual(stats['automatic'], {'good': 1, 'neutral': 0, 'bad': 0})
        self.assertEqual(stats['unevaluated_total'], 1)
        self.assertEqual(stats['overdue_unevaluated'], 0)
        self.assertEqual(stats['participation_rate'], 1 / 3)
        with connect(self.path) as connection, connection:
            connection.execute("DELETE FROM user_group_scopes WHERE user_id = 'test-supervisor'")
        self.assertEqual(self.request(path, token=manager)[1]['closed_total'], 0)
        with connect(self.path) as connection, connection:
            connection.execute("DELETE FROM user_roles WHERE user_id = 'test-supervisor' AND role = 'manager'")
        self.assertEqual(self.request(path, token=manager)[0], 403)

    def test_v4_migration_preserves_data_and_is_atomic(self):
        ticket = self.closed_ticket()
        legacy = Path(self.directory.name) / 'legacy.sqlite3'
        with connect(self.path) as source, connect(legacy) as destination, destination:
            source.backup(destination)
            destination.execute('DROP TABLE evaluation_history')
            destination.execute('DROP TABLE ticket_evaluations')
            destination.execute('DROP INDEX tickets_closed')
            destination.execute('PRAGMA user_version = 4')
        broken = Path(self.directory.name) / 'broken.sqlite3'
        with connect(legacy) as source, connect(broken) as destination, destination:
            source.backup(destination)
            destination.execute('CREATE TABLE evaluation_history (id INTEGER)')
        with self.assertRaises(sqlite3.OperationalError):
            initialize_database(broken)
        with connect(broken) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name = 'ticket_evaluations'").fetchone())
        initialize_database(legacy)
        initialize_database(legacy)
        with connect(legacy) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 5)
            row = dict(connection.execute('SELECT * FROM tickets WHERE id = ?', (ticket['id'],)).fetchone())
            self.assertEqual(row, {k: v for k, v in ticket.items() if k not in ('previous_ticket_id', 'cancellation_requires_approval')})
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM ticket_history').fetchone()[0], 5)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM ticket_evaluations').fetchone()[0], 0)

    def test_cli_expiration_and_cancelled_exclusion(self):
        ticket = self.closed_ticket()
        cancelled = self.ticket(self.submitter)
        status, _, _ = self.request(f"/api/v1/tickets/{cancelled['id']}/cancel", 'POST',
                                    {'expected_version': cancelled['version'], 'reason': '无需处理'}, self.submitter)
        self.assertEqual(status, 200)
        with connect(self.path) as connection, connection:
            connection.execute('UPDATE tickets SET closed_at = 0 WHERE id = ?', (ticket['id'],))
        for count in (1, 0):
            result = subprocess.run([sys.executable, '-m', 'app.evaluations'],
                                    env={**os.environ, 'IT_ADMIN_DATABASE': str(self.path)},
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f'Created {count} automatic evaluations.', result.stdout)
        self.assertIsNone(self.detail(cancelled)['evaluation'])
        self.assertEqual(self.detail(ticket)['evaluation']['source'], 'automatic')

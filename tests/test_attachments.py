import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from app.attachments import MAX_FILE_SIZE, MAX_REQUEST_SIZE, read_uploads
from app.database import SCHEMA_VERSION, connect, initialize_database
from tests.test_tickets import TicketHTTPTestCase


class AttachmentTests(TicketHTTPTestCase):
    def raw(self, path, token=None, body=None, content_type=None):
        headers = {}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        if content_type:
            headers['Content-Type'] = content_type
        request = Request(self.url + path, data=body, headers=headers)
        try:
            response = self.opener.open(request, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            content = response.read()
            if 'application/json' in response.headers.get('Content-Type', ''):
                content = json.loads(content)
            return response.status, content, response.headers

    def upload(self, token, files=None, field='files'):
        if files is None:
            files = [('报错.txt', b'error detail', 'text/plain')]
        boundary = 'attachment-test-boundary'
        parts = []
        for name, content, content_type in files:
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                          f'filename="{name}"\r\nContent-Type: {content_type}\r\n\r\n').encode() + content + b'\r\n')
        body = b''.join(parts) + f'--{boundary}--\r\n'.encode()
        return self.raw('/api/v1/attachments', token, body, f'multipart/form-data; boundary={boundary}')

    def create_with_files(self, token, ids, conversation=None):
        conversation = conversation or self.conversation(token)
        return self.request(f'/api/v1/conversations/{conversation}/ticket', 'POST', {
            'service_system_id': 'test-office', 'title': '附件测试', 'description': '描述', 'file_ids': ids,
        }, token)

    def test_upload_create_download_retry_and_restart(self):
        token = self.login()
        files = [('截图.png', b'png-test', 'image/png'), ('日志.txt', b'log', 'text/plain'),
                 ('../report.html', b'<script>alert(1)</script>', 'text/html')]
        status, result, _ = self.upload(token, files)
        self.assertEqual(status, 201, result)
        ids = [item['file_id'] for item in result['items']]
        self.assertEqual(result['items'][2]['file_name'], 'report.html')
        with connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM tickets').fetchone()[0], 0)
        conversation = self.conversation(token)
        status, ticket, _ = self.create_with_files(token, ids, conversation)
        self.assertEqual(status, 201, ticket)
        self.assertEqual(len(ticket['files']), 3)
        retry = self.create_with_files(token, ['unknown'], conversation)
        self.assertEqual(retry[:2], (200, ticket))
        self.stop_server()
        self.start_server()
        detail = self.request(f"/api/v1/tickets/{ticket['id']}", token=token)[1]
        self.assertEqual(detail['files'], ticket['files'])
        for file_id, (_, content, _) in zip(ids, files):
            code, downloaded, headers = self.raw(f'/api/v1/attachments/{file_id}', token)
            self.assertEqual((code, downloaded), (200, content))
            self.assertTrue(headers['Content-Disposition'].startswith('attachment;'))
            self.assertEqual(headers['Content-Type'], 'application/octet-stream')
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_upload_validation_and_batch_atomicity(self):
        token = self.login()
        for files, expected in (
            ([], 422), ([('empty.txt', b'', 'text/plain')], 422),
            ([('a', b'a', 'text/plain')] * 6, 413),
            ([('good', b'a', 'text/plain'), ('large', b'x' * (MAX_FILE_SIZE + 1), 'text/plain')], 413),
            ([('x' * 256, b'a', 'text/plain')], 422),
        ):
            with self.subTest(expected=expected, count=len(files)):
                self.assertEqual(self.upload(token, files)[0], expected)
        self.assertEqual(self.upload(token, field='wrong')[0], 422)
        self.assertEqual(self.raw('/api/v1/attachments', token, b'{}', 'application/json')[0], 422)
        self.assertEqual(self.raw('/api/v1/attachments', token, b'invalid', 'multipart/form-data')[0], 413)
        with patch('app.attachments.MAX_REQUEST_SIZE', 16):
            self.assertEqual(self.upload(token)[0], 413)
        with connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM attachments').fetchone()[0], 0)
        self.assertGreater(MAX_REQUEST_SIZE, MAX_FILE_SIZE)
        self.assertEqual(self.upload(token, [('limit.txt', b'x' * MAX_FILE_SIZE, 'text/plain')])[0], 201)

    def test_owner_binding_and_download_permissions_follow_reassignment(self):
        token = self.login()
        other = self.login('submitter_other')
        dispatcher = self.login('dispatcher')
        handler = self.login('handler')
        backup = self.login('handler_backup')
        file_id = self.upload(token)[1]['items'][0]['file_id']
        path = f'/api/v1/attachments/{file_id}'
        self.assertEqual(self.upload(None)[0], 401)
        self.assertEqual(self.raw(path)[0], 401)
        self.assertEqual(self.raw(path, other)[0], 404)
        self.assertEqual(self.raw(path, dispatcher)[0], 404)
        self.assertEqual(self.create_with_files(other, [file_id])[0], 422)
        status, ticket, _ = self.create_with_files(token, [file_id])
        self.assertEqual(status, 201)
        self.assertEqual(self.create_with_files(token, [file_id])[0], 422)
        assigned = self.request(f"/api/v1/tickets/{ticket['id']}/assignments", 'POST',
                                {'expected_version': ticket['version'], 'assignee_id': 'test-handler'}, dispatcher)[1]
        self.assertEqual(self.raw(path, handler)[0], 200)
        self.assertEqual(self.raw(path, dispatcher)[0], 200)
        self.assertEqual(self.raw(path, other)[0], 404)
        self.request(f"/api/v1/tickets/{ticket['id']}/reassignments", 'POST',
                     {'expected_version': assigned['version'], 'assignee_id': 'test-handler_backup', 'reason': '交接'}, dispatcher)
        self.assertEqual(self.raw(path, handler)[0], 404)
        self.assertEqual(self.raw(path, backup)[0], 200)

    def test_append_version_terminal_states_and_rollback(self):
        token = self.login()
        other = self.login('submitter_other')
        dispatcher = self.login('dispatcher')
        ticket = self.ticket(token)
        file_id = self.upload(token)[1]['items'][0]['file_id']
        path = f"/api/v1/tickets/{ticket['id']}/attachments"
        body = {'expected_version': 1, 'file_ids': [file_id]}
        self.assertEqual(self.request(path, 'POST', body, other)[0], 404)
        self.assertEqual(self.request(path, 'POST', body, dispatcher)[0], 403)
        self.assertEqual(self.request(path, 'POST', {**body, 'file_ids': [file_id, file_id]}, token)[0], 422)
        self.assertEqual(self.request(path, 'POST', {**body, 'file_ids': [file_id, 'missing']}, token)[0], 422)
        with connect(self.path) as connection, connection:
            self.assertIsNone(connection.execute('SELECT ticket_id FROM attachments').fetchone()[0])
            connection.execute("""CREATE TRIGGER fail_attachment_history BEFORE INSERT ON ticket_history
                WHEN NEW.action = 'attachments_added' BEGIN SELECT * FROM missing_attachment_table; END""")
        self.assertEqual(self.request(path, 'POST', body, token)[0], 503)
        with connect(self.path) as connection, connection:
            self.assertIsNone(connection.execute('SELECT ticket_id FROM attachments').fetchone()[0])
            self.assertEqual(connection.execute('SELECT version FROM tickets').fetchone()[0], 1)
            connection.execute('DROP TRIGGER fail_attachment_history')
        status, updated, _ = self.request(path, 'POST', body, token)
        self.assertEqual((status, updated['version'], len(updated['files'])), (200, 2, 1))
        self.assertEqual(self.request(path, 'POST', body, token)[0], 409)
        self.assertEqual(self.request(path, 'POST', {**body, 'expected_version': 2}, token)[1]['version'], 2)
        for terminal in ('closed', 'cancelled'):
            with connect(self.path) as connection, connection:
                connection.execute('UPDATE tickets SET status = ?', (terminal,))
            self.assertEqual(self.request(path, 'POST', {**body, 'expected_version': 2}, token)[0], 409)

    def test_concurrent_binding_one_file_to_two_tickets(self):
        token = self.login()
        file_id = self.upload(token)[1]['items'][0]['file_id']
        conversations = [self.conversation(token), self.conversation(token)]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda conversation: self.create_with_files(token, [file_id], conversation)[0], conversations))
        self.assertCountEqual(results, [201, 422])
        with connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM tickets').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM ticket_history').fetchone()[0], 1)

    def test_supplement_text_and_files_preserve_original_and_survive_restart(self):
        token = self.login()
        ticket = self.ticket(token)
        file_id = self.upload(token)[1]['items'][0]['file_id']
        path = f"/api/v1/tickets/{ticket['id']}/supplements"
        body = {'expected_version': 1, 'description': '  补充现场现象\n第二行  ', 'file_ids': [file_id]}
        status, updated, _ = self.request(path, 'POST', body, token)
        self.assertEqual(status, 200, updated)
        self.assertEqual((updated['version'], updated['status'], updated['description']),
                         (2, ticket['status'], ticket['description']))
        self.assertEqual(updated['files'][0]['file_id'], file_id)
        self.assertEqual(self.request(path, 'POST', body, token)[0], 409)
        self.stop_server()
        self.start_server()
        detail = self.request(f"/api/v1/tickets/{ticket['id']}", token=token)[1]
        event = detail['history'][-1]
        self.assertEqual((event['action'], event['actor_id'], event['note']),
                         ('supplement_added', ticket['submitter_id'], '补充现场现象\n第二行'))
        self.assertIsInstance(event['created_at'], int)
        self.assertEqual(detail['files'], updated['files'])
        updated = self.request(path, 'POST', {'expected_version': 2, 'description': '再次补充'}, token)[1]
        self.assertEqual(updated['version'], 3)
        # Binding only files already on this ticket is a no-op at the latest version.
        updated = self.request(path, 'POST', {'expected_version': 3, 'file_ids': [file_id]}, token)[1]
        self.assertEqual(updated['version'], 3)

    def test_supplement_validation_permissions_and_allowed_states(self):
        token = self.login()
        ticket = self.ticket(token)
        path = f"/api/v1/tickets/{ticket['id']}/supplements"
        valid = {'expected_version': 1, 'description': '补充'}
        self.assertEqual(self.request(path, 'POST', valid)[0], 401)
        self.assertEqual(self.request(path, 'POST', valid, self.login('submitter_other'))[0], 404)
        self.assertEqual(self.request(path, 'POST', valid, self.login('dispatcher'))[0], 403)
        for fields in ({}, {'description': ' \n '}, {'description': None}, {'description': 123},
                       {'description': 'x' * 10001}, {'file_ids': ['x'] * 6}):
            self.assertEqual(self.request(path, 'POST', {'expected_version': 1, **fields}, token)[0], 422)
        for current in ('pending_assignment', 'pending_acceptance', 'in_progress', 'awaiting_confirmation'):
            with connect(self.path) as connection, connection:
                connection.execute('UPDATE tickets SET status = ? WHERE id = ?', (current, ticket['id']))
                version = connection.execute('SELECT version FROM tickets WHERE id = ?', (ticket['id'],)).fetchone()[0]
            status, updated, _ = self.request(path, 'POST', {
                'expected_version': version, 'description': 'x' * 10000,
            }, token)
            self.assertEqual(status, 200, updated)
            self.assertEqual(updated['status'], current)
            self.assertEqual(updated['result_round'], 0)
        for terminal in ('closed', 'cancelled'):
            with connect(self.path) as connection, connection:
                connection.execute('UPDATE tickets SET status = ? WHERE id = ?', (terminal, ticket['id']))
            self.assertEqual(self.request(path, 'POST', {
                **valid, 'expected_version': updated['version'],
            }, token)[0], 409)

    def test_supplement_atomicity_and_file_ownership(self):
        token = self.login()
        ticket = self.ticket(token)
        file_id = self.upload(token)[1]['items'][0]['file_id']
        other_file = self.upload(self.login('submitter_other'))[1]['items'][0]['file_id']
        path = f"/api/v1/tickets/{ticket['id']}/supplements"
        body = {'expected_version': 1, 'description': '补充原文', 'file_ids': [file_id]}
        for ids in ([file_id, other_file], [file_id, 'missing'], [file_id, file_id]):
            self.assertEqual(self.request(path, 'POST', {**body, 'file_ids': ids}, token)[0], 422)
        with connect(self.path) as connection, connection:
            connection.execute("""CREATE TRIGGER fail_supplement BEFORE INSERT ON ticket_history
                WHEN NEW.action = 'supplement_added' BEGIN SELECT * FROM missing_supplement_table; END""")
        self.assertEqual(self.request(path, 'POST', body, token)[0], 503)
        with connect(self.path) as connection, connection:
            self.assertIsNone(connection.execute('SELECT ticket_id FROM attachments WHERE id = ?', (file_id,)).fetchone()[0])
            self.assertEqual(connection.execute('SELECT version FROM tickets').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM ticket_history').fetchone()[0], 1)
            connection.execute('DROP TRIGGER fail_supplement')
        self.assertEqual(self.request(path, 'POST', body, token)[0], 200)

    def test_concurrent_supplements_allow_one_version_update(self):
        token = self.login()
        ticket = self.ticket(token)
        path = f"/api/v1/tickets/{ticket['id']}/supplements"
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda text: self.request(path, 'POST', {
                'expected_version': 1, 'description': text,
            }, token)[0], ['补充一', '补充二']))
        self.assertCountEqual(responses, [200, 409])
        with connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT version FROM tickets').fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ticket_history WHERE action = 'supplement_added'").fetchone()[0], 1)

    def test_upload_database_failure_rolls_back_entire_batch(self):
        token = self.login()
        with connect(self.path) as connection, connection:
            connection.execute("""CREATE TRIGGER fail_second_file BEFORE INSERT ON attachments
                WHEN NEW.file_name = 'second' BEGIN SELECT * FROM missing_attachment_table; END""")
        self.assertEqual(self.upload(token, [('first', b'1', 'text/plain'), ('second', b'2', 'text/plain')])[0], 503)
        with connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM attachments').fetchone()[0], 0)

    def test_upload_rechecks_revoked_identity(self):
        token = self.login()

        async def revoke_after_read(request):
            files = await read_uploads(request)
            with connect(self.path) as connection, connection:
                connection.execute("UPDATE users SET active = 0 WHERE id = 'test-submitter'")
            return files

        with patch('app.tickets.read_uploads', revoke_after_read):
            self.assertEqual(self.upload(token)[0], 401)
        with connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM attachments').fetchone()[0], 0)

    def test_v5_migration_and_atomic_failure(self):
        token = self.login()
        ticket = self.ticket(token)
        legacy = Path(self.directory.name) / 'v5.sqlite3'
        with connect(self.path) as source, connect(legacy) as destination, destination:
            source.backup(destination)
            destination.execute('DROP TABLE iam_profiles')
            destination.execute('DROP TABLE attachments')
            destination.execute('ALTER TABLE tickets DROP COLUMN priority')
            destination.execute('PRAGMA user_version = 5')
        broken = Path(self.directory.name) / 'broken.sqlite3'
        with connect(legacy) as source, connect(broken) as destination, destination:
            source.backup(destination)
            destination.execute('CREATE TABLE attachments_ticket (id INTEGER)')
        with self.assertRaises(sqlite3.OperationalError):
            initialize_database(broken)
        with connect(broken) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 5)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='attachments'").fetchone())
        initialize_database(legacy)
        initialize_database(legacy)
        with connect(legacy) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(connection.execute('SELECT id FROM tickets').fetchone()[0], ticket['id'])
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM attachments').fetchone()[0], 0)

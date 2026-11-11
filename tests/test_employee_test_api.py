import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request

from app.database import connect
from app.employee_test_api import employee_principal
from app.iam import EmployeeProfile, IAMError
from tests.test_tickets import TicketHTTPTestCase


PROFILE = EmployeeProfile('employee-api-1', '000001', '工号测试甲', '测试部门',
                          'employee@example.invalid', '测试地点', None, None, None)
OTHER = replace(PROFILE, iam_user_id='employee-api-2', employee_number='000002', display_name='工号测试乙')


class EmployeeTestAPITests(TicketHTTPTestCase):
    def setUp(self):
        super().setUp()
        self.app.state.employee_test_api = True
        self.app.state.iam_client = Mock()
        profiles = {profile.employee_number: profile for profile in (PROFILE, OTHER)}
        self.app.state.iam_client.lookup.side_effect = lambda *, username: profiles[username]

    def endpoint(self, path, employee_no='000001', **query):
        return '/api/test/v1' + path + '?' + urlencode({'employee_no': employee_no, **query})

    def create(self, employee_no='000001', file_ids=None):
        conversation = self.request(self.endpoint('/conversations', employee_no), 'POST', {})
        self.assertEqual(conversation[0], 201, conversation)
        path = self.endpoint(f"/conversations/{conversation[1]['id']}/ticket", employee_no)
        body = {'service_system_id': 'test-office', 'title': '工号API测试',
                'description': '原始描述', 'priority': 'P3', 'file_ids': file_ids or []}
        response = self.request(path, 'POST', body)
        self.assertEqual(response[0], 201, response)
        return response[1], path, body

    def raw(self, path, body=None, content_type=None):
        headers = {'Content-Type': content_type} if content_type else {}
        request = Request(self.url + path, data=body, headers=headers)
        try:
            response = self.opener.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            content = response.read()
            if 'application/json' in response.headers.get('Content-Type', ''):
                content = json.loads(content)
            return response.status, content

    def upload(self, employee_no='000001'):
        body = (b'--test-boundary\r\nContent-Disposition: form-data; name="files"; '
                b'filename="test.txt"\r\nContent-Type: text/plain\r\n\r\nhello\r\n--test-boundary--\r\n')
        return self.raw(self.endpoint('/attachments', employee_no), body,
                        'multipart/form-data; boundary=test-boundary')

    def test_switch_required_employee_number_and_existing_auth(self):
        self.app.state.employee_test_api = False
        self.assertEqual(self.request(self.endpoint('/tickets'))[0], 404)
        self.app.state.iam_client.lookup.assert_not_called()
        self.app.state.employee_test_api = True
        for query in ('', '?employee_no=', '?employee_no=%20', '?employee_no=' + '1' * 65):
            self.assertEqual(self.request('/api/test/v1/tickets' + query)[0], 422)
        self.assertEqual(self.request('/api/v1/tickets')[0], 401)
        self.assertEqual(self.request('/api/v1/tickets?employee_no=000001')[0], 401)
        schema = self.request('/openapi.json')[1]['paths']
        test_operation = schema['/api/test/v1/tickets']['get']
        self.assertNotIn('security', test_operation)
        self.assertTrue(next(p for p in test_operation['parameters'] if p['name'] == 'employee_no')['required'])
        self.assertIn('multipart/form-data', schema['/api/test/v1/attachments']['post']['requestBody']['content'])

    def test_number_only_create_query_supplement_confirm_and_retry(self):
        ticket, path, body = self.create()
        self.assertRegex(ticket['id'], r'^IT\d{12}$')
        self.assertEqual(self.request(path, 'POST', body)[:2], (200, ticket))
        self.assertEqual(self.request(self.endpoint(f"/conversations/{ticket['conversation_id']}"))[1]['ticket_id'], ticket['id'])
        self.assertTrue(self.request(self.endpoint('/service-systems'))[1]['items'])
        listing = self.request(self.endpoint('/tickets', q=ticket['id'], limit=1, status='pending_assignment'))[1]
        self.assertEqual((listing['total'], listing['items'][0]['id']), (1, ticket['id']))
        url = f"/tickets/{ticket['id']}"
        supplement = {'expected_version': 1, 'description': '工号接口补充'}
        updated = self.request(self.endpoint(url + '/supplements'), 'POST', supplement)
        self.assertEqual(updated[0], 200, updated)
        self.assertEqual(updated[1]['version'], 2)
        self.assertEqual(self.request(self.endpoint(url + '/supplements'), 'POST', supplement)[0], 409)
        detail = self.request(self.endpoint(url))[1]
        self.assertEqual(detail['description'], '原始描述')
        self.assertEqual(detail['history'][-1]['note'], '工号接口补充')
        # Staff handling uses the established role-protected API.
        dispatcher = self.login('dispatcher')
        handler = self.login('handler')
        version = 2
        for action, token, fields in (
            ('assignments', dispatcher, {'assignee_id': 'test-handler'}),
            ('accept', handler, {}),
            ('results', handler, {'result': '已恢复'}),
        ):
            response = self.request('/api/v1' + url + '/' + action, 'POST',
                                    {'expected_version': version, **fields}, token)
            self.assertEqual(response[0], 200, response)
            version = response[1]['version']
        confirmed = self.request(self.endpoint(url + '/confirmations'), 'POST', {
            'expected_version': version, 'result_round': 1, 'outcome': 'resolved', 'note': '员工确认',
        })
        self.assertEqual(confirmed[0], 200, confirmed)
        self.assertEqual(confirmed[1]['status'], 'closed')
        self.app.state.iam_client.lookup.assert_called_once_with(username='000001')
        with connect(self.path) as connection:
            # Only the two staff logins above create sessions.
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 2)

    def test_employee_isolation_including_multirole_staff_and_versions(self):
        own, _, _ = self.create()
        other, _, _ = self.create('000002')
        with connect(self.path) as connection, connection:
            connection.execute("""INSERT INTO iam_profiles
                (user_id, iam_user_id, employee_number, refreshed_at) VALUES
                ('test-dispatcher', 'employee-api-staff', '000003', 0)""")
        staff, _, _ = self.create('000003')
        result = self.request(self.endpoint('/tickets', '000003', scope='visible'))[1]
        self.assertEqual([item['id'] for item in result['items']], [staff['id']])
        for hidden in (other, staff):
            url = f"/tickets/{hidden['id']}"
            self.assertEqual(self.request(self.endpoint(url))[0], 404)
            self.assertEqual(self.request(self.endpoint(url + '/supplements'), 'POST', {
                'expected_version': 1, 'description': '越权补充',
            })[0], 404)
            self.assertEqual(self.request(self.endpoint(url + '/confirmations'), 'POST', {
                'expected_version': 1, 'result_round': 1, 'outcome': 'resolved',
            })[0], 404)
            self.assertEqual(self.request(self.endpoint(f"/conversations/{hidden['conversation_id']}"))[0], 404)
        self.assertEqual(self.request(self.endpoint('/tickets', q=other['id']))[1]['total'], 0)
        self.assertEqual(self.request(self.endpoint(f"/tickets/{own['id']}"))[0], 200)

    def test_attachment_upload_binding_download_and_owner_rejection(self):
        status, uploaded = self.upload()
        self.assertEqual(status, 201, uploaded)
        file_id = uploaded['items'][0]['file_id']
        self.assertEqual(self.raw(self.endpoint('/attachments/' + file_id)), (200, b'hello'))
        self.assertEqual(self.raw(self.endpoint('/attachments/' + file_id, '000002'))[0], 404)
        ticket, _, _ = self.create(file_ids=[file_id])
        self.assertEqual(ticket['files'][0]['file_id'], file_id)
        more = self.upload()[1]['items'][0]['file_id']
        url = self.endpoint(f"/tickets/{ticket['id']}/supplements")
        added = self.request(url, 'POST', {'expected_version': 1, 'file_ids': [more]})
        self.assertEqual(added[0], 200, added)
        self.assertEqual(len(added[1]['files']), 2)
        self.assertEqual(self.raw(self.endpoint('/attachments/' + file_id, '000002'))[0], 404)
        invalid = self.request(self.endpoint('/attachments'), 'POST', {})
        self.assertEqual(invalid[0], 422)

    def test_missing_iam_errors_conflicts_inactive_and_role_checks(self):
        self.app.state.iam_client = None
        self.assertEqual(self.request(self.endpoint('/tickets'))[0], 503)
        self.app.state.iam_client = Mock()
        self.app.state.iam_client.lookup.side_effect = IAMError('iam_no_match', '无匹配人员', 422)
        self.assertEqual(self.request(self.endpoint('/tickets'))[0], 422)
        with connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM iam_profiles').fetchone()[0], 0)
        self.app.state.iam_client.lookup.side_effect = None
        self.app.state.iam_client.lookup.return_value = PROFILE
        self.assertEqual(self.request(self.endpoint('/tickets'))[0], 200)
        with connect(self.path) as connection, connection:
            connection.execute("UPDATE users SET active = 0 WHERE username = '000001'")
        self.assertEqual(self.request(self.endpoint('/tickets'))[0], 403)
        self.assertEqual(self.upload()[0], 403)
        with connect(self.path) as connection, connection:
            connection.execute("UPDATE users SET active = 1 WHERE username = '000001'")
            connection.execute("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE username = '000001')")
        self.assertEqual(self.request(self.endpoint('/tickets'))[0], 403)
        with connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)

    def test_concurrent_first_use_creates_one_mapping_without_sessions(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.request(self.endpoint('/tickets')), range(8)))
        self.assertTrue(all(result[0] == 200 for result in results), results)
        with connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM iam_profiles').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)

    def test_conflicting_local_identity_is_preserved(self):
        self.app.state.iam_client.lookup.side_effect = None
        self.app.state.iam_client.lookup.return_value = replace(PROFILE, employee_number='submitter')
        response = self.request(self.endpoint('/tickets', 'submitter'))
        self.assertEqual(response[0], 409, response)
        self.assertEqual(response[1]['detail']['code'], 'iam_identity_conflict')
        with connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM iam_profiles').fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT id FROM users WHERE username = 'submitter'").fetchone()[0],
                             'test-submitter')

    def test_writes_recheck_employee_after_lock_and_roll_back_history_failure(self):
        conversation = self.request(self.endpoint('/conversations'), 'POST', {})[1]['id']
        path = self.endpoint(f'/conversations/{conversation}/ticket')
        body = {'service_system_id': 'test-office', 'title': '重验', 'description': '测试'}
        calls = 0

        def deactivate_after_read(connection, employee_no):
            nonlocal calls
            principal = employee_principal(connection, employee_no)
            calls += 1
            if calls == 1:
                with connection:
                    connection.execute('UPDATE users SET active = 0 WHERE id = ?', (principal.id,))
            return principal

        with patch('app.employee_test_api.employee_principal', side_effect=deactivate_after_read):
            self.assertEqual(self.request(path, 'POST', body)[0], 403)
        with connect(self.path) as connection, connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM tickets').fetchone()[0], 0)
            connection.execute("UPDATE users SET active = 1 WHERE username = '000001'")
            connection.execute("""CREATE TRIGGER fail_employee_history BEFORE INSERT ON ticket_history
                                  BEGIN SELECT unavailable_test_function(); END""")
        self.assertEqual(self.request(path, 'POST', body)[0], 503)
        with connect(self.path) as connection, connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM tickets').fetchone()[0], 0)
            connection.execute('DROP TRIGGER fail_employee_history')
        self.assertEqual(self.request(path, 'POST', body)[0], 201)

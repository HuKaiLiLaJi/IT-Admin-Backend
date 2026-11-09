from app.database import connect
from tests.test_foundation import HTTPTestCase


class PasswordlessLoginTests(HTTPTestCase):
    def test_account_selection_login_and_logout(self):
        status, accounts, headers = self.request('/api/v1/auth/test-accounts')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(len(accounts['items']), 9)
        self.assertEqual(set(accounts['items'][0]), {'username', 'display_name'})
        for account in accounts['items']:
            status, login, _ = self.request('/api/v1/auth/login', 'POST', {'username': account['username']})
            self.assertEqual(status, 200)
            token = login['access_token']
            status, identity, _ = self.request('/api/v1/me', token=token)
            self.assertEqual((status, identity['username']), (200, account['username']))
            self.assertEqual(self.request('/api/v1/auth/logout', 'POST', token=token)[0], 204)
            self.assertEqual(self.request('/api/v1/me', token=token)[0], 401)

    def test_only_active_seed_identities_can_skip_password(self):
        with connect(self.path) as connection, connection:
            connection.execute("UPDATE users SET active = 0 WHERE username = 'handler'")
            connection.execute("""INSERT INTO users (id, username, display_name, password_salt, password_hash)
                SELECT 'custom-user', 'custom', 'Custom', password_salt, password_hash
                FROM users WHERE username = 'submitter'""")
            connection.execute("UPDATE users SET username = 'renamed' WHERE username = 'submitter_other'")
            connection.execute("""INSERT INTO users (id, username, display_name, password_salt, password_hash)
                SELECT 'wrong-id', 'submitter_other', 'Wrong ID', password_salt, password_hash
                FROM users WHERE username = 'submitter'""")
        accounts = self.request('/api/v1/auth/test-accounts')[1]['items']
        self.assertEqual(len(accounts), 7)
        for username in ('handler', 'custom', 'submitter_other', 'missing', "' OR 1=1 --"):
            self.assertEqual(self.request('/api/v1/auth/login', 'POST', {'username': username})[0], 401)
        self.assertEqual(self.request('/api/v1/auth/login', 'POST',
                                     {'username': 'submitter', 'role': 'manager'})[0], 422)

    def test_passwordless_session_uses_server_permissions(self):
        status, login, _ = self.request('/api/v1/auth/login', 'POST', {'username': 'submitter'})
        self.assertEqual(status, 200)
        self.assertEqual(self.request('/api/v1/evaluation-statistics', token=login['access_token'])[0], 403)
        status, login, _ = self.request('/api/v1/auth/login', 'POST', {'username': 'dispatcher'})
        self.assertEqual(status, 200)
        token = login['access_token']
        self.assertEqual(self.request('/api/v1/support-groups/test-it', token=token)[0], 200)
        self.assertEqual(self.request('/api/v1/support-groups/test-admin', token=token)[0], 403)
        with connect(self.path) as connection, connection:
            connection.execute("DELETE FROM user_group_scopes WHERE user_id = 'test-dispatcher'")
        self.assertEqual(self.request('/api/v1/support-groups/test-it', token=token)[0], 403)

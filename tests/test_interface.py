from tests.test_foundation import HTTPTestCase


class InterfaceTests(HTTPTestCase):
    def test_interface_assets_and_api_docs_remain_available(self):
        for path, content_type, marker in (
            ('/', 'text/html', 'IT／行政服务台'),
            ('/static/app.js', 'javascript', 'expected_version'),
            ('/static/app.css', 'text/css', '@media'),
        ):
            with self.opener.open(self.url + path, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(content_type, response.headers['Content-Type'])
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
                self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
                self.assertIn(marker, response.read().decode('utf-8'))
        with self.opener.open(self.url + '/', timeout=5) as response:
            html = response.read().decode('utf-8')
            for marker in ('提单人信息', '提报信息', 'intake-submitter', 'intake-department',
                           'intake-email', 'name="location"', '测试基地 A', 'intake-engineer',
                           'intake-attachments', 'save-intake-draft'):
                self.assertIn(marker, html)
        with self.opener.open(self.url + '/docs', timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIsNone(response.headers.get('Content-Security-Policy'))
            self.assertIn('SwaggerUIBundle', response.read().decode('utf-8'))
        self.assertEqual(self.request('/api/v1/tickets')[0], 401)

    def test_static_mount_cannot_serve_application_source(self):
        for path in ('/static/main.py', '/static/../main.py', '/static/%2e%2e/database.py'):
            self.assertEqual(self.request(path)[0], 404)

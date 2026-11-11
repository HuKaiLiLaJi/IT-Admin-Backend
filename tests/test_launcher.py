import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

from app import launcher


class LauncherTests(unittest.TestCase):
    def test_real_start_opens_browser_only_after_http_is_ready(self):
        real_server = launcher.uvicorn.Server
        servers = []
        browser_errors = []

        def server_factory(config):
            server = real_server(config)
            servers.append(server)
            return server

        def open_browser(url):
            try:
                opener = build_opener(ProxyHandler({}))
                with opener.open(url, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn('IT／行政服务台', response.read().decode('utf-8'))
                return True
            except Exception as error:
                browser_errors.append(error)
                return False
            finally:
                servers[0].should_exit = True

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'launcher.sqlite3'
            with patch.dict(os.environ, {'IT_ADMIN_DATABASE': str(path), 'IT_ADMIN_IAM_CONFIG': ''}), \
                    patch.object(launcher, 'PORT', 0), \
                    patch.object(launcher.uvicorn, 'Server', side_effect=server_factory), \
                    patch.object(launcher.webbrowser, 'open', side_effect=open_browser) as opened:
                self.assertEqual(launcher.main(), 0)
                opened.assert_called_once()
                self.assertEqual(browser_errors, [])
                self.assertTrue(path.is_file())

    def test_occupied_port_returns_failure_without_opening_browser(self):
        with socket.socket() as listener:
            listener.bind((launcher.HOST, 0))
            listener.listen()
            with patch.dict(os.environ), patch.object(launcher, 'PORT', listener.getsockname()[1]), \
                    patch.object(launcher.webbrowser, 'open') as opened:
                self.assertEqual(launcher.main(), 1)
                opened.assert_not_called()

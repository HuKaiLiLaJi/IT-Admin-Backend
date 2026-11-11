"""Console launcher for the local service desk."""

import os
import socket
import threading
import webbrowser

import uvicorn

from app.database import PROJECT_ROOT, database_path


HOST = "127.0.0.1"
PORT = 8000


def open_when_ready(server, stopped, url):
    while not stopped.wait(0.1):
        if server.started:
            try:
                opened = webbrowser.open(url)
            except webbrowser.Error:
                opened = False
            if not opened:
                print(f"请在浏览器手动打开 {url}", flush=True)
            return


def main():
    os.environ.setdefault("IT_ADMIN_IAM_PASSWORDLESS", "1")
    os.environ.setdefault("IT_ADMIN_TEST_EMPLOYEE_API", "1")
    config = PROJECT_ROOT / "IAM系统授权信息工单系统.md"
    if config.is_file():
        os.environ.setdefault("IT_ADMIN_IAM_CONFIG", str(config))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            # Reserve the port until Uvicorn takes over this same socket.
            listener.bind((HOST, PORT))
        except OSError as error:
            print(f"无法使用本机端口 {PORT}，请检查是否已开启服务。\n{error}", flush=True)
            return 1
        url = f"http://{HOST}:{listener.getsockname()[1]}/"
        print(f"服务台 {url}\n数据库 {database_path()}\n保留此窗口运行服务，按 Ctrl+C 停止。", flush=True)
        server = uvicorn.Server(uvicorn.Config("app.main:create_app", factory=True, host=HOST, port=PORT))
        stopped = threading.Event()
        browser_thread = threading.Thread(target=open_when_ready, args=(server, stopped, url), daemon=True)
        browser_thread.start()
        try:
            server.run(sockets=[listener])
        except KeyboardInterrupt:
            return 0 if server.started else 1
        finally:
            stopped.set()
            browser_thread.join(timeout=1)
        return 0 if server.started else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""IAM personnel lookup and explicit local account provisioning."""

import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from app.auth import password_key, session_key
from app.database import connect, database_path, initialize_database


CACHE_SECONDS = 3600


class IAMError(Exception):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


@dataclass(frozen=True)
class EmployeeProfile:
    iam_user_id: str
    employee_number: str
    display_name: str
    department: str | None
    email: str | None
    location: str | None
    qywx_id: str | None
    department_code: str | None
    work_title: str | None


def profile_field(record: dict, name: str, limit: int, required: bool = False) -> str | None:
    value = record.get(name)
    if value is None or value == "":
        if required:
            raise IAMError("iam_invalid_response", "IAM 返回的人员资料缺少必要字段")
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise IAMError("iam_invalid_response", "IAM 返回的人员字段格式无效")
    return value


class IAMClient:
    def __init__(self, access_key: str, secret_key: str, url: str):
        address = urlsplit(url)
        if address.scheme != "https" or not address.hostname or address.username or address.password or address.query or address.fragment:
            raise IAMError("iam_configuration", "IAM 接口地址配置无效", 503)
        if not access_key or not secret_key or any(char in access_key + secret_key for char in "\r\n"):
            raise IAMError("iam_configuration", "IAM 凭证配置无效", 503)
        self.access_key = access_key
        self.secret_key = secret_key
        self.url = url
        # This endpoint is on the intranet. Keep TLS verification and avoid desktop proxies.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    @classmethod
    def from_file(cls, path: Path):
        try:
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            raise IAMError("iam_configuration", "无法读取 IAM 授权配置文件", 503) from None
        access = re.search(r"AccessKey\s*[:：]\s*(\S+)", content, re.I)
        secret = re.search(r"SecretKey\s*[:：]\s*(\S+)", content, re.I)
        if not access or not secret:
            raise IAMError("iam_configuration", "IAM 授权文件缺少 AccessKey 或 SecretKey", 503)
        address = re.search(r"https://[^\s<>]+", content)
        if address is None:
            raise IAMError("iam_configuration", "IAM 授权文件缺少 HTTPS 接口地址", 503)
        return cls(access.group(1), secret.group(1), address.group(0))

    @classmethod
    def from_environment(cls):
        config = os.environ.get("IT_ADMIN_IAM_CONFIG")
        return cls.from_file(Path(config)) if config else None

    def lookup(self, *, username: str | None = None, qywx_id: str | None = None) -> EmployeeProfile:
        if (username is None) == (qywx_id is None):
            raise IAMError("iam_invalid_query", "请提供一个工号或企微人员标识", 422)
        value = username if username is not None else qywx_id
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise IAMError("iam_invalid_query", "工号或企微人员标识无效", 422)
        key = "username" if username is not None else "sys_ext_props[qywx_id][eq]"
        nonce = uuid4().hex
        timestamp = int(time.time() * 1000)
        message = f"/v1/usersnonce={nonce}&timestamp={timestamp}{self.secret_key}"
        try:
            digest = hashlib.new("sm3", message.encode("utf-8")).hexdigest()
        except ValueError:
            raise IAMError("iam_configuration", "当前 Python 运行环境不支持 IAM 所需的 SM3 鉴权", 503) from None
        signature = f"{nonce}${timestamp}$v2${self.access_key}$sm3${digest}"
        request = Request(self.url + "?" + urlencode({key: value, "status": "1"}), headers={
            "X-Idmesh-Signature": signature, "Accept": "application/json",
        }, method="GET")
        try:
            with self.opener.open(request, timeout=8) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise IAMError("iam_invalid_response", "IAM 返回内容超过允许大小")
                payload = json.loads(raw.decode("utf-8"))
        except HTTPError as error:
            code = "iam_authentication_failed" if error.code in (401, 403) else "iam_http_error"
            error.close()
            raise IAMError(code, "IAM 接口拒绝请求，请检查授权配置或联系管理员") from None
        except (URLError, TimeoutError, OSError):
            raise IAMError("iam_unavailable", "IAM 暂时无法连接，请稍后重试", 503) from None
        except (ValueError, UnicodeError):
            raise IAMError("iam_invalid_response", "IAM 返回内容格式无效") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise IAMError("iam_invalid_response", "IAM 返回内容格式无效")
        rows = payload["data"]
        total = payload.get("total_num")
        if type(total) is not int or total < 0 or total < len(rows):
            raise IAMError("iam_invalid_response", "IAM 返回的人员数量无效")
        if total == 0 and not rows:
            raise IAMError("iam_no_match", "IAM 未返回符合条件的在职人员，请核对标识或联系管理员", 422)
        if total != 1 or len(rows) != 1:
            raise IAMError("iam_ambiguous", "IAM 返回的人员结果不唯一，请联系管理员核实", 409)
        record = rows[0]
        if not isinstance(record, dict) or record.get("status") != "1":
            raise IAMError("iam_invalid_response", "IAM 返回的人员状态无效")
        props = record.get("sys_ext_props") or {}
        if not isinstance(props, dict):
            raise IAMError("iam_invalid_response", "IAM 返回的扩展资料无效")
        profile = EmployeeProfile(
            iam_user_id=profile_field(record, "id", 128, True),
            employee_number=profile_field(record, "username", 64, True),
            display_name=profile_field(record, "display_name", 200, True),
            department=profile_field(props, "org_name", 200),
            email=profile_field(record, "email", 320), location=profile_field(record, "region", 200),
            qywx_id=profile_field(props, "qywx_id", 256),
            department_code=profile_field(props, "org_code", 200),
            work_title=profile_field(record, "work_title", 200),
        )
        if (username is not None and profile.employee_number != username) or (qywx_id is not None and profile.qywx_id != qywx_id):
            raise IAMError("iam_mismatched_identity", "IAM 返回人员与查询标识不匹配，请联系管理员核实", 409)
        return profile


def save_profile(connection: sqlite3.Connection, profile: EmployeeProfile, *, user_id: str | None = None,
                 password: str | None = None) -> str:
    """Caller owns the write transaction. Existing identities are never merged by name."""
    mapping = connection.execute("SELECT * FROM iam_profiles WHERE iam_user_id = ?", (profile.iam_user_id,)).fetchone()
    if user_id is not None and (mapping is None or mapping["user_id"] != user_id):
        raise IAMError("iam_identity_conflict", "IAM 人员身份与已有账号绑定不一致", 409)
    if mapping is not None:
        user_id = mapping["user_id"]
    elif password is None or not 12 <= len(password) <= 256:
        raise IAMError("iam_password_required", "新建人员账号需要设置12～256字符的登录密码", 422)
    else:
        user_id = f"iam-{uuid4()}"
        salt = secrets.token_bytes(16)
        connection.execute(
            """INSERT INTO users (id, username, display_name, password_salt, password_hash)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, profile.employee_number, profile.display_name, salt, password_key(password, salt)),
        )
        connection.execute("INSERT INTO roles (code, name) VALUES ('submitter', '提交人') ON CONFLICT DO NOTHING")
        connection.execute("INSERT INTO user_roles (user_id, role) VALUES (?, 'submitter')", (user_id,))
    connection.execute(
        """UPDATE users SET username = ?, display_name = ?, department = ?, email = ?, location = ? WHERE id = ?""",
        (profile.employee_number, profile.display_name, profile.department, profile.email, profile.location, user_id),
    )
    connection.execute(
        """INSERT INTO iam_profiles (user_id, iam_user_id, employee_number, qywx_id, department_code, work_title, refreshed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET
           employee_number = excluded.employee_number, qywx_id = excluded.qywx_id,
           department_code = excluded.department_code, work_title = excluded.work_title,
           refreshed_at = excluded.refreshed_at""",
        (user_id, profile.iam_user_id, profile.employee_number, profile.qywx_id,
         profile.department_code, profile.work_title, int(time.time())),
    )
    return user_id


def profile_status(connection: sqlite3.Connection, user_id: str, enabled: bool) -> dict | None:
    row = connection.execute("SELECT * FROM iam_profiles WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    return {"employee_number": row["employee_number"], "refreshed_at": row["refreshed_at"],
            "stale": int(time.time()) - row["refreshed_at"] >= CACHE_SECONDS, "refresh_available": enabled}


def refresh_profile(connection: sqlite3.Connection, user_id: str, client: IAMClient | None, *,
                    force: bool = False, session_token: str | None = None):
    mapping = connection.execute("SELECT * FROM iam_profiles WHERE user_id = ?", (user_id,)).fetchone()
    if mapping is None:
        raise IAMError("iam_not_linked", "当前账号尚未关联 IAM 人员", 409)
    if client is None:
        raise IAMError("iam_not_configured", "当前服务尚未配置 IAM 连接", 503)
    if not force and int(time.time()) - mapping["refreshed_at"] < CACHE_SECONDS:
        return
    profile = client.lookup(username=mapping["employee_number"])
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT active FROM users WHERE id = ?", (user_id,)).fetchone()
            if current is None or not current["active"]:
                raise IAMError("unauthenticated", "当前账号已停用，请重新登录", 401)
            if session_token is not None and connection.execute(
                "SELECT 1 FROM sessions WHERE token_hash = ? AND user_id = ? AND expires_at > ?",
                (session_key(session_token), user_id, int(time.time())),
            ).fetchone() is None:
                raise IAMError("unauthenticated", "登录凭证无效或已过期", 401)
            save_profile(connection, profile, user_id=user_id)
    except sqlite3.IntegrityError:
        raise IAMError("iam_identity_conflict", "人员标识与已有账号冲突，请联系管理员核实", 409) from None


def main():
    parser = argparse.ArgumentParser(description="本地 IAM 人员查询和导入")
    parser.add_argument("action", choices=("lookup", "import-user"))
    query = parser.add_mutually_exclusive_group(required=True)
    query.add_argument("--username")
    query.add_argument("--qywx-id")
    parser.add_argument("--config", type=Path, help="IAM 授权 Markdown 文件，或设置 IT_ADMIN_IAM_CONFIG")
    parser.add_argument("--database", type=Path, help="目标数据库，默认使用 IT_ADMIN_DATABASE 或项目默认库")
    args = parser.parse_args()
    try:
        client = IAMClient.from_file(args.config) if args.config else IAMClient.from_environment()
        if client is None:
            raise IAMError("iam_not_configured", "请指定 --config 或设置 IT_ADMIN_IAM_CONFIG", 503)
        profile = client.lookup(username=args.username, qywx_id=args.qywx_id)
        if args.action == "lookup":
            print(json.dumps(asdict(profile), ensure_ascii=False, indent=2))
            return
        target = args.database.resolve() if args.database else database_path()
        initialize_database(target)
        with connect(target) as connection:
            exists = connection.execute("SELECT 1 FROM iam_profiles WHERE iam_user_id = ?", (profile.iam_user_id,)).fetchone()
            password = None
            if exists is None:
                password = getpass.getpass("设置该员工登录密码（12～256字符）: ")
                if password != getpass.getpass("再次输入密码: "):
                    raise IAMError("iam_password_mismatch", "两次输入的密码不一致", 422)
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                user_id = save_profile(connection, profile, password=password)
            print(f"人员资料已保存，工号 {profile.employee_number}，内部账号 {user_id}，数据库 {target}")
    except IAMError as error:
        parser.exit(1, f"{error.code}: {error.message}\n")
    except sqlite3.IntegrityError:
        parser.exit(1, "人员标识与已有账号冲突，未保存本次人员变更。\n")


if __name__ == "__main__":
    main()

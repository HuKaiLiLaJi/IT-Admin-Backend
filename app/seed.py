"""Explicit, non-destructive local test data initialization."""

import secrets

from app.auth import password_key
from app.database import connect, database_path, initialize_database


TEST_PASSWORD = "LocalTest-2026!"
ROLES = (
    ("submitter", "提交人"), ("dispatcher", "分派人员"), ("handler", "处理人"),
    ("manager", "管理者／测试主管"), ("config_admin", "配置管理员"),
)
GROUPS = (("test-it", "测试 IT 组"), ("test-admin", "测试行政组"))
SYSTEMS = (("test-office", "测试办公系统", "test-it"), ("test-facility", "测试行政设施", "test-admin"))
USERS = (
    ("submitter", "测试提交人", "submitter", None),
    ("submitter_other", "另一测试提交人", "submitter", None),
    ("dispatcher", "测试分派人员", "dispatcher", "test-it"),
    ("dispatcher_admin", "测试行政分派人员", "dispatcher", "test-admin"),
    ("handler", "测试 IT 处理人", "handler", "test-it"),
    ("handler_backup", "测试 IT 备用处理人", "handler", "test-it"),
    ("handler_other", "测试行政处理人", "handler", "test-admin"),
    ("supervisor", "测试主管", "manager", "test-it"),
    ("config_admin", "测试配置管理员", "config_admin", "test-it"),
)


def seed_test_data(path) -> int:
    initialize_database(path)
    inserted = 0
    with connect(path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany("INSERT INTO roles (code, name) VALUES (?, ?) ON CONFLICT(code) DO NOTHING", ROLES)
        connection.executemany("INSERT INTO support_groups (id, name) VALUES (?, ?) ON CONFLICT(id) DO NOTHING", GROUPS)
        connection.executemany(
            "INSERT INTO service_systems (id, name, group_id) VALUES (?, ?, ?) ON CONFLICT(id) DO NOTHING", SYSTEMS,
        )
        for username, display_name, role, group_id in USERS:
            # Existing accounts retain passwords, role assignments, scopes and active state.
            if connection.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                continue
            user_id = f"test-{username}"
            salt = secrets.token_bytes(16)
            connection.execute(
                """INSERT INTO users (id, username, display_name, password_salt, password_hash, department)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, username, display_name, salt, password_key(TEST_PASSWORD, salt), "测试部门"),
            )
            connection.execute("INSERT INTO user_roles (user_id, role) VALUES (?, 'submitter')", (user_id,))
            if role != "submitter":
                connection.execute("INSERT INTO user_roles (user_id, role) VALUES (?, ?)", (user_id, role))
            if group_id is not None:
                connection.execute(
                    "INSERT INTO user_group_scopes (user_id, role, group_id) VALUES (?, ?, ?)",
                    (user_id, role, group_id),
                )
            inserted += 1
    return inserted


if __name__ == "__main__":
    path = database_path()
    count = seed_test_data(path)
    print(f"Created {count} local test accounts. Database: {path}")

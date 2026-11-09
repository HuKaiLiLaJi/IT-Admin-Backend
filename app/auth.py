import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.database import connect


PASSWORD_ITERATIONS = 600_000
SESSION_SECONDS = 8 * 60 * 60
bearer = HTTPBearer(auto_error=False)


def password_key(password: str, salt: bytes) -> bytes:
    """Derive a login credential key, independent of file/content verification."""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)


def session_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "unauthenticated", "message": "登录凭证无效或已过期"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def forbidden() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={"code": "forbidden", "message": "当前账号没有所需权限"},
    )


def get_connection(request: Request):
    with connect(request.app.state.database_path) as connection:
        yield connection


Database = Annotated[sqlite3.Connection, Depends(get_connection)]


@dataclass(frozen=True)
class Principal:
    id: str
    username: str
    display_name: str
    department: str | None
    email: str | None
    location: str | None
    roles: tuple[str, ...]
    group_scopes: dict[str, tuple[str, ...]]


def get_current_user(
    connection: Database,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    if credentials is None:
        raise unauthorized()
    user = connection.execute(
        """SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id
           WHERE token_hash = ? AND expires_at > ? AND users.active = 1""",
        (session_key(credentials.credentials), int(time.time())),
    ).fetchone()
    if user is None:
        raise unauthorized()
    roles = tuple(row[0] for row in connection.execute(
        "SELECT role FROM user_roles WHERE user_id = ? ORDER BY role", (user["id"],)
    ))
    scopes: dict[str, list[str]] = {}
    for row in connection.execute(
        """SELECT scopes.role, scopes.group_id FROM user_group_scopes AS scopes
           JOIN support_groups ON support_groups.id = scopes.group_id
           WHERE scopes.user_id = ? AND support_groups.active = 1
           ORDER BY scopes.role, scopes.group_id""",
        (user["id"],),
    ):
        scopes.setdefault(row["role"], []).append(row["group_id"])
    return Principal(
        id=user["id"], username=user["username"], display_name=user["display_name"],
        department=user["department"], email=user["email"], location=user["location"],
        roles=roles, group_scopes={role: tuple(groups) for role, groups in scopes.items()},
    )


CurrentUser = Annotated[Principal, Depends(get_current_user)]


def require_role(role: str):
    def check(user: CurrentUser) -> Principal:
        if role not in user.roles:
            raise forbidden()
        return user

    return check


def require_group_scope(user: Principal, role: str, group_id: str) -> None:
    if role not in user.roles or group_id not in user.group_scopes.get(role, ()):
        raise forbidden()

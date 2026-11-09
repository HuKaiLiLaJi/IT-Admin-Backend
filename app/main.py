import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from app.auth import (
    SESSION_SECONDS, CurrentUser, Database, Principal, bearer, password_key,
    forbidden, require_group_scope, require_role, session_key, unauthorized,
)
from app.database import database_path, initialize_database
from app.seed import USERS
from app.tickets import router as tickets_router


TEST_USERNAMES = tuple(user[0] for user in USERS)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    username: str = Field(min_length=1, max_length=64)
    password: str | None = Field(default=None, min_length=1, max_length=256)


def create_app(path: Path | None = None) -> FastAPI:
    selected_path = path if path is not None else database_path()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        initialize_database(selected_path)
        yield

    application = FastAPI(title="内部 IT／行政工单后端", version="0.1.0", lifespan=lifespan)
    application.state.database_path = selected_path
    application.include_router(tickets_router)
    static_directory = Path(__file__).with_name("static")
    application.mount("/static", StaticFiles(directory=static_directory), name="static")

    @application.get("/", include_in_schema=False)
    def local_interface():
        return FileResponse(static_directory / "index.html")

    @application.middleware("http")
    async def prevent_auth_caching(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            )
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # Validation responses must not echo submitted credentials.
        fields = [
            {"loc": item["loc"], "type": item["type"], "msg": item["msg"]}
            for item in error.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": {
            "code": "invalid_request", "message": "请求字段无效", "fields": fields,
        }})

    @application.exception_handler(sqlite3.OperationalError)
    async def database_unavailable(request: Request, error: sqlite3.OperationalError):
        return JSONResponse(status_code=503, content={"detail": {
            "code": "storage_unavailable", "message": "数据库暂时不可用，请稍后重试",
        }})

    @application.get("/health")
    def health(connection: Database):
        connection.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @application.get("/api/v1/auth/test-accounts")
    def test_accounts(connection: Database):
        placeholders = ', '.join('?' for _ in TEST_USERNAMES)
        rows = connection.execute(
            f"""SELECT username, display_name FROM users
                WHERE active = 1 AND username IN ({placeholders})
                AND id = 'test-' || username ORDER BY username""", TEST_USERNAMES,
        ).fetchall()
        return {"items": [dict(row) for row in rows]}

    @application.post("/api/v1/auth/login")
    def login(body: LoginRequest, connection: Database):
        user = connection.execute("SELECT * FROM users WHERE username = ?", (body.username,)).fetchone()
        if body.password is None:
            matches = user is not None and body.username in TEST_USERNAMES and user["id"] == f"test-{body.username}"
        else:
            salt = user["password_salt"] if user is not None else bytes(16)
            expected = user["password_hash"] if user is not None else bytes(32)
            matches = secrets.compare_digest(password_key(body.password, salt), expected)
        if not matches or user is None or not user["active"]:
            raise unauthorized()
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (session_key(token), user["id"], now, now + SESSION_SECONDS),
            )
        return {"access_token": token, "token_type": "bearer", "expires_in": SESSION_SECONDS}

    @application.post("/api/v1/auth/logout", status_code=204)
    def logout(
        user: CurrentUser,
        connection: Database,
        credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    ):
        with connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (session_key(credentials.credentials),))
        return Response(status_code=204)

    @application.get("/api/v1/me")
    def me(user: CurrentUser):
        return asdict(user)

    @application.get("/api/v1/service-systems")
    def service_systems(user: CurrentUser, connection: Database):
        rows = connection.execute(
            """SELECT systems.id, systems.name, systems.group_id FROM service_systems AS systems
               JOIN support_groups AS groups ON groups.id = systems.group_id
               WHERE systems.active = 1 AND groups.active = 1 ORDER BY systems.id"""
        ).fetchall()
        return {"items": [dict(row) for row in rows]}

    @application.get("/api/v1/support-groups")
    def support_groups(
        user: Annotated[Principal, Depends(require_role("dispatcher"))], connection: Database,
    ):
        rows = connection.execute("SELECT id, name FROM support_groups WHERE active = 1 ORDER BY id").fetchall()
        groups = user.group_scopes.get("dispatcher", ())
        return {"items": [dict(row) for row in rows if row["id"] in groups]}

    @application.get("/api/v1/support-groups/{group_id}")
    def support_group(
        group_id: str, user: Annotated[Principal, Depends(require_role("dispatcher"))], connection: Database,
    ):
        require_group_scope(user, "dispatcher", group_id)
        row = connection.execute("SELECT id, name FROM support_groups WHERE id = ? AND active = 1", (group_id,)).fetchone()
        if row is None:
            raise forbidden()
        return dict(row)

    return application


app = create_app()

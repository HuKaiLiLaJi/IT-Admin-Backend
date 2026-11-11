import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "backend.sqlite3"
SCHEMA_VERSION = 8

SCHEMA = (
    """CREATE TABLE users (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        password_salt BLOB NOT NULL,
        password_hash BLOB NOT NULL,
        department TEXT,
        email TEXT,
        location TEXT,
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
    )""",
    """CREATE TABLE roles (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL
    )""",
    """CREATE TABLE user_roles (
        user_id TEXT NOT NULL REFERENCES users(id),
        role TEXT NOT NULL REFERENCES roles(code),
        PRIMARY KEY (user_id, role)
    )""",
    """CREATE TABLE support_groups (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
    )""",
    """CREATE TABLE user_group_scopes (
        user_id TEXT NOT NULL,
        role TEXT NOT NULL,
        group_id TEXT NOT NULL REFERENCES support_groups(id),
        PRIMARY KEY (user_id, role, group_id),
        FOREIGN KEY (user_id, role) REFERENCES user_roles(user_id, role)
    )""",
    """CREATE TABLE service_systems (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        group_id TEXT NOT NULL REFERENCES support_groups(id),
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
    )""",
    """CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id),
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL CHECK (expires_at > created_at)
    )""",
    "CREATE INDEX sessions_expiry ON sessions(expires_at)",
)


TICKET_SCHEMA = (
    """CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        submitter_id TEXT NOT NULL REFERENCES users(id),
        previous_ticket_id TEXT REFERENCES tickets(id),
        created_at INTEGER NOT NULL,
        UNIQUE (id, submitter_id)
    )""",
    """CREATE TABLE tickets (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL UNIQUE,
        submitter_id TEXT NOT NULL REFERENCES users(id),
        service_system_id TEXT NOT NULL REFERENCES service_systems(id),
        group_id TEXT NOT NULL REFERENCES support_groups(id),
        title TEXT NOT NULL CHECK (length(trim(title)) > 0),
        description TEXT NOT NULL CHECK (length(trim(description)) > 0),
        department TEXT,
        email TEXT,
        location TEXT,
        issue_id TEXT,
        status TEXT NOT NULL DEFAULT 'pending_assignment' CHECK (status IN (
            'pending_assignment', 'pending_acceptance', 'in_progress',
            'awaiting_confirmation', 'closed', 'cancelled'
        )),
        assignee_id TEXT REFERENCES users(id),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        closed_at INTEGER,
        FOREIGN KEY (conversation_id, submitter_id) REFERENCES conversations(id, submitter_id)
    )""",
    """CREATE TABLE ticket_history (
        id INTEGER PRIMARY KEY,
        ticket_id TEXT NOT NULL REFERENCES tickets(id),
        action TEXT NOT NULL,
        actor_id TEXT NOT NULL REFERENCES users(id),
        from_status TEXT,
        to_status TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )""",
    "CREATE INDEX tickets_submitter ON tickets(submitter_id, created_at, id)",
    "CREATE INDEX tickets_group ON tickets(group_id, created_at, id)",
    "CREATE INDEX tickets_assignee ON tickets(assignee_id, created_at, id)",
    "CREATE INDEX ticket_history_ticket ON ticket_history(ticket_id, id)",
    "CREATE INDEX conversations_previous ON conversations(previous_ticket_id)",
)


RESOLUTION_SCHEMA = (
    "ALTER TABLE tickets ADD COLUMN result_round INTEGER NOT NULL DEFAULT 0 CHECK (result_round >= 0)",
    "ALTER TABLE tickets ADD COLUMN result_submitted_at INTEGER",
    "ALTER TABLE tickets ADD COLUMN confirmed_at INTEGER",
    "ALTER TABLE tickets ADD COLUMN cancelled_at INTEGER",
    "ALTER TABLE ticket_history ADD COLUMN result_round INTEGER",
    "ALTER TABLE ticket_history ADD COLUMN cancellation_request_id TEXT",
    """CREATE TABLE ticket_results (
        ticket_id TEXT NOT NULL REFERENCES tickets(id),
        round INTEGER NOT NULL CHECK (round > 0),
        handler_id TEXT NOT NULL REFERENCES users(id),
        content TEXT NOT NULL CHECK (length(trim(content)) > 0),
        submitted_at INTEGER NOT NULL,
        confirmation TEXT CHECK (confirmation IN ('resolved', 'unresolved')),
        confirmed_by TEXT REFERENCES users(id),
        confirmed_at INTEGER,
        PRIMARY KEY (ticket_id, round)
    )""",
    """CREATE TABLE cancellation_requests (
        sequence INTEGER PRIMARY KEY,
        id TEXT NOT NULL UNIQUE,
        ticket_id TEXT NOT NULL REFERENCES tickets(id),
        requested_by TEXT NOT NULL REFERENCES users(id),
        reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
        requested_at INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'approved', 'rejected', 'superseded')),
        decided_by TEXT REFERENCES users(id),
        decision_reason TEXT,
        decided_at INTEGER
    )""",
    "CREATE UNIQUE INDEX cancellation_pending ON cancellation_requests(ticket_id) WHERE status = 'pending'",
    "CREATE INDEX cancellation_ticket ON cancellation_requests(ticket_id, requested_at, id)",
)


EVALUATION_SCHEMA = (
    """CREATE TABLE ticket_evaluations (
        ticket_id TEXT PRIMARY KEY REFERENCES tickets(id),
        rating TEXT NOT NULL CHECK (rating IN ('good', 'neutral', 'bad')),
        reason TEXT,
        source TEXT NOT NULL CHECK (source IN ('active', 'automatic')),
        actor_id TEXT REFERENCES users(id),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        CHECK (rating = 'good' OR length(trim(reason)) > 0 AND reason IS NOT NULL),
        CHECK ((source = 'active' AND actor_id IS NOT NULL) OR
               (source = 'automatic' AND actor_id IS NULL AND rating = 'good'))
    )""",
    """CREATE TABLE evaluation_history (
        id INTEGER PRIMARY KEY,
        ticket_id TEXT NOT NULL REFERENCES tickets(id),
        rating TEXT NOT NULL CHECK (rating IN ('good', 'neutral', 'bad')),
        reason TEXT,
        source TEXT NOT NULL CHECK (source IN ('active', 'automatic')),
        actor_id TEXT REFERENCES users(id),
        ticket_version INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    )""",
    "CREATE INDEX evaluation_history_ticket ON evaluation_history(ticket_id, id)",
    "CREATE INDEX tickets_closed ON tickets(closed_at) WHERE status = 'closed'",
)


ATTACHMENT_SCHEMA = (
    """CREATE TABLE attachments (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL REFERENCES users(id),
        ticket_id TEXT REFERENCES tickets(id),
        file_name TEXT NOT NULL,
        content_type TEXT NOT NULL,
        size INTEGER NOT NULL CHECK (size > 0 AND size <= 10485760),
        content BLOB NOT NULL CHECK (length(content) = size),
        created_at INTEGER NOT NULL
    )""",
    "CREATE INDEX attachments_ticket ON attachments(ticket_id, created_at, id)",
)


IAM_SCHEMA = (
    """CREATE TABLE iam_profiles (
        user_id TEXT PRIMARY KEY REFERENCES users(id),
        iam_user_id TEXT NOT NULL UNIQUE,
        employee_number TEXT NOT NULL UNIQUE,
        qywx_id TEXT UNIQUE,
        department_code TEXT,
        work_title TEXT,
        refreshed_at INTEGER NOT NULL
    )""",
)


def database_path() -> Path:
    configured = Path(os.environ.get("IT_ADMIN_DATABASE", DEFAULT_DATABASE_PATH))
    if not configured.is_absolute():
        configured = PROJECT_ROOT / configured
    return configured.resolve()


@contextmanager
def connect(path: Path):
    connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
    finally:
        connection.close()


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("Database schema is newer than this application")
        if version == 0:
            for statement in SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 1")
        if version < 2:
            for statement in TICKET_SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 2")
        if version < 3:
            connection.execute("ALTER TABLE tickets ADD COLUMN assigned_at INTEGER")
            connection.execute("ALTER TABLE tickets ADD COLUMN accepted_at INTEGER")
            connection.execute("ALTER TABLE ticket_history ADD COLUMN from_assignee_id TEXT REFERENCES users(id)")
            connection.execute("ALTER TABLE ticket_history ADD COLUMN to_assignee_id TEXT REFERENCES users(id)")
            connection.execute("ALTER TABLE ticket_history ADD COLUMN note TEXT")
            connection.execute("PRAGMA user_version = 3")
        if version < 4:
            for statement in RESOLUTION_SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 4")
        if version < 5:
            for statement in EVALUATION_SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 5")
        if version < 6:
            for statement in ATTACHMENT_SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 6")
        if version < 7:
            for statement in IAM_SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 7")
        if version < 8:
            connection.execute("ALTER TABLE tickets ADD COLUMN priority TEXT CHECK (priority IN ('P1', 'P3'))")
            connection.execute("PRAGMA user_version = 8")

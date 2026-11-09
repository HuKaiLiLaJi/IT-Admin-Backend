"""Evaluation persistence and a retryable local expiration command."""

import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.database import connect, database_path, initialize_database


BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")


def evaluation_deadline(closed_at: int) -> int:
    closed_day = datetime.fromtimestamp(closed_at, BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((closed_day + timedelta(days=8)).timestamp())


def expiration_cutoff(now: int) -> int:
    today = datetime.fromtimestamp(now, BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((today - timedelta(days=7)).timestamp())


def save_evaluation(connection, ticket, rating, reason, source, actor_id, now):
    # Caller owns BEGIN IMMEDIATE. Persist version, current result and audit atomically.
    changed = connection.execute(
        """UPDATE tickets SET version = version + 1, updated_at = ?
           WHERE id = ? AND version = ?""", (now, ticket['id'], ticket['version']),
    )
    if changed.rowcount != 1:
        raise HTTPException(status_code=409, detail={
            'code': 'version_conflict', 'message': '工单已更新，请重新读取后再操作',
        })
    connection.execute(
        """INSERT INTO ticket_evaluations
           (ticket_id, rating, reason, source, actor_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(ticket_id) DO UPDATE SET rating = excluded.rating, reason = excluded.reason,
               source = excluded.source, actor_id = excluded.actor_id, updated_at = excluded.updated_at""",
        (ticket['id'], rating, reason, source, actor_id, now, now),
    )
    connection.execute(
        """INSERT INTO evaluation_history
           (ticket_id, rating, reason, source, actor_id, ticket_version, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ticket['id'], rating, reason, source, actor_id, ticket['version'] + 1, now),
    )


def expire_evaluations(path) -> int:
    """Commit batches independently. A failed batch can be retried without duplicates."""
    total = 0
    with connect(path) as connection:
        while True:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                now = int(time.time())
                tickets = connection.execute(
                    """SELECT t.* FROM tickets t LEFT JOIN ticket_evaluations e ON e.ticket_id = t.id
                       WHERE t.status = 'closed' AND t.closed_at < ? AND e.ticket_id IS NULL
                       ORDER BY t.closed_at, t.id LIMIT 100""", (expiration_cutoff(now),),
                ).fetchall()
                for ticket in tickets:
                    save_evaluation(connection, ticket, 'good', None, 'automatic', None, now)
            total += len(tickets)
            if not tickets:
                return total


if __name__ == '__main__':
    path = database_path()
    initialize_database(path)
    print(f'Created {expire_evaluations(path)} automatic evaluations. Database: {path}')

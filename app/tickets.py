"""Local-test conversations, ticket intake and resource-scoped queries."""

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated
from typing import Literal
from uuid import uuid4
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.auth import (
    CurrentUser, Database, Principal, bearer, forbidden, get_current_user,
    require_group_scope, require_role,
)
from app.evaluations import evaluation_deadline, expiration_cutoff, save_evaluation
from app.attachments import FILE_COLUMNS, MAX_FILES, bind_files, read_uploads, ticket_files


router = APIRouter(prefix="/api/v1")
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
TicketScope = Literal['visible', 'mine']
BEIJING_TIME = timezone(timedelta(hours=8))


class ConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    previous_ticket_id: Identifier | None = None


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    service_system_id: Identifier
    title: Title
    description: Description
    location: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    file_ids: list[Identifier] = Field(default_factory=list, max_length=MAX_FILES)


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_version: int = Field(ge=1)


class AssignmentRequest(ActionRequest):
    assignee_id: Identifier


class AttachmentRequest(ActionRequest):
    file_ids: list[Identifier] = Field(min_length=1, max_length=MAX_FILES)


class NoteRequest(ActionRequest):
    note: Description


class ReassignmentRequest(AssignmentRequest):
    reason: Description


class ResultRequest(ActionRequest):
    result: Description


class ConfirmationRequest(ActionRequest):
    result_round: int = Field(ge=1)
    outcome: Literal['resolved', 'unresolved']
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=10000)] | None = None


class CancellationRequest(ActionRequest):
    reason: Description


class CancellationDecisionRequest(CancellationRequest):
    decision: Literal['approved', 'rejected']


class EvaluationRequest(ActionRequest):
    rating: Literal['good', 'neutral', 'bad']
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=10000)] | None = None


def mutation_user(
    connection: Database,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    # Authorize after taking the write lock. Commit before the HTTP response is sent.
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        yield get_current_user(connection, credentials)


ActionUser = Annotated[Principal, Depends(mutation_user, scope="function")]


def mutation_submitter(user: ActionUser):
    if "submitter" not in user.roles:
        raise forbidden()
    return user


Submitter = Annotated[Principal, Depends(mutation_submitter)]


def missing_resource() -> HTTPException:
    return HTTPException(status_code=404, detail={
        "code": "not_found", "message": "记录不存在或不可访问",
    })


def check_previous_ticket(connection: sqlite3.Connection, ticket_id: str | None, user_id: str) -> None:
    if ticket_id is None:
        return
    previous = connection.execute(
        "SELECT status FROM tickets WHERE id = ? AND submitter_id = ?", (ticket_id, user_id),
    ).fetchone()
    if previous is None:
        raise missing_resource()
    if previous["status"] not in ("closed", "cancelled"):
        raise HTTPException(status_code=409, detail={
            "code": "previous_ticket_not_terminal", "message": "关联旧单须为已关闭或已撤销",
        })


def ticket_visibility(user: Principal, scope: TicketScope = 'visible') -> tuple[str, list[str]]:
    if scope == 'mine':
        if 'submitter' not in user.roles:
            raise forbidden()
        return "t.submitter_id = ?", [user.id]
    clauses = ["0"]
    values = []
    if "submitter" in user.roles:
        clauses.append("t.submitter_id = ?")
        values.append(user.id)
    if "handler" in user.roles:
        clauses.append("t.assignee_id = ?")
        values.append(user.id)
    groups = user.group_scopes.get("dispatcher", ())
    if "dispatcher" in user.roles and groups:
        placeholders = ", ".join("?" for _ in groups)
        clauses.append(f"t.group_id IN ({placeholders})")
        values.extend(groups)
    return "(" + " OR ".join(clauses) + ")", values


# Keep the old-ticket link in the conversation, shared by detail and list responses.
TICKET_SELECT = """SELECT t.*, t.id AS ticket_id, s.name AS system,
                   p.employee_number AS engineer_employee_no, c.previous_ticket_id,
                   (t.accepted_at IS NOT NULL OR t.result_round > 0
                    OR t.status IN ('in_progress', 'awaiting_confirmation') OR EXISTS (
                        SELECT 1 FROM ticket_history h WHERE h.ticket_id = t.id
                        AND (h.from_status IN ('in_progress', 'awaiting_confirmation')
                             OR h.to_status IN ('in_progress', 'awaiting_confirmation'))
                    )) AS cancellation_requires_approval FROM tickets t
                   JOIN conversations c ON c.id = t.conversation_id
                   LEFT JOIN service_systems s ON s.id = t.service_system_id
                   LEFT JOIN iam_profiles p ON p.user_id = t.assignee_id"""


def ticket_response(row: sqlite3.Row) -> dict:
    result = dict(row)
    for field in ('created_at', 'updated_at'):
        result[field + '_iso'] = datetime.fromtimestamp(result[field], BEIJING_TIME).isoformat()
    return result


@router.post("/conversations", status_code=201)
def create_conversation(body: ConversationRequest, user: Submitter, connection: Database):
    conversation_id = str(uuid4())
    now = int(time.time())
    check_previous_ticket(connection, body.previous_ticket_id, user.id)
    connection.execute(
        "INSERT INTO conversations (id, submitter_id, previous_ticket_id, created_at) VALUES (?, ?, ?, ?)",
        (conversation_id, user.id, body.previous_ticket_id, now),
    )
    return {"id": conversation_id, "submitter_id": user.id,
            "previous_ticket_id": body.previous_ticket_id, "created_at": now, "ticket_id": None}


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, user: CurrentUser, connection: Database):
    row = connection.execute(
        """SELECT c.*, t.id AS ticket_id FROM conversations c
           LEFT JOIN tickets t ON t.conversation_id = c.id
           WHERE c.id = ? AND c.submitter_id = ?""", (conversation_id, user.id),
    ).fetchone()
    if row is None:
        raise missing_resource()
    return dict(row)


@router.post("/conversations/{conversation_id}/ticket", status_code=201)
def create_ticket(
    conversation_id: str, body: TicketRequest, response: Response, user: Submitter, connection: Database,
):
    conversation = connection.execute(
        "SELECT * FROM conversations WHERE id = ? AND submitter_id = ?", (conversation_id, user.id),
    ).fetchone()
    if conversation is None:
        raise missing_resource()
    existing = connection.execute(
        TICKET_SELECT + " WHERE t.conversation_id = ?", (conversation_id,),
    ).fetchone()
    if existing is not None:
        response.status_code = 200
        return {**ticket_response(existing), "files": ticket_files(connection, existing["id"])}
    check_previous_ticket(connection, conversation["previous_ticket_id"], user.id)
    system = connection.execute(
        """SELECT s.group_id FROM service_systems s JOIN support_groups g ON g.id = s.group_id
           WHERE s.id = ? AND s.active = 1 AND g.active = 1""", (body.service_system_id,),
    ).fetchone()
    if system is None:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_service_system", "message": "请选择有效的测试运维系统",
        })
    ticket_id = str(uuid4())
    now = int(time.time())
    connection.execute(
        """INSERT INTO tickets (id, conversation_id, submitter_id, service_system_id, group_id,
           title, description, department, email, location, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticket_id, conversation_id, user.id, body.service_system_id, system["group_id"],
         body.title, body.description, user.department, user.email, body.location or user.location, now, now),
    )
    connection.execute(
        """INSERT INTO ticket_history (ticket_id, action, actor_id, from_status, to_status, created_at)
           VALUES (?, 'created', ?, NULL, 'pending_assignment', ?)""", (ticket_id, user.id, now),
    )
    bind_files(connection, body.file_ids, user.id, ticket_id)
    result = ticket_response(connection.execute(TICKET_SELECT + " WHERE t.id = ?", (ticket_id,)).fetchone())
    return {**result, "files": ticket_files(connection, ticket_id)}


@router.get("/tickets")
def list_tickets(
    user: CurrentUser, connection: Database,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[str | None, Query(max_length=200)] = None,
    status: Literal['pending_assignment', 'pending_acceptance', 'in_progress',
                    'awaiting_confirmation', 'closed', 'cancelled'] | None = None,
    service_system_id: Annotated[str | None, Query(max_length=128)] = None,
    scope: TicketScope = 'visible',
):
    predicate, values = ticket_visibility(user, scope)
    if q and q.strip():
        predicate += """ AND (instr(lower(t.title), lower(?)) > 0 OR t.id = ?
                        OR instr(lower(t.description), lower(?)) > 0 OR EXISTS (
                            SELECT 1 FROM service_systems search_system
                            WHERE search_system.id = t.service_system_id
                            AND instr(lower(search_system.name), lower(?)) > 0))"""
        values.extend([q.strip()] * 4)
    if status is not None:
        predicate += " AND t.status = ?"
        values.append(status)
    if service_system_id is not None:
        predicate += " AND t.service_system_id = ?"
        values.append(service_system_id)
    with connection:
        connection.execute("BEGIN")
        total = connection.execute("SELECT COUNT(*) FROM tickets t WHERE " + predicate, values).fetchone()[0]
        rows = connection.execute(
            TICKET_SELECT + " WHERE " + predicate + " ORDER BY t.created_at DESC, t.id DESC LIMIT ? OFFSET ?",
            [*values, limit, offset],
        ).fetchall()
    return {"items": [ticket_response(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str, user: CurrentUser, connection: Database, scope: TicketScope = 'visible'):
    predicate, values = ticket_visibility(user, scope)
    with connection:
        connection.execute("BEGIN")
        row = connection.execute(TICKET_SELECT + " WHERE t.id = ? AND " + predicate, [ticket_id, *values]).fetchone()
        if row is None:
            raise missing_resource()
        history = connection.execute(
            "SELECT * FROM ticket_history WHERE ticket_id = ? ORDER BY id", (ticket_id,),
        ).fetchall()
        results = connection.execute(
            "SELECT * FROM ticket_results WHERE ticket_id = ? ORDER BY round", (ticket_id,),
        ).fetchall()
        cancellations = connection.execute(
            "SELECT * FROM cancellation_requests WHERE ticket_id = ? ORDER BY sequence", (ticket_id,),
        ).fetchall()
        evaluation = connection.execute(
            'SELECT * FROM ticket_evaluations WHERE ticket_id = ?', (ticket_id,),
        ).fetchone()
        evaluation_history = connection.execute(
            'SELECT * FROM evaluation_history WHERE ticket_id = ? ORDER BY id', (ticket_id,),
        ).fetchall()
        files = ticket_files(connection, ticket_id)
        deadline = evaluation_deadline(row['closed_at']) if row['status'] == 'closed' and row['closed_at'] is not None else None
    return {**ticket_response(row), "files": files, "history": [dict(event) for event in history],
            "results": [dict(result) for result in results],
            "cancellation_requests": [dict(request) for request in cancellations],
            "evaluation": dict(evaluation) if evaluation is not None else None,
            "evaluation_history": [dict(event) for event in evaluation_history],
            "evaluation_deadline_at": deadline,
            "evaluation_locked": deadline is None or int(time.time()) >= deadline
            or evaluation is not None and evaluation['source'] == 'automatic'}


def action_ticket(connection: sqlite3.Connection, user: Principal, ticket_id: str):
    predicate, values = ticket_visibility(user)
    row = connection.execute(
        TICKET_SELECT + " WHERE t.id = ? AND " + predicate, [ticket_id, *values],
    ).fetchone()
    if row is None:
        raise missing_resource()
    return row


def check_action(ticket: sqlite3.Row, expected_version: int, statuses: tuple[str, ...]) -> None:
    if ticket["version"] != expected_version:
        raise HTTPException(status_code=409, detail={
            "code": "version_conflict", "message": "工单已更新，请重新读取后再操作",
        })
    if ticket["status"] not in statuses:
        raise HTTPException(status_code=409, detail={
            "code": "invalid_ticket_state", "message": "当前工单状态不允许此操作",
        })


def check_assignee(connection: sqlite3.Connection, assignee_id: str, group_id: str) -> None:
    valid = connection.execute(
        """SELECT 1 FROM users u
           JOIN user_group_scopes s ON s.user_id = u.id AND s.role = 'handler'
           JOIN support_groups g ON g.id = s.group_id
           WHERE u.id = ? AND u.active = 1 AND s.group_id = ? AND g.active = 1""",
        (assignee_id, group_id),
    ).fetchone()
    if valid is None:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_assignee", "message": "请选择本处理组内有效的处理人",
        })


def save_action(
    connection: sqlite3.Connection, ticket: sqlite3.Row, user: Principal,
    action: str, status: str, assignee_id: str | None, note: str | None = None,
    cancellation_request_id: str | None = None,
):
    now = int(time.time())
    assigned_at = ticket["assigned_at"]
    accepted_at = ticket["accepted_at"]
    if action in ("assigned", "reassigned"):
        assigned_at, accepted_at = now, None
    if action == "accepted":
        accepted_at = now
    result_round = ticket['result_round']
    result_submitted_at = ticket['result_submitted_at']
    confirmed_at = ticket['confirmed_at']
    if action == 'result_submitted':
        result_round += 1
        result_submitted_at, confirmed_at = now, None
    if action in ('confirmed_resolved', 'confirmed_unresolved'):
        confirmed_at = now
    closed_at = now if status == 'closed' else ticket['closed_at']
    cancelled_at = now if status == 'cancelled' else ticket['cancelled_at']
    changed = connection.execute(
        """UPDATE tickets SET status = ?, assignee_id = ?, assigned_at = ?, accepted_at = ?,
           result_round = ?, result_submitted_at = ?, confirmed_at = ?, closed_at = ?, cancelled_at = ?,
           version = version + 1, updated_at = ? WHERE id = ? AND version = ?""",
        (status, assignee_id, assigned_at, accepted_at, result_round, result_submitted_at,
         confirmed_at, closed_at, cancelled_at, now, ticket["id"], ticket["version"]),
    )
    if changed.rowcount != 1:
        raise HTTPException(status_code=409, detail={
            "code": "version_conflict", "message": "工单已更新，请重新读取后再操作",
        })
    connection.execute(
        """INSERT INTO ticket_history
           (ticket_id, action, actor_id, from_status, to_status, created_at,
            from_assignee_id, to_assignee_id, note, result_round, cancellation_request_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticket["id"], action, user.id, ticket["status"], status, now,
         ticket["assignee_id"], assignee_id, note,
         result_round if action in ('result_submitted', 'confirmed_resolved', 'confirmed_unresolved') else None,
         cancellation_request_id),
    )
    if status in ('closed', 'cancelled'):
        # Terminal actions retire outstanding requests in the same transaction.
        connection.execute(
            """UPDATE cancellation_requests SET status = 'superseded', decided_by = ?,
               decision_reason = ?, decided_at = ? WHERE ticket_id = ? AND status = 'pending'""",
            (user.id, '工单已结束', now, ticket['id']),
        )
    return ticket_response(connection.execute(TICKET_SELECT + " WHERE t.id = ?", (ticket["id"],)).fetchone())


@router.post("/attachments", status_code=201, openapi_extra={
    "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["files"], "properties": {
            "files": {"type": "array", "minItems": 1, "maxItems": MAX_FILES,
                      "items": {"type": "string", "format": "binary"}},
        },
    }}}},
})
async def upload_attachments(
    request: Request, user: CurrentUser, connection: Database,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    mutation_submitter(user)
    uploads = await read_uploads(request)
    # Recheck identity after receiving the body and acquiring the write lock.
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        user = mutation_submitter(get_current_user(connection, credentials))
        items = []
        for name, content_type, content in uploads:
            file_id = str(uuid4())
            connection.execute(
                """INSERT INTO attachments (id, owner_id, file_name, content_type, size, content, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (file_id, user.id, name, content_type, len(content), content, int(time.time())),
            )
            items.append(dict(connection.execute(
                f"SELECT {FILE_COLUMNS} FROM attachments WHERE id = ?", (file_id,),
            ).fetchone()))
    return {"items": items}


@router.get("/attachments/{file_id}")
def download_attachment(file_id: str, user: CurrentUser, connection: Database):
    predicate, values = ticket_visibility(user)
    row = connection.execute(
        """SELECT a.content, a.file_name FROM attachments a WHERE a.id = ? AND (
            (a.ticket_id IS NULL AND a.owner_id = ?) OR a.ticket_id IN (
                SELECT t.id FROM tickets t WHERE """ + predicate + "))",
        [file_id, user.id, *values],
    ).fetchone()
    if row is None:
        raise missing_resource()
    return Response(content=row["content"], media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row['file_name'], safe='')}",
    })


@router.post("/tickets/{ticket_id}/attachments")
def append_attachments(ticket_id: str, body: AttachmentRequest, user: ActionUser, connection: Database):
    ticket = action_ticket(connection, user, ticket_id)
    require_ticket_submitter(ticket, user)
    check_action(ticket, body.expected_version,
                 ("pending_assignment", "pending_acceptance", "in_progress", "awaiting_confirmation"))
    added = bind_files(connection, body.file_ids, user.id, ticket_id)
    result = ticket_response(ticket)
    if added:
        result = save_action(connection, ticket, user, "attachments_added", ticket["status"],
                             ticket["assignee_id"], f"追加{added}个附件")
    return {**result, "files": ticket_files(connection, ticket_id)}


@router.get("/support-groups/{group_id}/handlers")
def list_handlers(group_id: str, user: CurrentUser, connection: Database):
    require_group_scope(user, "dispatcher", group_id)
    rows = connection.execute(
        """SELECT u.id, u.display_name FROM users u
           JOIN user_group_scopes s ON s.user_id = u.id AND s.role = 'handler'
           JOIN support_groups g ON g.id = s.group_id
           WHERE u.active = 1 AND s.group_id = ? AND g.active = 1 ORDER BY u.id""", (group_id,),
    ).fetchall()
    return {"items": [dict(row) for row in rows]}


@router.post("/tickets/{ticket_id}/assignments")
def assign_ticket(ticket_id: str, body: AssignmentRequest, user: ActionUser, connection: Database):
    ticket = action_ticket(connection, user, ticket_id)
    require_group_scope(user, "dispatcher", ticket["group_id"])
    check_action(ticket, body.expected_version, ("pending_assignment",))
    check_assignee(connection, body.assignee_id, ticket["group_id"])
    return save_action(connection, ticket, user, "assigned", "pending_acceptance", body.assignee_id)


@router.post("/tickets/{ticket_id}/reassignments")
def reassign_ticket(ticket_id: str, body: ReassignmentRequest, user: ActionUser, connection: Database):
    ticket = action_ticket(connection, user, ticket_id)
    require_group_scope(user, "dispatcher", ticket["group_id"])
    check_action(ticket, body.expected_version, ("pending_acceptance", "in_progress"))
    check_assignee(connection, body.assignee_id, ticket["group_id"])
    if body.assignee_id == ticket["assignee_id"]:
        raise HTTPException(status_code=422, detail={
            "code": "same_assignee", "message": "改派目标须与当前处理人不同",
        })
    return save_action(
        connection, ticket, user, "reassigned", "pending_acceptance", body.assignee_id, body.reason,
    )


def require_current_handler(ticket: sqlite3.Row, user: Principal) -> None:
    if "handler" not in user.roles or ticket["assignee_id"] != user.id:
        raise forbidden()


@router.post("/tickets/{ticket_id}/accept")
def accept_ticket(ticket_id: str, body: ActionRequest, user: ActionUser, connection: Database):
    ticket = action_ticket(connection, user, ticket_id)
    require_current_handler(ticket, user)
    check_action(ticket, body.expected_version, ("pending_acceptance",))
    return save_action(connection, ticket, user, "accepted", "in_progress", user.id)


@router.post("/tickets/{ticket_id}/notes")
def add_note(ticket_id: str, body: NoteRequest, user: ActionUser, connection: Database):
    ticket = action_ticket(connection, user, ticket_id)
    require_current_handler(ticket, user)
    check_action(ticket, body.expected_version, ("in_progress",))
    return save_action(connection, ticket, user, "note_added", ticket["status"], user.id, body.note)


def require_ticket_submitter(ticket: sqlite3.Row, user: Principal) -> None:
    if 'submitter' not in user.roles or ticket['submitter_id'] != user.id:
        raise forbidden()


@router.post('/tickets/{ticket_id}/results')
def submit_result(ticket_id: str, body: ResultRequest, user: ActionUser, connection: Database):
    ticket = action_ticket(connection, user, ticket_id)
    require_current_handler(ticket, user)
    check_action(ticket, body.expected_version, ('in_progress',))
    updated = save_action(connection, ticket, user, 'result_submitted', 'awaiting_confirmation', user.id)
    connection.execute(
        """INSERT INTO ticket_results (ticket_id, round, handler_id, content, submitted_at)
           VALUES (?, ?, ?, ?, ?)""",
        (ticket_id, updated['result_round'], user.id, body.result, updated['result_submitted_at']),
    )
    return updated


@router.post('/tickets/{ticket_id}/confirmations')
def confirm_result(ticket_id: str, body: ConfirmationRequest, user: ActionUser, connection: Database):
    ticket = action_ticket(connection, user, ticket_id)
    require_ticket_submitter(ticket, user)
    check_action(ticket, body.expected_version, ('awaiting_confirmation',))
    if body.result_round != ticket['result_round']:
        raise HTTPException(status_code=409, detail={
            'code': 'result_round_conflict', 'message': '处理结果轮次已变化，请重新读取',
        })
    status = 'closed' if body.outcome == 'resolved' else 'in_progress'
    updated = save_action(connection, ticket, user, 'confirmed_' + body.outcome, status,
                          ticket['assignee_id'], body.note or None)
    connection.execute(
        """UPDATE ticket_results SET confirmation = ?, confirmed_by = ?, confirmed_at = ?
           WHERE ticket_id = ? AND round = ?""",
        (body.outcome, user.id, updated['confirmed_at'], ticket_id, body.result_round),
    )
    return updated


@router.post('/tickets/{ticket_id}/cancel')
def cancel_ticket(ticket_id: str, body: CancellationRequest, user: ActionUser, connection: Database):
    ticket = action_ticket(connection, user, ticket_id)
    require_ticket_submitter(ticket, user)
    check_action(ticket, body.expected_version, ('pending_assignment', 'pending_acceptance'))
    if ticket['cancellation_requires_approval']:
        raise HTTPException(status_code=409, detail={
            'code': 'cancellation_approval_required', 'message': '工单已进入过处理阶段，请申请撤销并由当前处理人审批',
        })
    return save_action(connection, ticket, user, 'cancelled', 'cancelled', ticket['assignee_id'], body.reason)


@router.post('/tickets/{ticket_id}/cancellation-requests')
def request_cancellation(ticket_id: str, body: CancellationRequest, user: ActionUser, connection: Database):
    ticket = action_ticket(connection, user, ticket_id)
    require_ticket_submitter(ticket, user)
    check_action(ticket, body.expected_version, ('pending_acceptance', 'in_progress', 'awaiting_confirmation'))
    if not ticket['cancellation_requires_approval']:
        raise HTTPException(status_code=409, detail={
            'code': 'invalid_ticket_state', 'message': '尚未进入处理阶段，可直接撤销',
        })
    pending = connection.execute(
        "SELECT 1 FROM cancellation_requests WHERE ticket_id = ? AND status = 'pending'", (ticket_id,),
    ).fetchone()
    if pending:
        raise HTTPException(status_code=409, detail={
            'code': 'cancellation_pending', 'message': '已有待处理的撤销申请',
        })
    request_id = str(uuid4())
    updated = save_action(connection, ticket, user, 'cancellation_requested', ticket['status'],
                          ticket['assignee_id'], body.reason, request_id)
    connection.execute(
        """INSERT INTO cancellation_requests (id, ticket_id, requested_by, reason, requested_at)
           VALUES (?, ?, ?, ?, ?)""", (request_id, ticket_id, user.id, body.reason, updated['updated_at']),
    )
    return updated


@router.post('/tickets/{ticket_id}/cancellation-requests/{request_id}/decision')
def decide_cancellation(
    ticket_id: str, request_id: str, body: CancellationDecisionRequest, user: ActionUser, connection: Database,
):
    ticket = action_ticket(connection, user, ticket_id)
    require_current_handler(ticket, user)
    check_action(ticket, body.expected_version, ('pending_acceptance', 'in_progress', 'awaiting_confirmation'))
    request = connection.execute(
        'SELECT * FROM cancellation_requests WHERE id = ? AND ticket_id = ?', (request_id, ticket_id),
    ).fetchone()
    if request is None:
        raise missing_resource()
    if request['status'] != 'pending':
        raise HTTPException(status_code=409, detail={
            'code': 'cancellation_decided', 'message': '撤销申请已处理',
        })
    now = int(time.time())
    connection.execute(
        """UPDATE cancellation_requests SET status = ?, decided_by = ?, decision_reason = ?, decided_at = ?
           WHERE id = ?""", (body.decision, user.id, body.reason, now, request_id),
    )
    status = 'cancelled' if body.decision == 'approved' else ticket['status']
    return save_action(connection, ticket, user, 'cancellation_' + body.decision, status,
                       ticket['assignee_id'], body.reason, request_id)


@router.put('/tickets/{ticket_id}/evaluation')
def evaluate_ticket(ticket_id: str, body: EvaluationRequest, user: ActionUser, connection: Database):
    ticket = action_ticket(connection, user, ticket_id)
    require_ticket_submitter(ticket, user)
    check_action(ticket, body.expected_version, ('closed',))
    now = int(time.time())
    existing = connection.execute(
        'SELECT source FROM ticket_evaluations WHERE ticket_id = ?', (ticket_id,),
    ).fetchone()
    if (ticket['closed_at'] is None or now >= evaluation_deadline(ticket['closed_at'])
            or existing is not None and existing['source'] == 'automatic'):
        raise HTTPException(status_code=409, detail={
            'code': 'evaluation_locked', 'message': '评价已到期锁定',
        })
    if body.rating != 'good' and not body.reason:
        raise HTTPException(status_code=422, detail={
            'code': 'evaluation_reason_required', 'message': '中评和差评必须填写原因',
        })
    save_evaluation(connection, ticket, body.rating, body.reason or None, 'active', user.id, now)
    return ticket_response(connection.execute(TICKET_SELECT + ' WHERE t.id = ?', (ticket_id,)).fetchone())


@router.get('/evaluation-statistics')
def evaluation_statistics(
    user: Annotated[Principal, Depends(require_role('manager'))], connection: Database,
    group_id: Identifier | None = None,
):
    groups = user.group_scopes.get('manager', ())
    if group_id is not None:
        require_group_scope(user, 'manager', group_id)
        groups = (group_id,)
    placeholders = ', '.join('?' for _ in groups) or 'NULL'
    now = int(time.time())
    with connection:
        connection.execute('BEGIN')
        rows = connection.execute(
            f"""SELECT e.source, e.rating, COUNT(*) AS count FROM tickets t
                JOIN ticket_evaluations e ON e.ticket_id = t.id
                WHERE t.status = 'closed' AND t.closed_at IS NOT NULL AND t.group_id IN ({placeholders})
                GROUP BY e.source, e.rating""", groups,
        ).fetchall()
        totals = connection.execute(
            f"""SELECT COUNT(*) AS closed_total,
                COALESCE(SUM(e.ticket_id IS NULL AND t.closed_at < ?), 0) AS overdue_unevaluated
                FROM tickets t LEFT JOIN ticket_evaluations e ON e.ticket_id = t.id
                WHERE t.status = 'closed' AND t.closed_at IS NOT NULL AND t.group_id IN ({placeholders})""",
            (expiration_cutoff(now), *groups),
        ).fetchone()
    counts = {source: dict.fromkeys(('good', 'neutral', 'bad'), 0) for source in ('active', 'automatic')}
    for row in rows:
        counts[row['source']][row['rating']] = row['count']
    active = sum(counts['active'].values())
    automatic = sum(counts['automatic'].values())
    closed = totals['closed_total']
    return {'as_of': now, 'group_ids': list(groups), 'closed_total': closed,
            'active': counts['active'], 'automatic': counts['automatic'],
            'active_total': active, 'automatic_total': automatic,
            'unevaluated_total': closed - active - automatic,
            'overdue_unevaluated': totals['overdue_unevaluated'],
            'participation_rate': active / closed if closed else None}
